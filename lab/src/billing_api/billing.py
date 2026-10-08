import logging
import secrets
from typing import NoReturn

from billing_api import repository
from billing_api.database import Connection
from billing_api.errors import ApiError, not_found, validation_failed
from billing_api.faults import Fault, InjectedFault
from billing_api.repository import LockedInvoice, PaymentRow
from billing_api.schemas import (
    Customer,
    CustomerCreate,
    Invoice,
    InvoiceCreate,
    Payment,
    PaymentResult,
)

logger = logging.getLogger("billing_api.billing")

_NOT_PAYABLE = {
    "draft": "Invoice {id} is a draft and cannot be paid until it is issued.",
    "paid": "Invoice {id} is already paid.",
    "void": "Invoice {id} is void and cannot be paid.",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def create_customer(connection: Connection, account_id: str, request: CustomerCreate) -> Customer:
    customer = repository.insert_customer(
        connection, account_id, new_id("cus"), request.name, request.email
    )
    logger.info(
        "Customer created",
        extra={
            "event_name": "customer.created",
            "account_id": account_id,
            "customer_id": customer.id,
        },
    )
    return customer


def create_invoice(connection: Connection, account_id: str, request: InvoiceCreate) -> Invoice:
    total_cents = sum(line.quantity * line.unit_amount_cents for line in request.lines)
    invoice_id = new_id("inv")
    with connection.transaction():
        if repository.get_customer(connection, account_id, request.customer_id) is None:
            raise validation_failed(
                [
                    {
                        "location": "body",
                        "field": "customer_id",
                        "message": "No customer with this ID exists in this account.",
                        "type": "customer_not_found",
                    }
                ]
            )
        repository.lock_account(connection, account_id)
        number = repository.next_invoice_number(connection, account_id)
        repository.insert_invoice(
            connection,
            invoice_id=invoice_id,
            account_id=account_id,
            customer_id=request.customer_id,
            number=number,
            currency=request.currency,
            due_date=request.due_date,
            total_cents=total_cents,
            lines=request.lines,
        )
    logger.info(
        "Invoice created",
        extra={
            "event_name": "invoice.created",
            "account_id": account_id,
            "invoice_id": invoice_id,
            "invoice_number": number,
            "customer_id": request.customer_id,
            "currency": request.currency,
            "total_cents": total_cents,
            "line_count": len(request.lines),
        },
    )
    return _require_invoice(connection, account_id, invoice_id)


def get_invoice(connection: Connection, account_id: str, invoice_id: str) -> Invoice:
    invoice = repository.get_invoice(connection, account_id, invoice_id)
    if invoice is None:
        raise not_found("Invoice", invoice_id)
    return invoice


def pay_invoice(
    connection: Connection, account_id: str, invoice_id: str, faults: frozenset[Fault]
) -> PaymentResult:
    if Fault.PAYMENT_PARTIAL_COMMIT in faults:
        _record_payment_then_fail(connection, account_id, invoice_id)
    with connection.transaction():
        invoice = _lock_payable_invoice(connection, account_id, invoice_id)
        payment = repository.insert_payment(
            connection, new_id("pay"), account_id, invoice.id, invoice.total_cents
        )
        repository.mark_invoice_paid(connection, account_id, invoice.id)
    _log_payment_recorded(account_id, invoice, payment)
    logger.info(
        "Invoice paid",
        extra={
            "event_name": "invoice.paid",
            "account_id": account_id,
            "invoice_id": invoice.id,
            "payment_id": payment.id,
        },
    )
    return PaymentResult(
        payment=Payment(currency=invoice.currency, **vars(payment)),
        invoice=_require_invoice(connection, account_id, invoice.id),
    )


def _record_payment_then_fail(connection: Connection, account_id: str, invoice_id: str) -> NoReturn:
    with connection.transaction():
        invoice = _lock_payable_invoice(connection, account_id, invoice_id)
        payment = repository.insert_payment(
            connection, new_id("pay"), account_id, invoice.id, invoice.total_cents
        )
    _log_payment_recorded(account_id, invoice, payment)
    raise InjectedFault(
        f"Lab fault {Fault.PAYMENT_PARTIAL_COMMIT}: payment {payment.id} was committed, "
        "then marking the invoice as paid failed."
    )


def _lock_payable_invoice(
    connection: Connection, account_id: str, invoice_id: str
) -> LockedInvoice:
    invoice = repository.lock_invoice(connection, account_id, invoice_id)
    if invoice is None:
        raise not_found("Invoice", invoice_id)
    if invoice.status != "open":
        logger.warning(
            "Payment rejected",
            extra={
                "event_name": "payment.rejected",
                "account_id": account_id,
                "invoice_id": invoice.id,
                "invoice_status": invoice.status,
            },
        )
        raise ApiError(
            409, "INVOICE_NOT_PAYABLE", _NOT_PAYABLE[invoice.status].format(id=invoice.id)
        )
    return invoice


def _log_payment_recorded(account_id: str, invoice: LockedInvoice, payment: PaymentRow) -> None:
    logger.info(
        "Payment recorded",
        extra={
            "event_name": "payment.recorded",
            "account_id": account_id,
            "invoice_id": invoice.id,
            "payment_id": payment.id,
            "amount_cents": payment.amount_cents,
            "currency": invoice.currency,
        },
    )


def _require_invoice(connection: Connection, account_id: str, invoice_id: str) -> Invoice:
    invoice = repository.get_invoice(connection, account_id, invoice_id)
    if invoice is None:
        raise RuntimeError(f"Invoice {invoice_id} disappeared after it was written.")
    return invoice
