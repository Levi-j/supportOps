import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from supportops import render
from supportops.cli.state import get_state
from supportops.errors import ConfigError, ExitCode
from supportops.http_checks import (
    REQUEST_ID_PATTERN,
    HttpResult,
    auth_headers,
    create_client,
    resolve_credentials,
    response_hint,
    send,
)
from supportops.latency import MAX_REQUESTS, LatencyReport, measure_latency
from supportops.settings import load_settings
from supportops.targets import get_target
from supportops.write_guard import check_write_request, confirm_lab_service, is_write

METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
MAX_BODY_BYTES = 1_000_000
SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-api-key"})

app = typer.Typer(
    help="Send diagnostic requests to the API and measure its response times.",
    no_args_is_help=True,
)

NoAuth = Annotated[bool, typer.Option("--no-auth", help="Send the request without an API key.")]
KeyEnv = Annotated[
    str | None,
    typer.Option("--key-env", help="Read the API key from this environment variable instead."),
]
JsonOutput = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")]


class RequestReport(BaseModel):
    result: HttpResult
    credentials: str
    hint: str | None


@app.command("request")
def request_command(
    ctx: typer.Context,
    method: Annotated[str, typer.Argument(help="HTTP method, for example GET or POST.")],
    path: Annotated[str, typer.Argument(help="Path on the API, for example /v1/account.")],
    data: Annotated[str | None, typer.Option("--data", help="Request body as inline JSON.")] = None,
    data_file: Annotated[
        Path | None,
        typer.Option("--data-file", help="Read the request body from this file.", dir_okay=False),
    ] = None,
    raw: Annotated[
        bool,
        typer.Option("--raw", help="Send the body exactly as given, even if it isn't valid JSON."),
    ] = False,
    header: Annotated[
        list[str] | None,
        typer.Option("--header", "-H", help="Extra header as 'Name: value'. Repeatable."),
    ] = None,
    request_id: Annotated[
        str | None,
        typer.Option("--request-id", help="Use this X-Request-Id instead of a generated one."),
    ] = None,
    no_auth: NoAuth = False,
    key_env: KeyEnv = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm a request that can change data (lab only).")
    ] = False,
    full: Annotated[bool, typer.Option("--full", help="Show the whole response body.")] = False,
    json_output: JsonOutput = False,
) -> None:
    """Send one diagnostic request and explain the response."""
    settings = load_settings(get_state(ctx).env_file).settings
    profile = get_target(settings.target)
    method = _checked_method(method)
    _check_path(path)
    if is_write(method):
        check_write_request(method, confirmed=yes, api_url=str(settings.api_url), profile=profile)
    if request_id is not None and not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ConfigError(
            "--request-id may only contain letters, digits, '_' and '-' (at most 64).",
            hint="The API replaces request IDs that don't follow this rule.",
        )
    headers = _parse_headers(header or [])
    body = _read_body(data, data_file, raw)
    if body is not None and not any(name.lower() == "content-type" for name in headers):
        headers["Content-Type"] = "application/json"
    credentials = resolve_credentials(settings, no_auth=no_auth, key_env=key_env)
    with create_client(settings) as client:
        if is_write(method):
            confirm_lab_service(client, profile)
        result = send(
            client,
            method,
            path,
            headers={**headers, **auth_headers(credentials, profile)},
            content=body,
            request_id=request_id,
            full_body=full,
        )
    hint = result.failure.hint if result.failure else response_hint(result, credentials, profile)
    report = RequestReport(result=result, credentials=credentials.describe(), hint=hint)
    if json_output:
        render.emit_json(report)
    else:
        _print_request_report(report)
    if result.status is None or result.status >= 400:
        raise typer.Exit(int(ExitCode.PROBLEM))


@app.command("latency")
def latency_command(
    ctx: typer.Context,
    path: Annotated[str, typer.Argument(help="Path to request, for example /v1/invoices.")],
    count: Annotated[
        int,
        typer.Option(
            "--count",
            "-n",
            min=1,
            max=MAX_REQUESTS,
            help=f"Number of sequential GET requests (at most {MAX_REQUESTS}).",
        ),
    ] = 20,
    threshold_ms: Annotated[
        float | None,
        typer.Option(
            "--threshold-ms",
            min=1,
            help="Slow-response threshold for p95. Defaults to SUPPORTOPS_SLOW_REQUEST_MS.",
        ),
    ] = None,
    no_auth: NoAuth = False,
    key_env: KeyEnv = None,
    json_output: JsonOutput = False,
) -> None:
    """Measure response times of repeated GET requests, one after another."""
    settings = load_settings(get_state(ctx).env_file).settings
    profile = get_target(settings.target)
    _check_path(path)
    credentials = resolve_credentials(settings, no_auth=no_auth, key_env=key_env)
    with create_client(settings) as client:
        report = measure_latency(
            client,
            path,
            count,
            threshold_ms=threshold_ms or settings.slow_request_ms,
            headers=auth_headers(credentials, profile),
        )
    if json_output:
        render.emit_json(report)
    else:
        _print_latency_report(report)
    if not report.ok:
        raise typer.Exit(int(ExitCode.PROBLEM))


def _checked_method(method: str) -> str:
    upper = method.upper()
    if upper not in METHODS:
        raise ConfigError(f"Unsupported HTTP method '{method}'. Use one of: {', '.join(METHODS)}.")
    return upper


def _check_path(path: str) -> None:
    if not path.startswith("/"):
        raise ConfigError(
            f"The path must start with '/', for example /v1/account (got '{path}').",
            hint="The base URL comes from SUPPORTOPS_API_URL, so pass only the path.",
        )


