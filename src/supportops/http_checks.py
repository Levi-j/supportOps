import contextlib
import json
import os
import re
import secrets
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, Field

from supportops import __version__
from supportops.errors import ConfigError
from supportops.redaction import mask_api_key
from supportops.settings import Settings
from supportops.targets import TargetProfile

REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
REPORTED_HEADERS = (
    "content-type",
    "content-length",
    "x-request-id",
    "location",
    "retry-after",
    "www-authenticate",
    "allow",
)
BODY_PREVIEW_CHARACTERS = 4000
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAY_HAVE_REACHED_SERVER = frozenset({"read_timeout", "timeout", "connection_closed"})
UNCERTAIN_WRITE_NOTE = (
    "The request may already have reached the server and taken effect. Check the resource "
    "before sending it again; SupportOps never retries a write by itself."
)
LOCALHOST_NOTE = (
    "On Windows, localhost tries IPv6 (::1) first, which can add seconds to every new "
    "connection when the service only listens on IPv4. Try 127.0.0.1 instead."
)

Clock = Callable[[], float]


class TransportFailure(BaseModel):
    category: str
    summary: str
    detail: str
    hint: str


class Problem(BaseModel):
    code: str | None = None
    title: str | None = None
    detail: str | None = None
    status: int | None = None
    request_id: str | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)


class HttpResult(BaseModel):
    method: str
    url: str
    request_id: str
    duration_ms: float
    status: int | None = None
    reason: str | None = None
    response_request_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    body_truncated: bool = False
    body_characters: int = 0
    problem: Problem | None = None
    failure: TransportFailure | None = None

    def json_body(self) -> Any:
        if self.body is None or self.body_truncated:
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            return None


@dataclass(frozen=True)
class Credentials:
    api_key: str | None = field(repr=False)
    source: str

    def describe(self) -> str:
        if self.api_key is None:
            return self.source
        return f"API key {mask_api_key(self.api_key)} from {self.source}"


def create_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=str(settings.api_url),
        timeout=httpx.Timeout(
            settings.http_timeout_seconds, connect=settings.connect_timeout_seconds
        ),
        follow_redirects=False,
        headers={"User-Agent": f"supportops/{__version__}"},
        transport=transport,
    )


def new_request_id(prefix: str = "supportops") -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def resolve_credentials(settings: Settings, *, no_auth: bool, key_env: str | None) -> Credentials:
    if no_auth and key_env:
        raise ConfigError("Use either --no-auth or --key-env, not both.")
    if no_auth:
        return Credentials(api_key=None, source="no credentials (--no-auth)")
    if key_env:
        value = os.environ.get(key_env)
        if not value:
            raise ConfigError(
                f"Environment variable {key_env} is not set or is empty.",
                hint=f'Set it first, for example: $env:{key_env} = "bk_..." (PowerShell).',
            )
        return _checked(Credentials(api_key=value, source=f"environment variable {key_env}"))
    if settings.api_key is None:
        return Credentials(api_key=None, source="no credentials (SUPPORTOPS_API_KEY is not set)")
    return _checked(
        Credentials(api_key=settings.api_key.get_secret_value(), source="SUPPORTOPS_API_KEY")
    )


def auth_headers(credentials: Credentials, profile: TargetProfile) -> dict[str, str]:
    if credentials.api_key is None:
        return {}
    return {"Authorization": f"{profile.auth_scheme} {credentials.api_key}"}


def send(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    content: bytes | None = None,
    request_id: str | None = None,
    full_body: bool = False,
    clock: Clock = time.perf_counter,
) -> HttpResult:
    request_id = request_id or new_request_id()
    request = client.build_request(
        method, path, headers={**(headers or {}), REQUEST_ID_HEADER: request_id}, content=content
    )
    url = str(request.url)
    started = clock()
    try:
        response = client.send(request)
    except httpx.TransportError as exc:
        return HttpResult(
            method=method,
            url=url,
            request_id=request_id,
            duration_ms=_milliseconds(clock() - started),
            failure=describe_failure(exc, request),
        )
    duration_ms = _milliseconds(clock() - started)
    body, truncated = _body_preview(response, full_body)
    return HttpResult(
        method=method,
        url=url,
        request_id=request_id,
        duration_ms=duration_ms,
        status=response.status_code,
        reason=response.reason_phrase,
        response_request_id=response.headers.get(REQUEST_ID_HEADER),
        headers={
            name: response.headers[name] for name in REPORTED_HEADERS if name in response.headers
        },
        body=body,
        body_truncated=truncated,
        body_characters=len(response.text),
        problem=_problem(response),
    )


