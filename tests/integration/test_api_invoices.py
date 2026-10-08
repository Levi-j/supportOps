from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.integration.support import JUNIPER_KEY, KESTREL_KEY, LabDatabase, bearer, fetch_all
from tests.log_capture import JsonCapture

pytestmark = pytest.mark.integration

JUNIPER = bearer(JUNIPER_KEY)
KESTREL = bearer(KESTREL_KEY)


def invoice_request(**overrides: Any) -> dict[str, Any]:
    return {
        "customer_id": "cus_juniper_main",
        "currency": "EUR",
        "lines": [
            {"description": "Practice management plan", "quantity": 1, "unit_amount_cents": 4900},
            {"description": "Additional staff seats", "quantity": 2, "unit_amount_cents": 1500},
        ],
        **overrides,
    }


def test_creates_an_open_invoice_with_a_server_calculated_total(
    client: TestClient, billing_db: LabDatabase, logs: JsonCapture
) -> None:
    response = client.post("/v1/invoices", headers=JUNIPER, json=invoice_request())

    assert response.status_code == 201
    invoice = response.json()
    assert invoice["status"] == "open"
    assert invoice["number"] == "INV-1005"
    assert invoice["total_cents"] == 7900
    assert [line["amount_cents"] for line in invoice["lines"]] == [4900, 3000]
    assert invoice["due_date"]
    assert response.headers["Location"] == f"/v1/invoices/{invoice['id']}"
    rows = fetch_all(
        billing_db,
        "SELECT account_id, total_cents FROM billing.invoices WHERE id = %s",
        (invoice["id"],),
    )
    assert rows == [("acct_juniper", 7900)]
    [created] = logs.events("invoice.created")
    assert created["total_cents"] == 7900
    assert created["line_count"] == 2


def test_invoice_numbers_continue_per_account(client: TestClient) -> None:
    first = client.post("/v1/invoices", headers=JUNIPER, json=invoice_request()).json()
    second = client.post("/v1/invoices", headers=JUNIPER, json=invoice_request()).json()
    kestrel = client.post(
        "/v1/invoices",
        headers=KESTREL,
        json=invoice_request(customer_id="cus_kestrel_ops", currency="USD"),
    ).json()

    assert [first["number"], second["number"], kestrel["number"]] == [
        "INV-1005",
        "INV-1006",
        "INV-2005",
    ]


def test_explicit_due_date_is_kept(client: TestClient) -> None:
    response = client.post(
        "/v1/invoices", headers=JUNIPER, json=invoice_request(due_date="2030-01-31")
    )

    assert response.json()["due_date"] == "2030-01-31"


def test_client_cannot_set_the_total(client: TestClient) -> None:
    response = client.post("/v1/invoices", headers=JUNIPER, json=invoice_request(total_cents=1))

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert (error["field"], error["type"]) == ("total_cents", "extra_forbidden")


@pytest.mark.parametrize(
    ("overrides", "field", "error_type"),
    [
        ({"lines": []}, "lines", "too_short"),
        (
            {"lines": [{"description": "Seat", "quantity": 0, "unit_amount_cents": 100}]},
            "lines.0.quantity",
            "greater_than_equal",
        ),
        (
            {"lines": [{"description": "Seat", "quantity": 1, "unit_amount_cents": -5}]},
            "lines.0.unit_amount_cents",
            "greater_than_equal",
        ),
        ({"currency": "JPY"}, "currency", "literal_error"),
    ],
)
def test_invalid_invoices_are_rejected_with_field_paths(
    client: TestClient, overrides: dict[str, Any], field: str, error_type: str
) -> None:
    response = client.post("/v1/invoices", headers=JUNIPER, json=invoice_request(**overrides))

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert (error["field"], error["type"]) == (field, error_type)


def test_customer_from_another_account_is_rejected(
    client: TestClient, billing_db: LabDatabase
) -> None:
    response = client.post(
        "/v1/invoices",
        headers=KESTREL,
        json=invoice_request(customer_id="cus_juniper_main", currency="USD"),
    )

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert (error["field"], error["type"]) == ("customer_id", "customer_not_found")
    assert fetch_all(
        billing_db, "SELECT count(*) FROM billing.invoices WHERE account_id = 'acct_kestrel'"
    ) == [(4,)]


def test_lists_only_own_invoices_with_filters(client: TestClient) -> None:
    all_invoices = client.get("/v1/invoices", headers=JUNIPER).json()["data"]
    open_invoices = client.get("/v1/invoices?status=open", headers=JUNIPER).json()["data"]
    harbor = client.get("/v1/invoices?customer_id=cus_juniper_harbor", headers=JUNIPER).json()

    assert {invoice["number"] for invoice in all_invoices} == {
        "INV-1001",
        "INV-1002",
        "INV-1003",
        "INV-1004",
    }
    assert [invoice["number"] for invoice in open_invoices] == ["INV-1003"]
    assert {invoice["number"] for invoice in harbor["data"]} == {"INV-1002", "INV-1004"}


def test_invalid_status_filter_is_a_422(client: TestClient) -> None:
    response = client.get("/v1/invoices?status=overdue", headers=JUNIPER)

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert (error["location"], error["field"]) == ("query", "status")


def test_gets_an_invoice_with_its_lines(client: TestClient) -> None:
    response = client.get("/v1/invoices/inv_juniper_1003", headers=JUNIPER)

    assert response.status_code == 200
    invoice = response.json()
    assert invoice["total_cents"] == 14900
    assert sum(line["amount_cents"] for line in invoice["lines"]) == 14900


def test_another_tenants_invoice_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/invoices/inv_juniper_1003", headers=KESTREL)

    assert response.status_code == 404
    assert response.json()["detail"] == "Invoice inv_juniper_1003 not found."
