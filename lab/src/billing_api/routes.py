from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response

from billing_api import billing, repository
from billing_api.auth import CurrentAccount
from billing_api.dependencies import DbConnection, Settings
from billing_api.errors import not_found
from billing_api.schemas import (
    AccountResponse,
    ApiKeyInfo,
    Customer,
    CustomerCreate,
    CustomerList,
    Invoice,
    InvoiceCreate,
    InvoiceList,
    InvoiceStatus,
    PaymentResult,
    Problem,
)

ResourceId = Annotated[str, Path(min_length=1, max_length=100)]
Limit = Annotated[int, Query(ge=1, le=100, description="Maximum number of results.")]
Offset = Annotated[int, Query(ge=0, le=10_000, description="Number of results to skip.")]

_COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": Problem, "description": "Missing or invalid API key"},
    403: {"model": Problem, "description": "The account is suspended"},
    422: {"model": Problem, "description": "Missing or invalid fields"},
    503: {"model": Problem, "description": "The database is unavailable or busy"},
}

router = APIRouter(prefix="/v1", responses=_COMMON_ERRORS)


@router.get("/account", tags=["account"], summary="Show the account that owns the API key")
def get_account(account: CurrentAccount) -> AccountResponse:
    return AccountResponse(
        id=account.account_id,
        name=account.account_name,
        api_key=ApiKeyInfo(
            prefix=account.key_prefix,
            label=account.key_label,
            created_at=account.key_created_at,
        ),
    )


@router.get("/customers", tags=["customers"], summary="List customers")
def list_customers(
    account: CurrentAccount, connection: DbConnection, limit: Limit = 20, offset: Offset = 0
) -> CustomerList:
    customers = repository.list_customers(connection, account.account_id, limit + 1, offset)
    return CustomerList(data=customers[:limit], has_more=len(customers) > limit)


@router.post("/customers", tags=["customers"], summary="Create a customer", status_code=201)
def create_customer(
    request: CustomerCreate, account: CurrentAccount, connection: DbConnection, response: Response
) -> Customer:
    customer = billing.create_customer(connection, account.account_id, request)
    response.headers["Location"] = f"/v1/customers/{customer.id}"
    return customer


@router.get(
    "/customers/{customer_id}",
    tags=["customers"],
    summary="Get a customer",
    responses={404: {"model": Problem, "description": "No such customer in this account"}},
)
def get_customer(
    customer_id: ResourceId, account: CurrentAccount, connection: DbConnection
) -> Customer:
    customer = repository.get_customer(connection, account.account_id, customer_id)
    if customer is None:
        raise not_found("Customer", customer_id)
    return customer


@router.get("/invoices", tags=["invoices"], summary="List invoices")
def list_invoices(
    account: CurrentAccount,
    connection: DbConnection,
    status: InvoiceStatus | None = None,
    customer_id: Annotated[str | None, Query(max_length=100)] = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> InvoiceList:
    invoices = repository.list_invoices(
        connection, account.account_id, status, customer_id, limit + 1, offset
    )
    return InvoiceList(data=invoices[:limit], has_more=len(invoices) > limit)


@router.post(
    "/invoices",
    tags=["invoices"],
    summary="Create an invoice; the total is calculated from its lines",
    status_code=201,
)
def create_invoice(
    request: InvoiceCreate, account: CurrentAccount, connection: DbConnection, response: Response
) -> Invoice:
    invoice = billing.create_invoice(connection, account.account_id, request)
    response.headers["Location"] = f"/v1/invoices/{invoice.id}"
    return invoice


@router.get(
    "/invoices/{invoice_id}",
    tags=["invoices"],
    summary="Get an invoice with its lines",
    responses={404: {"model": Problem, "description": "No such invoice in this account"}},
)
def get_invoice(
    invoice_id: ResourceId, account: CurrentAccount, connection: DbConnection
) -> Invoice:
    return billing.get_invoice(connection, account.account_id, invoice_id)


@router.post(
    "/invoices/{invoice_id}/pay",
    tags=["invoices"],
    summary="Pay an open invoice in full",
    responses={
        404: {"model": Problem, "description": "No such invoice in this account"},
        409: {"model": Problem, "description": "The invoice is not open"},
    },
)
def pay_invoice(
    invoice_id: ResourceId, account: CurrentAccount, connection: DbConnection, settings: Settings
) -> PaymentResult:
    return billing.pay_invoice(connection, account.account_id, invoice_id, settings.faults)
