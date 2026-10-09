# Billing lab reference

The billing API is a fictional FastAPI service built for practicing real support workflows. It has three business accounts, each with its own customers, invoices, and credentials. This reference covers the API contract, sample data, security rules, and diagnostic events.

For installation, startup, and safe lab management, see the [main README](../README.md#local-lab). With the lab running, browse its interactive API documentation at [http://127.0.0.1:8001/docs](http://127.0.0.1:8001/docs).

## Lab API keys

The database is seeded with these **public, development-only** credentials:

| API key | Account | Status |
| --- | --- | --- |
| `bk_juniper01_lab_only_not_a_real_key` | Juniper Dental Group | Active |
| `bk_juniper00_lab_only_not_a_real_key` | Juniper Dental Group | Revoked |
| `bk_kestrel01_lab_only_not_a_real_key` | Kestrel Logistics | Active |
| `bk_alderfin1_lab_only_not_a_real_key` | Alder & Finch Studio | Account suspended |

The default `.env.example` uses the active Juniper key. These strings are lab fixtures, not credentials for a real system. All `/v1` endpoints expect the key in an HTTP bearer header:

```http
Authorization: Bearer bk_juniper01_lab_only_not_a_real_key
```

For the interactive API documentation, select **Authorize** and supply `Bearer ` followed by an active lab key.

The API stores a 12-character key prefix and a SHA-256 hash, **not the complete key**. Authentication compares hashes in constant time. Missing, malformed, unknown, revoked, and expired keys receive the same `401 UNAUTHENTICATED` response; the specific reason is recorded in a redacted `auth.rejected` log event. A valid key belonging to a suspended account returns `403 ACCOUNT_SUSPENDED`.

SupportOps can investigate the stored prefix, but **a matching prefix does not prove that the entire key is valid**.

## Available endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/v1/account` | Identify the account associated with the key |
| `GET` | `/v1/customers` | List customers, with `limit` and `offset` |
| `POST` | `/v1/customers` | Create a customer |
| `GET` | `/v1/customers/{id}` | Retrieve a customer |
| `GET` | `/v1/invoices` | List invoices, with status, customer, and pagination filters |
| `POST` | `/v1/invoices` | Create an invoice from line items |
| `GET` | `/v1/invoices/{id}` | Retrieve an invoice and its lines |
| `POST` | `/v1/invoices/{id}/pay` | Pay an open invoice in full |

List responses contain a `data` array and a `has_more` flag.

## Make a few requests

### Try a read-only request

Run these examples from the **SupportOps repository root**, with the billing lab running.

**PowerShell:**

```powershell
$key = "bk_juniper01_lab_only_not_a_real_key"
curl.exe -s -H "Authorization: Bearer $key" http://127.0.0.1:8001/v1/account
curl.exe -s -H "Authorization: Bearer $key" "http://127.0.0.1:8001/v1/invoices?status=open"
```

**Linux/macOS:**

```bash
key="bk_juniper01_lab_only_not_a_real_key"
curl -s -H "Authorization: Bearer $key" http://127.0.0.1:8001/v1/account
curl -s -H "Authorization: Bearer $key" "http://127.0.0.1:8001/v1/invoices?status=open"
```

### Create a customer (changes local data)

The [sample request body](examples/new-customer.json) can be used to create a customer. **This writes to the billing database; skip it if you want to preserve your current lab data.** To practice a fault scenario, use the disposable incident lab instead.

**PowerShell:**

```powershell
curl.exe -s -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" `
  --data-binary "@docs/examples/new-customer.json" http://127.0.0.1:8001/v1/customers
```

**Linux/macOS:**

```bash
curl -s -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
  --data-binary "@docs/examples/new-customer.json" http://127.0.0.1:8001/v1/customers
```

JSON files avoid common quoting problems with `curl.exe` in Windows PowerShell 5.1. Additional [example bodies](examples/) include deliberately malformed and invalid requests.

## Keeping customers separate

The API determines the account from the verified credential, not from a client-supplied account ID. Customer and invoice reads are scoped to that account.

For example, a Kestrel key cannot retrieve a Juniper invoice:

```powershell
curl.exe -i -H "Authorization: Bearer bk_kestrel01_lab_only_not_a_real_key" `
  http://127.0.0.1:8001/v1/invoices/inv_juniper_1003
```

The result is `404`, just as for a nonexistent invoice. This avoids disclosing whether another account owns the requested resource. Invoice creation also rejects customer references outside the authenticated account.

## Invoices and payments

Invoice amounts are represented as **integer cents**. The server calculates totals from the submitted line items and rejects client-supplied `total_cents` values; [this invalid example](examples/invoice-with-total.json) demonstrates the rule. New invoices start as `open` and receive an account-scoped number such as `INV-1005`.

Paying an invoice normally records the payment and changes the status to `paid` in **one PostgreSQL transaction**. A partial failure rolls back both changes. The API locks the invoice row during payment processing to prevent concurrent payments from succeeding against the same invoice.

- An invoice that is not payable returns `409 INVOICE_NOT_PAYABLE`.
- A blocked database operation can return `503 DATABASE_BUSY` with `Retry-After`; the default lock timeout is three seconds. Invoice reads can continue while the payment write waits.

The disposable INC-003 scenario deliberately breaks the normal atomic-payment behavior for investigation purposes; see [Reproducing a payment inconsistency](#reproducing-a-broken-payment-lab-only).

## Error responses

Errors use [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457), with content type `application/problem+json`. A typical missing-resource response is:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "Not Found",
  "code": "RESOURCE_NOT_FOUND",
  "request_id": "00e9a6a2-4796-452c-a091-0862aac56c62"
}
```

| HTTP status | Error code | Meaning |
| --- | --- | --- |
| `400` | `MALFORMED_REQUEST` | Body cannot be parsed as JSON |
| `401` | `UNAUTHENTICATED` | Missing or invalid API key |
| `403` | `ACCOUNT_SUSPENDED` | Account is not permitted to make requests |
| `404` | `RESOURCE_NOT_FOUND` | Resource not found or not visible to this account |
| `409` | `INVOICE_NOT_PAYABLE` | Invoice cannot be paid in its current state |
| `422` | `VALIDATION_FAILED` | JSON is valid but fields fail validation |
| `500` | `INTERNAL_ERROR` | Unexpected application error |
| `503` | `SERVICE_UNAVAILABLE` | Required dependency is unavailable |
| `503` | `DATABASE_BUSY` | Database operation timed out, including lock waits |

The distinction between `400` and `422` matters when debugging client integrations: malformed JSON is different from a valid JSON document with invalid fields. Validation errors identify fields without echoing submitted values. Unknown fields are rejected rather than silently ignored.

To reproduce safe validation failures, use the [broken JSON](examples/broken-customer.json) and [missing-email](examples/customer-missing-email.json) fixtures. Their expected responses are `400 MALFORMED_REQUEST` and `422 VALIDATION_FAILED`, respectively; neither request creates a record.

## Following a request through the logs

The API includes `X-Request-Id` in responses and carries that ID through its structured JSON events. Authenticated request logs also include an account identifier for scoped investigations.

| Event | Diagnostic use |
| --- | --- |
| `auth.rejected` | Reason an API key was rejected, with a safe prefix |
| `request.invalid_json` | Parsing failure and position, without the request body |
| `request.validation_failed` | Fields that failed validation |
| `customer.created`, `invoice.created` | Identifiers for newly created records |
| `payment.recorded`, `invoice.paid` | Payment-processing events |
| `payment.rejected` | Invoice could not be paid |
| `db.unavailable` | Database dependency or connection problem |
| `db.lock_timeout`, `db.statement_timeout` | Database operation exceeded its timeout |

A revoked key, for instance, produces `auth.rejected` with a reason such as `revoked_key`, while the client receives only a generic `401`.

From the repository root:

```powershell
docker compose --project-name supportops logs billing-api --tail 50
uv run supportops logs search --event "auth.*" --since 15m
uv run supportops logs trace REQUEST_ID
```

The API does not log complete API keys, Authorization headers, request bodies, or customer email addresses. For tracing and search options, see the [logs and request IDs runbook](runbooks/logs-and-request-ids.md).

## Database users and permissions

The lab separates database administration, application writes, and support diagnostics:

| Role | Purpose | Access |
| --- | --- | --- |
| `lab_admin` | Database initialization | Superuser |
| `billing_app` | Billing API operations | Read, insert, and update billing data; no delete or schema changes |
| `supportops_ro` | SupportOps diagnostics | Read-only billing data and PostgreSQL monitoring views |

The diagnostic role has two independent protections: transactions default to read-only, and table permissions do not allow data changes. The seeded lab contains nine invoices, which you can count without modifying any records:

```powershell
docker compose --project-name supportops exec postgres `
  psql -U supportops_ro -d billing -c "SELECT count(*) FROM billing.invoices;"
```

An attempted `DELETE` is rejected by the read-only transaction setting; disabling that setting does not give the role delete privileges. The role and sample data are defined in `lab/sql/` and initialized when PostgreSQL creates a new database volume.

See the [database diagnostics runbook](runbooks/database-diagnostics.md) for the predefined SQL checks and how to interpret permission and session-visibility limitations.

## Reproducing a broken payment (lab only)

The billing service includes a **lab-only fault** named `payment_partial_commit`. When enabled, it records a payment but fails before marking the invoice paid; another attempt can leave a second payment recorded. This behavior is intentional and does **not** represent normal payment processing. The application refuses to start with that fault outside `BILLING_ENV=lab`.

Use the **disposable** scenario environment, not the persistent billing database:

```powershell
uv run supportops-lab up
uv run supportops-lab start INC-003
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc003-cust-02
```

Read [INC-003: Payment recorded, invoice still open](incidents/INC-003-payment-recorded-invoice-open.md) for the HTTP responses, database evidence, and escalation handoff. The same fault is covered by disposable integration tests:

```bash
uv run pytest -m integration -k fault
```

To tear down the disposable environment when finished, run `uv run supportops-lab down`. Neither this workflow nor its tests require resetting the persistent billing lab.
