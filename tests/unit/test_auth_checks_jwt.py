import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner, Result

from supportops.cli import auth as auth_cli
from supportops.cli.main import app
from supportops.http_checks import create_client
from supportops.settings import Settings
from tests.unit.fake_api import FakeApi, respond

ORDERFLOW_ENV = {"SUPPORTOPS_TARGET": "orderflow", "SUPPORTOPS_API_URL": "http://127.0.0.1:8080"}
EMAIL = "pat.customer@example.com"
SIGNATURE = "dGVzdC1zaWduYXR1cmUtbm90LXJlYWwtYXQtYWxs"
INVALID = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
UNAUTHENTICATED = {"status": 401, "code": "UNAUTHENTICATED", "requestId": "x"}

runner = CliRunner()


def segment(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def make_token(
    *, role: str = "CUSTOMER", age: timedelta = timedelta(minutes=5), **extra: Any
) -> str:
    issued = int((datetime.now(UTC) - age).timestamp())
    payload = {"iss": "orderflow", "sub": "5", "iat": issued, "exp": issued + 1800, "role": role}
    payload.update(extra)
    return f"{segment({'alg': 'HS256'})}.{segment(payload)}.{SIGNATURE}"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()

    def create_fake_client(settings: Settings) -> httpx.Client:
        return create_client(settings, transport=fake.transport)

    monkeypatch.setattr(auth_cli, "create_client", create_fake_client)
    return fake


def check(token: str | None, *args: str) -> Result:
    env = {"COLUMNS": "200", **ORDERFLOW_ENV}
    if token is not None:
        env["ORDERFLOW_TOKEN"] = token
    return runner.invoke(app, ["auth", "check", "--key-env", "ORDERFLOW_TOKEN", *args], env=env)


def report_of(result: Result) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.stdout)
    return data


