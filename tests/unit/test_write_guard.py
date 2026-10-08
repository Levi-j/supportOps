from dataclasses import replace

import httpx
import pytest

from supportops.errors import ExitCode
from supportops.http_checks import create_client
from supportops.settings import Settings
from supportops.targets import BILLING
from supportops.write_guard import (
    WriteRefused,
    check_write_request,
    confirm_lab_service,
    is_write,
)
from tests.unit.fake_api import FakeApi, lab_health, respond


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_state_changing_methods_are_writes(method: str) -> None:
    assert is_write(method)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_read_methods_are_not_writes(method: str) -> None:
    assert not is_write(method)


def test_writes_need_the_yes_flag() -> None:
    with pytest.raises(WriteRefused, match="needs --yes") as excinfo:
        check_write_request(
            "POST", confirmed=False, api_url="http://127.0.0.1:8001", profile=BILLING
        )

    assert excinfo.value.exit_code == ExitCode.USAGE


@pytest.mark.parametrize(
    "url", ["https://api.example.com", "http://10.0.0.5:8001", "http://billing.internal"]
)
def test_writes_to_non_loopback_hosts_are_refused(url: str) -> None:
    with pytest.raises(WriteRefused, match="only allowed against the local lab"):
        check_write_request("POST", confirmed=True, api_url=url, profile=BILLING)


def test_targets_without_lab_writes_are_refused() -> None:
    read_only_target = replace(BILLING, name="orderflow", lab_writes_allowed=False)

    with pytest.raises(WriteRefused, match="not allowed against the 'orderflow' target"):
        check_write_request(
            "POST", confirmed=True, api_url="http://127.0.0.1:8080", profile=read_only_target
        )


@pytest.mark.parametrize(
    "url", ["http://localhost:8001", "http://127.0.0.1:8001", "http://[::1]:8001"]
)
def test_loopback_hosts_pass_the_offline_checks(url: str) -> None:
    check_write_request("POST", confirmed=True, api_url=url, profile=BILLING)


def lab_client(api: FakeApi) -> httpx.Client:
    return create_client(Settings(), transport=api.transport)


def test_a_service_reporting_lab_is_accepted() -> None:
    api = FakeApi().on("GET", "/health", lab_health("lab"))

    confirm_lab_service(lab_client(api), BILLING)

    assert api.methods == ["GET"]


@pytest.mark.parametrize(
    "responder",
    [
        lab_health("production"),
        respond(200, {"status": "ok"}),
        respond(500, text="boom"),
        httpx.ConnectError("refused"),
    ],
)
def test_anything_else_refuses_the_write(responder: object) -> None:
    api = FakeApi().on("GET", "/health", responder)  # type: ignore[arg-type]

    with pytest.raises(WriteRefused):
        confirm_lab_service(lab_client(api), BILLING)

    assert api.methods == ["GET"]
