from dataclasses import dataclass
from typing import LiteralString

from supportops.errors import ConfigError

KESTREL_REVOKED_KEY = "bk_kestrel01_lab_only_not_a_real_key"
KESTREL_ROTATED_KEY = "bk_kestrel02_lab_only_not_a_real_key"
JUNIPER_KEY = "bk_juniper01_lab_only_not_a_real_key"
LAB_KEYS = frozenset({KESTREL_REVOKED_KEY, KESTREL_ROTATED_KEY, JUNIPER_KEY})
POWERSHELL_STRIPPED_BODY = b"{name:Acme,email:billing@acme.example}"
VALID_CUSTOMER_BODY = b'{"name":"Acme","email":"billing@acme.example"}'

_ROTATE_KESTREL_KEY: LiteralString = """
UPDATE billing.api_keys
SET revoked_at = now() - interval '2 days'
WHERE key_prefix = 'bk_kestrel01';

INSERT INTO billing.api_keys (id, account_id, key_prefix, key_hash, label, created_at)
SELECT 'key_kestrel_rotated', 'acct_kestrel', left(lab_key, 12),
       encode(sha256(convert_to(lab_key, 'UTF8')), 'hex'), 'Rotated integration key',
       now() - interval '2 days'
FROM (VALUES ('bk_kestrel02_lab_only_not_a_real_key')) AS rotated (lab_key);
"""


@dataclass(frozen=True)
class CustomerRequest:
    request_id: str
    method: str
    path: str
    expected_status: int
    api_key: str | None = None
    body: bytes | None = None
    description: str = ""


@dataclass(frozen=True)
class Expectation:
    request_id: str
    rules: tuple[str, ...]
    confidence: str
    escalation: str | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Scenario:
    id: str
    slug: str
    title: str
    customer_report: str
    simulation: str
    requests: tuple[CustomerRequest, ...]
    expectation: Expectation
    faults: tuple[str, ...] = ()
    setup_sql: LiteralString | None = None

    @property
    def report(self) -> str:
        return f"docs/incidents/{self.id}-{self.slug}.md"


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="INC-001",
        slug="revoked-api-key",
        title="Integration receives 401 after an API key rotation",
        customer_report=(
            "Our integration suddenly started returning 401 responses. We haven't changed our "
            "application."
        ),
        simulation=(
            "The scenario revokes Kestrel Logistics' key bk_kestrel01 two days in the past and "
            "issues a replacement key, then calls the API with the revoked key, as an "
            "integration that was never updated would."
        ),
        setup_sql=_ROTATE_KESTREL_KEY,
        requests=(
            CustomerRequest(
                "inc001-cust-01",
                "GET",
                "/v1/invoices",
                401,
                api_key=KESTREL_REVOKED_KEY,
                description="List invoices with the old key",
            ),
            CustomerRequest(
                "inc001-cust-02",
                "GET",
                "/v1/account",
                401,
                api_key=KESTREL_REVOKED_KEY,
                description="Retry against another endpoint",
            ),
        ),
        expectation=Expectation(
            request_id="inc001-cust-01",
            rules=("key_revoked",),
            confidence="confirmed",
            escalation=None,
            evidence=("auth.rejected", "http.request", "billing.api_key_status"),
        ),
    ),
    Scenario(
        id="INC-002",
        slug="powershell-malformed-json",
        title="Customer creation fails with 400 from a PowerShell script",
        customer_report=(
            "Creating a customer returns HTTP 400 from our PowerShell script, but the JSON looks "
            "valid."
        ),
        simulation=(
            "The scenario sends the exact bytes that Windows PowerShell 5.1 passes to curl.exe "
            "when a JSON string with embedded double quotes is given as an argument: the quotes "
            "are removed. It then sends the same customer as valid JSON, as a file-based fix "
            "would."
        ),
        requests=(
            CustomerRequest(
                "inc002-cust-01",
                "POST",
                "/v1/customers",
                400,
                api_key=JUNIPER_KEY,
                body=POWERSHELL_STRIPPED_BODY,
                description="Body as received from PowerShell 5.1 and curl.exe",
            ),
            CustomerRequest(
                "inc002-cust-02",
                "POST",
                "/v1/customers",
                201,
                api_key=JUNIPER_KEY,
                body=VALID_CUSTOMER_BODY,
                description="Same customer sent from a JSON file",
            ),
        ),
        expectation=Expectation(
            request_id="inc002-cust-01",
            rules=("malformed_json",),
            confidence="confirmed",
            escalation=None,
            evidence=("request.invalid_json", "http.request"),
        ),
    ),
    Scenario(
        id="INC-003",
        slug="payment-recorded-invoice-open",
        title="Payment recorded twice while the invoice stays open",
        customer_report=(
            "We tried paying an invoice, received an error, tried again, and now there appear "
            "to be two payments while the invoice is still open."
        ),
        simulation=(
            "The scenario starts the billing API with the lab-only fault payment_partial_commit, "
            "which commits a payment and then fails before marking the invoice as paid. The "
            "customer pays invoice inv_juniper_1003, receives a 500, and retries once. All "
            "payments are simulated database records; no money moves."
        ),
        faults=("payment_partial_commit",),
        requests=(
            CustomerRequest(
                "inc003-cust-01",
                "POST",
                "/v1/invoices/inv_juniper_1003/pay",
                500,
                api_key=JUNIPER_KEY,
                description="First payment attempt",
            ),
            CustomerRequest(
                "inc003-cust-02",
                "POST",
                "/v1/invoices/inv_juniper_1003/pay",
                500,
                api_key=JUNIPER_KEY,
                description="Customer retries after the error",
            ),
        ),
        expectation=Expectation(
            request_id="inc003-cust-02",
            rules=("payment_invoice_inconsistent", "unhandled_exception"),
            confidence="confirmed",
            escalation="Engineering",
            evidence=(
                "billing.invoice_lookup",
                "billing.payment_on_unpaid_invoice",
                "billing.duplicate_payments",
                "payment.recorded",
                "unhandled_exception",
            ),
        ),
    ),
)


def get_scenario(text: str) -> Scenario:
    wanted = text.strip().lower()
    for scenario in SCENARIOS:
        if wanted in (scenario.id.lower(), scenario.slug):
            return scenario
    raise ConfigError(
        f"Unknown scenario '{text}'.",
        hint="Available scenarios: "
        + ", ".join(f"{item.id} ({item.slug})" for item in SCENARIOS)
        + ". List them with 'supportops-lab scenarios'.",
    )
