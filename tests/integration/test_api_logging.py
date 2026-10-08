import pytest
from fastapi.testclient import TestClient

from tests.integration.support import JUNIPER_KEY, JUNIPER_REVOKED_KEY, bearer
from tests.log_capture import JsonCapture

pytestmark = pytest.mark.integration

JUNIPER = bearer(JUNIPER_KEY)
NEW_EMAIL = "private.person@lakeside-dental.example"
BODY_MARKER = "body-marker-7f3a"
INVALID_VALUE = "invalid-value-9c2e"


def test_logs_never_contain_keys_headers_bodies_or_emails(
    client: TestClient, logs: JsonCapture
) -> None:
    client.get("/v1/account", headers=JUNIPER)
    client.get("/v1/account", headers=bearer(JUNIPER_REVOKED_KEY))
    client.get("/v1/customers", headers=JUNIPER)
    client.post("/v1/customers", headers=JUNIPER, json={"name": "Private", "email": NEW_EMAIL})
    client.post(
        "/v1/customers",
        headers={**JUNIPER, "Content-Type": "application/json"},
        content=f"{{name:{BODY_MARKER}}}".encode(),
    )
    client.post("/v1/customers", headers=JUNIPER, json={"name": "X", "email": INVALID_VALUE})
    client.post("/v1/invoices/inv_juniper_1003/pay", headers=JUNIPER)

    logged = str(logs.service_entries())
    for secret in (
        JUNIPER_KEY,
        JUNIPER_REVOKED_KEY,
        "Bearer ",
        "Authorization",
        NEW_EMAIL,
        "ap@juniper-dental.example",
        BODY_MARKER,
        INVALID_VALUE,
    ):
        assert secret not in logged, secret
    assert len(logs.events("http.request")) == 7