def bases(report: dict[str, Any]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for finding in report["findings"]:
        grouped.setdefault(finding["basis"], []).append(finding["text"])
    return grouped


def assert_no_secrets(token: str, result: Result) -> None:
    for part in (token, *token.split(".")):
        assert part not in result.output
    assert EMAIL not in result.output
    assert "example.com" not in result.output


def me(role: str = "CUSTOMER") -> Any:
    return respond(
        200, {"id": 5, "email": EMAIL, "role": role, "createdAt": "2026-10-01T09:00:00Z"}
    )


def test_an_accepted_token_shows_the_user_but_never_the_email(api: FakeApi) -> None:
    token = make_token()
    api.on("GET", "/api/v1/users/me", me())

    result = check(token)

    output = " ".join(result.stdout.split())
    assert result.exit_code == 0, result.output
    assert api.requests[0].headers["Authorization"] == f"Bearer {token}"
    assert "Bearer token check for http://127.0.0.1:8080/" in output
    assert "Format: a decodable JWT (signature not checked)" in output
    assert "identified user 5 with role CUSTOMER" in output
    assert "Token claims (decoded locally, unverified and untrusted)" in output
    assert "unverified and untrusted: only the service can tell" in output
    assert "doesn't store issued tokens" in output
    assert_no_secrets(token, result)


def test_json_output_keeps_claims_apart_from_evidence(api: FakeApi) -> None:
    token = make_token()
    api.on("GET", "/api/v1/users/me", me())

    result = check(token, "--json")

    report = report_of(result)
    grouped = bases(report)
    assert report["token"]["verified"] is False
    assert report["token"]["claims"]["role"] == "CUSTOMER"
    assert report["request"]["body"] is None
    assert report["account"] == {
        "account_id": "5",
        "account_name": None,
        "key_prefix": None,
        "key_label": None,
        "role": "CUSTOMER",
    }
    assert all("claims" not in text for text in grouped.get("evidence", []))
    assert all(text.startswith("The token claims") for text in grouped["unverified_claim"])
    assert_no_secrets(token, result)


def test_an_expired_token_rejected_by_the_api_is_only_a_possible_explanation(
    api: FakeApi,
) -> None:
    token = make_token(age=timedelta(hours=3))
    api.on("GET", "/api/v1/users/me", respond(401, UNAUTHENTICATED, problem=True, headers=INVALID))

    result = check(token, "--json")

    grouped = bases(report_of(result))
    assert result.exit_code == 1
    assert any('error="invalid_token"' in text for text in grouped["evidence"])
    assert any("claims it expired" in text for text in grouped["unverified_claim"])
    [inference] = grouped["inference"]
    assert inference.startswith("If these claims are genuine, the expiry would explain the 401.")
    assert "isn't confirmed" in inference
    assert_no_secrets(token, result)


def test_a_rejected_token_with_plausible_claims_suggests_a_signature_problem(
    api: FakeApi,
) -> None:
    api.on("GET", "/api/v1/users/me", respond(401, UNAUTHENTICATED, problem=True, headers=INVALID))

    result = check(make_token(), "--json")

    [inference] = bases(report_of(result))["inference"]
    assert "don't explain the 401" in inference
    assert "Only the service's owners can confirm" in inference


def test_a_401_without_invalid_token_means_no_usable_token_arrived(api: FakeApi) -> None:
    api.on(
        "GET",
        "/api/v1/users/me",
        respond(401, UNAUTHENTICATED, problem=True, headers={"WWW-Authenticate": "Bearer"}),
    )

    result = check(make_token())

    assert result.exit_code == 1
    assert "saw no usable bearer token" in " ".join(result.stdout.split())


def test_a_forbidden_token(api: FakeApi) -> None:
    api.on("GET", "/api/v1/users/me", respond(403, {"code": "ACCESS_DENIED"}, problem=True))

    result = check(make_token(), "--json")

    assert result.exit_code == 1
    assert report_of(result)["outcome"] == "forbidden"


def test_a_role_that_changed_after_the_token_was_issued(api: FakeApi) -> None:
    api.on("GET", "/api/v1/users/me", me(role="CUSTOMER"))

    result = check(make_token(role="ADMIN"), "--json")

    [inference] = bases(report_of(result))["inference"]
    assert "claims role ADMIN" in inference
    assert "reports role CUSTOMER" in inference
    assert "permissions may lag" in inference


def test_an_unknown_role_claim_predicts_403s(api: FakeApi) -> None:
    api.on("GET", "/api/v1/users/me", me())

    result = check(make_token(role="SUPPORT"), "--json")

    grouped = bases(report_of(result))
    assert any("would grant no role" in text for text in grouped["unverified_claim"])
    assert any("would answer 403" in text for text in grouped["inference"])


def test_without_a_token_nothing_is_sent(api: FakeApi) -> None:
    result = check(None)

    assert result.exit_code == 2
    assert api.requests == []
    assert "No bearer token is set in environment variable ORDERFLOW_TOKEN." in result.stdout


def test_a_token_pasted_with_its_scheme_is_not_sent(api: FakeApi) -> None:
    token = make_token()

    result = check(f"Bearer {token}")

    output = " ".join(result.stdout.split())
    assert result.exit_code == 1
    assert api.requests == []
    assert "starts with 'Bearer '" in output
    assert "wasn't sent because it contains whitespace" in output
    assert_no_secrets(token, result)


def test_a_billing_key_given_to_the_orderflow_target(api: FakeApi) -> None:
    api.on("GET", "/api/v1/users/me", respond(401, UNAUTHENTICATED, problem=True, headers=INVALID))

    result = check("bk_juniper01_lab_only_not_a_real_key")

    assert result.exit_code == 1
    assert "billing lab API key, not a token" in " ".join(result.stdout.split())
    assert "bk_juniper01_lab_only_not_a_real_key" not in result.output


def test_an_unreachable_service(api: FakeApi) -> None:
    error = httpx.ConnectError("refused")
    error.__cause__ = ConnectionRefusedError(10061, "refused")
    api.on("GET", "/api/v1/users/me", error)

    result = check(make_token())

    assert result.exit_code == 3
    assert "supportops health" in result.stdout