def _parse_headers(items: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition(":")
        name = name.strip()
        if not separator or not name:
            raise ConfigError("--header must look like 'Name: value'.")
        if name.lower() in SENSITIVE_HEADERS:
            raise ConfigError(
                f"The {name} header can't be set with --header.",
                hint="Credentials come from SUPPORTOPS_API_KEY or --key-env, so they never "
                "appear on the command line or in your shell history.",
            )
        if name.lower() == "x-request-id":
            raise ConfigError("Use --request-id to choose the request ID.")
        headers[name] = value.strip()
    return headers


def _read_body(data: str | None, data_file: Path | None, raw: bool) -> bytes | None:
    if data is not None and data_file is not None:
        raise ConfigError("Use either --data or --data-file, not both.")
    if data_file is not None:
        if not data_file.is_file():
            raise ConfigError(f"File not found: {data_file}")
        content = data_file.read_bytes()
        if len(content) > MAX_BODY_BYTES:
            raise ConfigError(f"{data_file} is larger than {MAX_BODY_BYTES} bytes.")
        source = f"The file {data_file}"
    elif data is not None:
        content = data.encode("utf-8")
        source = "The --data value"
    else:
        return None
    if not raw:
        _require_json(content, source, inline=data is not None)
    return content


def _require_json(content: bytes, source: str, *, inline: bool) -> None:
    try:
        json.loads(content)
    except UnicodeDecodeError:
        raise ConfigError(
            f"{source} isn't valid UTF-8 or UTF-16 text.",
            hint="Save it as UTF-8, or add --raw to send the bytes unchanged on purpose.",
        ) from None
    except json.JSONDecodeError as exc:
        hint = "Fix the JSON, or add --raw to send it unchanged on purpose."
        if inline and b'"' not in content and (b"{" in content or b"[" in content):
            hint = (
                "The JSON has no double quotes left. Windows PowerShell 5.1 strips embedded "
                "double quotes when it passes arguments to programs, so put the JSON in a file "
                "and use --data-file instead. Add --raw only if you want to send it unchanged."
            )
        raise ConfigError(
            f"{source} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).",
            hint=hint,
        ) from None


def _print_request_report(report: RequestReport) -> None:
    result = report.result
    render.emit_line(f"{result.method} {result.url}", style="bold")
    if result.failure is not None:
        render.emit_line(
            f"Failed after {result.duration_ms:.0f} ms: {result.failure.category}", style="bold red"
        )
        render.emit_line(result.failure.summary)
        render.emit_line(f"Detail: {result.failure.detail}")
        render.emit_line(
            f"Request ID: {result.request_id} (no response, so the API has nothing logged under it)"
        )
    else:
        style = "bold green" if result.status is not None and result.status < 400 else "bold red"
        render.emit_line(
            f"{result.status} {result.reason} in {result.duration_ms:.0f} ms", style=style
        )
        render.emit_line(f"Request ID: {result.request_id} {_echo_note(result)}")
    render.emit_line(f"Credentials: {report.credentials}")
    if result.problem is not None:
        problem = result.problem
        render.emit_line(
            f"Problem: {problem.code or problem.title} - {problem.detail or ''}".rstrip(" -")
        )
        for error in problem.errors:
            location = ".".join(
                str(error.get(key, "")) for key in ("location", "field") if error.get(key)
            )
            render.emit_line(f"  {location}: {error.get('message', '')} ({error.get('type', '')})")
    if result.headers:
        render.emit_line("Headers:")
        for name, value in result.headers.items():
            render.emit_line(f"  {name}: {value}")
    if result.body is not None:
        render.emit_line("Body:")
        for line in result.body.splitlines():
            render.emit_line(f"  {line}")
        if result.body_truncated:
            render.emit_line(
                f"  ... truncated after {len(result.body)} of {result.body_characters} "
                "characters; use --full to see everything"
            )
    if report.hint:
        render.emit_line(f"Hint: {report.hint}", style="yellow")


def _echo_note(result: HttpResult) -> str:
    if result.response_request_id is None:
        return "(the response had no X-Request-Id header)"
    if result.response_request_id != result.request_id:
        return f"(the server used {result.response_request_id} instead)"
    return "(echoed by the server)"


def _print_latency_report(report: LatencyReport) -> None:
    render.emit_line(f"GET {report.url}: {report.requested} sequential requests", style="bold")
    outcomes = ", ".join(f"{outcome} x{count}" for outcome, count in report.status_counts.items())
    render.emit_line(f"Responses: {outcomes}")
    if report.successful == 0:
        render.emit_line(
            "No successful responses, so there are no latency figures.", style="bold red"
        )
        return
    render.emit_line(
        f"Successful: {report.successful} of {report.requested}   "
        f"p50 {report.p50_ms:.1f} ms   p95 {report.p95_ms:.1f} ms   max {report.max_ms:.1f} ms   "
        f"(threshold {report.threshold_ms:.0f} ms)"
    )
    render.emit_line(
        "Slowest: "
        + ", ".join(f"{sample.request_id} {sample.duration_ms:.1f} ms" for sample in report.slowest)
    )
    if report.failed:
        render.emit_line(
            f"{report.failed} of {report.requested} requests failed; the latency figures cover "
            "only the successful ones.",
            style="bold red",
        )
    if report.threshold_exceeded:
        render.emit_line(
            f"p95 ({report.p95_ms:.1f} ms) is above the {report.threshold_ms:.0f} ms threshold.",
            style="bold red",
        )
    for note in report.notes:
        render.emit_line(f"Note: {note}", style="yellow")
    if report.ok:
        render.emit_line("All requests succeeded within the threshold.", style="bold green")
