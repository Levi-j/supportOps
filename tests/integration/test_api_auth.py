import pytest
from fastapi.testclient import TestClient

from billing_api.auth import hash_api_key
from tests.integration.support import (
    JUNIPER_KEY,
    JUNIPER_REVOKED_KEY,
    SUSPENDED_ALDER_KEY,
    LabDatabase,
    bearer,
    execute,
)
from tests.log_capture import JsonCapture

pytestmark = pytest.mark.integration

GENERIC_401 = {
    "type": "about:blank",
    "title": "Unauthorized",
    "status": 401,
    "detail": "Missing or invalid API key.",
    "code": "UNAUTHENTICATED",
}
EXPIRED_KEY = "bk_juniper02_lab_only_expired_key"


def assert_generic_401(response_body: dict[str, object], request_id: str) -> None:
    body = dict(response_body)
    assert body.pop("request_id") == request_id
    assert body == GENERIC_401


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({}, "missing_header"),
        ({"Authorization": "Basic dXNlcjpwYXNzd29yZA=="}, "malformed_header"),
        ({"Authorization": "Bearer"}, "malformed_header"),
        ({"Authorization": "Bearer not-a-billing-key"}, "malformed_header"),
        (bearer("bk_" + "z" * 30), "unknown_key"),
        (bearer(JUNIPER_KEY[:-4]), "unknown_key"),
        (bearer(JUNIPER_REVOKED_KEY), "revoked_key"),
    ],
)
def test_every_rejected_key_gets_the_same_401(
    client: TestClient, logs: JsonCapture, headers: dict[str, str], reason: str
) -> None:
    response = client.get("/v1/account", headers={**headers, "X-Request-Id": "auth-matrix"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="billing-api"'
    assert response.headers["content-type"] == "application/problem+json"
    assert_generic_401(response.json(), "auth-matrix")
    [rejection] = logs.events("auth.rejected")
    assert rejection["reason"] == reason
    assert rejection["request_id"] == "auth-matrix"


def test_the_log_identifies_a_known_key_only_by_its_prefix(
    client: TestClient, logs: JsonCapture
) -> None:
    client.get("/v1/account", headers=bearer(JUNIPER_REVOKED_KEY))

    [rejection] = logs.events("auth.rejected")
    assert rejection["key_prefix"] == "bk_juniper00"
    assert rejection["account_id"] == "acct_juniper"
    assert JUNIPER_REVOKED_KEY not in str(logs.entries)


def test_expired_key_is_rejected(
    client: TestClient, billing_db: LabDatabase, logs: JsonCapture
) -> None:
    execute(
        billing_db,
        """
        INSERT INTO billing.api_keys
            (id, account_id, key_prefix, key_hash, label, created_at, expires_at)
        VALUES ('key_juniper_expired', 'acct_juniper', %s, %s, 'Temporary key',
                now() - interval '40 days', now() - interval '10 days')
        """,
        (EXPIRED_KEY[:12], hash_api_key(EXPIRED_KEY)),
    )

    response = client.get("/v1/account", headers={**bearer(EXPIRED_KEY), "X-Request-Id": "expired"})

    assert response.status_code == 401
    assert_generic_401(response.json(), "expired")
    [rejection] = logs.events("auth.rejected")
    assert rejection["reason"] == "expired_key"


def test_suspended_account_gets_403(client: TestClient, logs: JsonCapture) -> None:
    response = client.get("/v1/account", headers=bearer(SUSPENDED_ALDER_KEY))

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "ACCOUNT_SUSPENDED"
    assert "WWW-Authenticate" not in response.headers
    [rejection] = logs.events("auth.rejected")
    assert rejection["reason"] == "account_suspended"
    assert rejection["account_id"] == "acct_alder"


def test_valid_key_identifies_the_account(client: TestClient, logs: JsonCapture) -> None:
    response = client.get("/v1/account", headers=bearer(JUNIPER_KEY))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "acct_juniper"
    assert body["name"] == "Juniper Dental Group"
    assert body["api_key"]["prefix"] == "bk_juniper01"
    assert body["api_key"]["label"] == "Practice software"
    assert JUNIPER_KEY not in response.text
    [access] = logs.events("http.request")
    assert access["account_id"] == "acct_juniper"
    assert logs.events("auth.rejected") == []


def test_lowercase_bearer_scheme_is_accepted(client: TestClient) -> None:
    response = client.get("/v1/account", headers={"Authorization": f"bearer {JUNIPER_KEY}"})

    assert response.status_code == 200
