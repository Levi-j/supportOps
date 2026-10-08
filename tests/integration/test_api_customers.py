import pytest
from fastapi.testclient import TestClient

from tests.integration.support import JUNIPER_KEY, KESTREL_KEY, LabDatabase, bearer, fetch_all
from tests.log_capture import JsonCapture

pytestmark = pytest.mark.integration

JUNIPER = bearer(JUNIPER_KEY)
KESTREL = bearer(KESTREL_KEY)
NEW_CUSTOMER_EMAIL = "accounts@lakeside-dental.example"


def test_lists_only_the_accounts_own_customers(client: TestClient) -> None:
    response = client.get("/v1/customers", headers=JUNIPER)

    assert response.status_code == 200
    ids = {customer["id"] for customer in response.json()["data"]}
    assert ids == {"cus_juniper_main", "cus_juniper_harbor"}
    assert response.json()["has_more"] is False


def test_limit_reports_more_results(client: TestClient) -> None:
    body = client.get("/v1/customers?limit=1", headers=JUNIPER).json()

    assert len(body["data"]) == 1
    assert body["has_more"] is True


def test_creates_a_customer_in_the_callers_account(
    client: TestClient, billing_db: LabDatabase, logs: JsonCapture
) -> None:
    response = client.post(
        "/v1/customers",
        headers=JUNIPER,
        json={"name": "  Lakeside Dental  ", "email": NEW_CUSTOMER_EMAIL},
    )

    assert response.status_code == 201
    customer = response.json()
    assert customer["id"].startswith("cus_")
    assert customer["name"] == "Lakeside Dental"
    assert response.headers["Location"] == f"/v1/customers/{customer['id']}"
    rows = fetch_all(
        billing_db, "SELECT account_id FROM billing.customers WHERE id = %s", (customer["id"],)
    )
    assert rows == [("acct_juniper",)]
    [created] = logs.events("customer.created")
    assert created["customer_id"] == customer["id"]
    assert created["account_id"] == "acct_juniper"
    assert NEW_CUSTOMER_EMAIL not in str(logs.entries)
    assert "Lakeside" not in str(logs.entries)


def test_gets_an_own_customer(client: TestClient) -> None:
    response = client.get("/v1/customers/cus_juniper_main", headers=JUNIPER)

    assert response.status_code == 200
    assert response.json()["email"] == "ap@juniper-dental.example"


def test_another_tenants_customer_looks_exactly_like_a_missing_one(client: TestClient) -> None:
    foreign = client.get("/v1/customers/cus_juniper_main", headers=KESTREL)
    missing = client.get("/v1/customers/cus_does_not_exist", headers=KESTREL)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"] == "RESOURCE_NOT_FOUND"
    assert foreign.json()["detail"] == "Customer cus_juniper_main not found."
    assert "juniper-dental" not in foreign.text


def test_missing_field_is_a_422_with_the_field_path(client: TestClient, logs: JsonCapture) -> None:
    response = client.post("/v1/customers", headers=JUNIPER, json={"name": "No Email Ltd"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert body["errors"] == [
        {"location": "body", "field": "email", "message": "Field required", "type": "missing"}
    ]
    [failure] = logs.events("request.validation_failed")
    assert failure["fields"] == ["body.email"]


def test_unknown_fields_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/customers",
        headers=JUNIPER,
        json={"name": "Lakeside", "email": NEW_CUSTOMER_EMAIL, "account_id": "acct_kestrel"},
    )

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert (error["field"], error["type"]) == ("account_id", "extra_forbidden")


def test_invalid_values_are_never_echoed(client: TestClient, logs: JsonCapture) -> None:
    response = client.post(
        "/v1/customers", headers=JUNIPER, json={"name": "Lakeside", "email": "secret-not-an-email"}
    )

    assert response.status_code == 422
    assert "secret-not-an-email" not in response.text
    assert "secret-not-an-email" not in str(logs.entries)


def test_body_with_unquoted_property_names_is_a_400(client: TestClient, logs: JsonCapture) -> None:
    body = b"{name:Lakeside Dental,email:accounts@lakeside-dental.example}"

    response = client.post(
        "/v1/customers",
        headers={**JUNIPER, "Content-Type": "application/json"},
        content=body,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_REQUEST"
    [invalid] = logs.events("request.invalid_json")
    assert invalid["error_message"] == "Expecting property name enclosed in double quotes"
    assert invalid["error_position"] == 1
    assert invalid["content_length"] == str(len(body))
    assert "Lakeside" not in str(logs.entries)
