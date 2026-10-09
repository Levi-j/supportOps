from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor

import httpx

from supportops_lab.docker import UnsafeOperation
from supportops_lab.ownership import LOOPBACK, RESERVED_HOST_PORTS
from supportops_lab.scenarios import CustomerRequest
from supportops_lab.state import SentRequest

USER_AGENT = "supportops-lab-customer/1.0"

Watcher = Callable[[Future[SentRequest]], None]


def send_requests(
    base_url: str,
    requests: Sequence[CustomerRequest],
    transport: httpx.BaseTransport | None = None,
    *,
    watched_request: str | None = None,
    watcher: Watcher | None = None,
) -> list[SentRequest]:
    url = httpx.URL(base_url)
    if url.host != LOOPBACK or url.port is None or url.port in RESERVED_HOST_PORTS:
        raise UnsafeOperation(
            f"{base_url} isn't a scenario lab address, so no customer request was sent."
        )
    with httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(15.0, connect=3.0),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    ) as client:
        confirm_lab_api(client)
        sent = []
        for request in requests:
            if watcher is not None and request.request_id == watched_request:
                sent.append(_send_watched(client, request, watcher))
            else:
                sent.append(_send(client, request))
        return sent


def confirm_lab_api(client: httpx.Client) -> None:
    try:
        response = client.get("/health")
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        body = None
    if not isinstance(body, dict) or body.get("environment") != "lab":
        raise UnsafeOperation(
            f"The API at {client.base_url} didn't confirm it is a lab service, so no customer "
            "request was sent.",
            hint="Run 'supportops-lab status' and 'supportops-lab reset'.",
        )


def _send_watched(client: httpx.Client, request: CustomerRequest, watcher: Watcher) -> SentRequest:
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="customer") as pool:
        future = pool.submit(_send, client, request)
        watcher(future)
        return future.result()


def _send(client: httpx.Client, request: CustomerRequest) -> SentRequest:
    headers = {"X-Request-Id": request.request_id}
    if request.api_key is not None:
        headers["Authorization"] = f"Bearer {request.api_key}"
    if request.body is not None:
        headers["Content-Type"] = "application/json"
    sent = SentRequest(
        request_id=request.request_id,
        method=request.method,
        path=request.path,
        expected_status=request.expected_status,
        expected_code=request.expected_code,
    )
    try:
        response = client.request(
            request.method, request.path, headers=headers, content=request.body
        )
    except httpx.HTTPError as exc:
        sent.error = type(exc).__name__
        return sent
    sent.status = response.status_code
    sent.echoed_request_id = response.headers.get("X-Request-Id")
    sent.retry_after = response.headers.get("Retry-After")
    sent.problem_code = _problem_code(response)
    return sent


def _problem_code(response: httpx.Response) -> str | None:
    if response.status_code < 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None