def classify_transport_error(exc: httpx.TransportError) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    causes = _cause_chain(exc)
    if any(isinstance(cause, socket.gaierror) for cause in causes):
        return "dns_failure"
    if any(isinstance(cause, ConnectionRefusedError) for cause in causes):
        return "connection_refused"
    if any(isinstance(cause, ssl.SSLError) for cause in causes):
        return "tls_error"
    if isinstance(exc, httpx.ReadError | httpx.WriteError | httpx.RemoteProtocolError):
        return "connection_closed"
    if isinstance(exc, httpx.ConnectError):
        return "connection_failed"
    return "transport_error"


def describe_failure(exc: httpx.TransportError, request: httpx.Request) -> TransportFailure:
    category = classify_transport_error(exc)
    url = request.url
    host = url.host
    port = url.port or (443 if url.scheme == "https" else 80)
    certificate = any(
        isinstance(cause, ssl.SSLCertVerificationError) for cause in _cause_chain(exc)
    )
    summary, hint = _FAILURE_TEXT[category]
    if category == "tls_error" and certificate:
        summary, hint = _CERTIFICATE_TEXT
    hint = hint.format(host=host, port=port)
    if request.method in WRITE_METHODS and category in MAY_HAVE_REACHED_SERVER:
        hint = f"{hint} {UNCERTAIN_WRITE_NOTE}"
    return TransportFailure(
        category=category,
        summary=summary.format(host=host, port=port),
        detail=_first_line(exc),
        hint=hint,
    )


def response_hint(
    result: HttpResult, credentials: Credentials, profile: TargetProfile
) -> str | None:
    status = result.status
    if status is None or status < 300:
        return None
    request_id = result.response_request_id or result.request_id
    code = result.problem.code if result.problem else None
    if status < 400:
        location = result.headers.get("location", "another URL")
        return (
            f"The server redirected to {location}. SupportOps doesn't follow redirects; "
            "check the path and SUPPORTOPS_API_URL."
        )
    if status == 400:
        return (
            "The server couldn't read the request. If you sent a body, check that it is valid "
            "JSON and that Content-Type is application/json."
        )
    if status == 401:
        return (
            f"The API rejected the credentials ({credentials.describe()}). Check that the key "
            "is current and not revoked or expired. The API deliberately gives the same answer "
            f"for every bad key; its logs record the exact reason under request ID {request_id}."
        )
    if status == 403:
        if code == "ACCOUNT_SUSPENDED":
            return "The key was accepted, but the account is suspended. This needs account support."
        return "The credentials are valid, but they don't allow this operation."
    if status == 404:
        return (
            "Nothing exists at this path for this account. Check the path and any IDs; "
            "resources that belong to another account also return 404. To see which account "
            f"the key belongs to, run: supportops api request GET {profile.account_path}"
        )
    if status == 405:
        return (
            f"This method isn't supported here (allowed: {result.headers.get('allow', 'unknown')})."
        )
    if status == 409:
        return "The request conflicts with the resource's current state; see the problem detail."
    if status == 422:
        return "The server read the JSON but rejected some fields; see the field list above."
    if status == 429:
        return "Too many requests. Wait for the Retry-After period before trying again."
    if status == 503:
        retry = result.headers.get("retry-after")
        retry_note = f" The server suggests retrying after {retry} seconds." if retry else ""
        return (
            "The service is temporarily unavailable or busy." + retry_note + " Run "
            "'supportops health' to check its readiness and database."
        )
    if status >= 500:
        return (
            "The server failed while handling the request. Search its logs for request ID "
            f"{request_id} to find the error and stack trace."
        )
    return None


