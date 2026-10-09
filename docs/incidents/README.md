# Incident reports

These reports document three reproducible troubleshooting cases in the **disposable SupportOps scenario lab**. Each follows a customer-style complaint through reproduction, request tracing, evidence gathering, diagnosis, and a support or engineering response.

The incidents are **simulated**, not production cases. Accounts, credentials, invoices, and payment records are fictional. The investigation excerpts are drawn from actual scenario runs and are identified with their original request IDs and timestamps.

## Incident index

| Incident | Reported problem | Confirmed finding | Severity | Recommended owner |
| --- | --- | --- | --- | --- |
| [INC-001 — Revoked API key](INC-001-revoked-api-key.md) | Integration starts receiving `401` responses. | `key_revoked` | Low | Customer / integration owner, with support guidance |
| [INC-002 — PowerShell JSON formatting](INC-002-powershell-malformed-json.md) | Creating a customer fails with `400` from a PowerShell script. | `malformed_json` | Low | Customer / integration owner, with support guidance |
| [INC-003 — Duplicate payment records](INC-003-payment-recorded-invoice-open.md) | Two payment attempts fail while the invoice remains open. | `payment_invoice_inconsistent` and `unhandled_exception` | High | Engineering |

Each finding above was reported with **confirmed** confidence for the reproduced request. The reports separately identify unverified interpretations and any limitations in log coverage.

## Reproduce and investigate a case

From the repository root, with Docker running:

```powershell
uv run supportops-lab start INC-001
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc001-cust-01
uv run supportops-lab reset
```

`start` recreates the **scenario lab only** and replays the selected incident with predictable request IDs. Its Compose project, temporary database, ports, and configuration are separate from the persistent `supportops` lab. The reset command clears the disposable scenario data; it does not reset your regular billing database.

To verify the complete workflow automatically:

```powershell
uv run pytest -m e2e
```

The E2E suite uses its own isolated scenario project and checks the observed responses, investigation findings, confidence levels, escalation decisions, and clean reset behavior.

For future reports, use the [incident template](TEMPLATE.md). The [triage and escalation runbook](../runbooks/triage-and-escalation.md) explains how to assess severity, hand evidence to engineering, and prepare customer-safe updates.
