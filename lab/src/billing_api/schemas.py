from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

InvoiceStatus = Literal["draft", "open", "paid", "void"]
Currency = Literal["EUR", "USD", "GBP"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FieldProblem(BaseModel):
    location: str
    field: str
    message: str
    type: str


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str
    request_id: str | None = None
    errors: list[FieldProblem] | None = None


class ApiKeyInfo(BaseModel):
    prefix: str
    label: str
    created_at: datetime


class AccountResponse(BaseModel):
    id: str
    name: str
    api_key: ApiKeyInfo


class CustomerCreate(RequestModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Customer(BaseModel):
    id: str
    name: str
    email: str
    created_at: datetime


class CustomerList(BaseModel):
    data: list[Customer]
    has_more: bool


class InvoiceLineCreate(RequestModel):
    description: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1, le=10_000)
    unit_amount_cents: int = Field(ge=1, le=10_000_000)


class InvoiceCreate(RequestModel):
    customer_id: str = Field(min_length=1, max_length=100)
    currency: Currency
    due_date: date | None = None
    lines: list[InvoiceLineCreate] = Field(min_length=1, max_length=50)


class InvoiceLine(BaseModel):
    description: str
    quantity: int
    unit_amount_cents: int
    amount_cents: int


class InvoiceSummary(BaseModel):
    id: str
    number: str
    customer_id: str
    status: InvoiceStatus
    currency: str
    total_cents: int
    due_date: date
    created_at: datetime
    paid_at: datetime | None


class Invoice(InvoiceSummary):
    updated_at: datetime
    lines: list[InvoiceLine]


class InvoiceList(BaseModel):
    data: list[InvoiceSummary]
    has_more: bool


class Payment(BaseModel):
    id: str
    invoice_id: str
    amount_cents: int
    currency: str
    status: Literal["succeeded", "failed"]
    created_at: datetime


class PaymentResult(BaseModel):
    payment: Payment
    invoice: Invoice
