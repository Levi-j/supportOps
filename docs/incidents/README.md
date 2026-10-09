# Incident reports

These five reports document simulated support incidents against the SupportOps billing API. Each starts with a customer-reported symptom, reproduces the failure in an isolated lab, and works through the available HTTP, log, and PostgreSQL evidence to reach a diagnosis or engineering handoff.

All accounts, credentials, invoices, and payments are fictional. These are **reproducible exercises, not production incidents**. The reports include excerpts from actual scenario runs, with the original request IDs and timestamps. Each one distinguishes the facts observed during the request from later database checks and any conclusions that still require verification.

## Incident index

| Incident | Reported issue | Finding | Severity | Next owner |
| --- | --- | --- | --- | --- |
| [INC-001 — Revoked API key](INC-001-revoked-api-key.md) | An integration unexpectedly starts receiving `401` responses. | `key_revoked` | Low | Integration owner, supported by the support team |
| [INC-002 — Malformed JSON in PowerShell](INC-002-powershell-malformed-json.md) | A customer-creation request returns `400` from a PowerShell script. | `malformed_json` | Low | Integration owner, supported by the support team |
| [INC-003 — Payment records on an open invoice](INC-003-payment-recorded-invoice-open.md) | Two payment requests fail, but payment records exist and the invoice remains open. | `payment_invoice_inconsistent` and `unhandled_exception` | High | Engineering |
| [INC-004 — Incorrect database host](INC-004-db-misconfigured.md) | API requests return `503` after maintenance, although the service is running. | `api_cannot_reach_database` | High | Deployment owner / on-call |
| [INC-005 — Payment blocked by an open transaction](INC-005-blocked-writes.md) | Payments time out with `503 DATABASE_BUSY` while invoice reads continue to work. | `lock_contention` | High | Engineering / DBA |

The investigations returned **confirmed** findings for the reproduced requests. That label applies to the specific failure established by the request logs and corroborating evidence; it does not mean every suggested root cause or every measure of customer impact has been independently verified.

For example, INC-004 confirms that the API could not connect to its database. Its startup configuration then supports the explanation that `localhost` referred to the API container rather than PostgreSQL. In INC-005, a lock-wait snapshot captured during the payment request identifies the blocking session. The report also explains why a later database check no longer shows the waiting connection.

## Reproducing an incident

With Docker running, use the commands below from the repository root:

```powershell
uv run supportops-lab start INC-001
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc001-cust-01
uv run supportops-lab reset
```

`start` recreates the **disposable scenario lab**, applies the selected failure, and sends predefined requests with recognizable IDs. The scenarios use their own Docker Compose project, temporary database, dynamically assigned ports, and generated configuration. They do not reset or change the persistent `supportops` lab.

Two scenarios introduce infrastructure-level failures. INC-004 recreates the disposable billing API with an incorrect database hostname; INC-005 holds an invoice row lock open in a PostgreSQL session. `supportops-lab reset` removes either condition by recreating the disposable environment and checking service health, data consistency, and database session activity.

To run the automated incident tests:

```powershell
uv run pytest -m e2e
```

The E2E suite uses a separate, test-owned scenario project. It checks the expected API responses, diagnostic findings, confidence levels, escalations, and recovery to a clean baseline.

For new investigations, start with the [incident template](TEMPLATE.md). The [triage and escalation runbook](../runbooks/triage-and-escalation.md) covers severity assessment, engineering handoffs, and customer-safe communication.
