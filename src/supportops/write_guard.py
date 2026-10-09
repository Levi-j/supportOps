import httpx

from supportops.errors import ExitCode, SupportOpsError
from supportops.http_checks import WRITE_METHODS, send
from supportops.targets import TargetProfile

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class WriteRefused(SupportOpsError):
    exit_code = ExitCode.USAGE


def is_write(method: str) -> bool:
    return method.upper() in WRITE_METHODS


def check_method_allowed(method: str, profile: TargetProfile) -> None:
    if method.upper() not in profile.allowed_methods:
        _refuse_read_only(method, profile)


def check_write_request(
    method: str, *, confirmed: bool, api_url: str, profile: TargetProfile
) -> None:
    if not profile.lab_writes_allowed:
        _refuse_read_only(method, profile)
    if not confirmed:
        raise WriteRefused(
            f"{method} can change data on the server, so it needs --yes. Nothing was sent.",
            hint="Add --yes only if you really intend to change data in the local lab.",
        )
    host = httpx.URL(api_url).host
    if host not in LOOPBACK_HOSTS:
        raise WriteRefused(
            f"Write requests are only allowed against the local lab, but SUPPORTOPS_API_URL "
            f"points to '{host}'. Nothing was sent.",
            hint="SupportOps never changes data on remote or production systems.",
        )


def _refuse_read_only(method: str, profile: TargetProfile) -> None:
    raise WriteRefused(
        f"{method.upper()} requests are not allowed against the '{profile.name}' target: "
        "SupportOps only reads from it. Nothing was sent.",
        hint="Use GET requests to diagnose this target. Changes belong to the service's owners "
        "and their own tools; --yes doesn't change this.",
    )


def confirm_lab_service(client: httpx.Client, profile: TargetProfile) -> None:
    result = send(client, "GET", profile.liveness_path)
    if result.failure is not None:
        raise WriteRefused(
            "Couldn't confirm that the target is the local lab, so the write request was not "
            f"sent. {result.failure.summary}",
            hint=result.failure.hint,
        )
    body = result.json_body()
    environment = body.get("environment") if isinstance(body, dict) else None
    if result.status != 200 or environment != "lab":
        raise WriteRefused(
            f"The service at {result.url} reports environment '{environment}' instead of 'lab', "
            "so the write request was not sent.",
            hint="Write requests only go to the local lab. Check SUPPORTOPS_API_URL.",
        )