_FAILURE_TEXT = {
    "dns_failure": (
        "The host name {host} could not be resolved.",
        "Check the host in SUPPORTOPS_API_URL for typos and whether the name resolves from "
        "this machine (PowerShell: Resolve-DnsName {host}; Linux: getent hosts {host}).",
    ),
    "connection_refused": (
        "Nothing accepted the connection on {host}:{port}.",
        "The service isn't running or isn't listening on that port. Check 'docker compose ps' "
        "and that the port in SUPPORTOPS_API_URL matches the published port (the lab uses 8001).",
    ),
    "connect_timeout": (
        "No answer to the connection attempt to {host}:{port} before the connect timeout.",
        "The host may be down or unreachable, or a firewall may be silently dropping traffic. "
        "Check the address and the network path before raising SUPPORTOPS_CONNECT_TIMEOUT_SECONDS.",
    ),
    "read_timeout": (
        "Connected to {host}:{port}, but no response arrived before the read timeout.",
        "The service accepted the connection but didn't answer in time. It may be overloaded, "
        "stuck, or waiting on a slow dependency such as its database. Check its logs and "
        "readiness with 'supportops health'.",
    ),
    "timeout": (
        "The request to {host}:{port} timed out.",
        "The connection was established, but the exchange didn't finish in time. Check the "
        "service's logs and the network path.",
    ),
    "tls_error": (
        "The TLS handshake with {host}:{port} failed.",
        "Check the URL scheme: the lab API speaks plain http://, so https:// fails there. "
        "For a real HTTPS service, check its TLS configuration.",
    ),
    "connection_closed": (
        "The connection to {host}:{port} closed before a complete response arrived.",
        "The service, or something in between such as a proxy, dropped the connection. Check "
        "the service's logs; the process may have crashed or restarted.",
    ),
    "connection_failed": (
        "The connection to {host}:{port} failed.",
        "SupportOps couldn't identify the cause from the error. The detail above shows the "
        "operating system's message.",
    ),
    "transport_error": (
        "The request to {host}:{port} failed before an HTTP response arrived.",
        "SupportOps couldn't classify this failure. The detail above shows the original error.",
    ),
}

_CERTIFICATE_TEXT = (
    "The TLS certificate presented by {host}:{port} could not be verified.",
    "The certificate may be expired, self-signed, or issued for a different host name. Check "
    "the certificate before trusting the connection.",
)


def _checked(credentials: Credentials) -> Credentials:
    key = credentials.api_key or ""
    if key != key.strip() or any(character.isspace() for character in key):
        raise ConfigError(
            f"The API key from {credentials.source} contains spaces or line breaks.",
            hint="Remove them; they often sneak in when a key is copied from a document or chat.",
        )
    return credentials


def _body_preview(response: httpx.Response, full_body: bool) -> tuple[str | None, bool]:
    text = response.text
    if not text:
        return None, False
    if _is_json(response.headers.get("content-type", "")):
        with contextlib.suppress(ValueError):
            text = json.dumps(response.json(), indent=2, ensure_ascii=False)
    if not full_body and len(text) > BODY_PREVIEW_CHARACTERS:
        return text[:BODY_PREVIEW_CHARACTERS], True
    return text, False


def _problem(response: httpx.Response) -> Problem | None:
    if "application/problem+json" not in response.headers.get("content-type", ""):
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    errors = body.get("errors")
    return Problem(
        code=_text(body.get("code")),
        title=_text(body.get("title")),
        detail=_text(body.get("detail")),
        status=body["status"] if isinstance(body.get("status"), int) else None,
        request_id=_text(body.get("request_id") or body.get("requestId")),
        errors=[error for error in errors if isinstance(error, dict)]
        if isinstance(errors, list)
        else [],
    )


def _is_json(content_type: str) -> bool:
    media_type = content_type.split(";")[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _cause_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _first_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return lines[0][:300] if lines else type(exc).__name__


def _milliseconds(seconds: float) -> float:
    return round(seconds * 1000, 1)
