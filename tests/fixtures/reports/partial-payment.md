# Internal investigation draft: request `demo-500-a`

> **Internal investigation draft - not for direct customer distribution.** SupportOps generated this from logs and read-only checks for human review. It may contain sensitive operational details, internal identifiers and information about other accounts. Verify every statement against the cited evidence, and remove internal details before sharing anything with a customer. Nothing here has been sent to anyone.

## Summary

| Field | Details |
| --- | --- |
| Request ID | `demo-500-a` |
| Result | FINDINGS: Invoice inv_juniper_1005 has inconsistent payment data (confirmed). One more finding follows. |
| Request | `POST /v1/invoices/inv_juniper_1005/pay -> 500` (48 ms) |
| Request time (UTC) | `2026-10-08T09:05:00.013Z` |
| Account | `acct_juniper` |
| Invoice | `inv_juniper_1005` |
| Payments | `pay_3f2a9c1e5b7d4a60` |
| Report generated (UTC) | `2026-10-08T12:30:00.000Z` |

## Findings

### 1. Invoice inv_juniper_1005 has inconsistent payment data (confidence: confirmed)

Invoice inv_juniper_1005 is inconsistent in the database: it has a successful payment but isn't marked paid. Current record: status open; total 12000 cents; line total 12000 cents; 1 successful and 0 failed payments. This request's logs record payment pay_3f2a9c1e5b7d4a60 for the invoice and no 'invoice.paid' event.

Evidence: [E1], [E2], [E3]

**Interpretation (not verified)**

- The payment was committed, but the invoice wasn't marked as paid. The customer may have been charged while the invoice still shows as unpaid, and a retry could create another payment.

**Evidence limits**

- Database results show the state when the investigation ran, not at the time of the request.
- billing.payment_on_unpaid_invoice also lists 1 other invoice (inv_juniper_1006); it is not attributed to this request.

### 2. The API failed with an unhandled exception (confidence: confirmed)

The API failed with an unhandled exception: InjectedFault: Lab fault payment_partial_commit: payment pay_3f2a9c1e5b7d4a60 was committed, then marking the invoice as paid failed. The stack trace is included in the evidence.

Evidence: [E4], [E5], [E3]

**Interpretation (not verified)**

- The request had already logged payment.recorded before it failed, so it may have partly succeeded. Retrying it could repeat that effect.

## Timeline

| Offset | Time (UTC) | Log entry |
| --- | --- | --- |
| +0 ms | `2026-10-08T09:05:00.004Z` | INFO payment.recorded: Payment recorded [account_id=acct_juniper invoice_id=inv_juniper_1005 payment_id=pay_3f2a9c1e5b7d4a60 amount_cents=12000 currency=EUR] |
| +7 ms | `2026-10-08T09:05:00.011Z` | ERROR unhandled_exception: Unhandled exception - InjectedFault: Lab fault payment_partial_commit: payment pay_3f2a9c1e5b7d4a60 was committed, then marking the invoice as paid failed. |
| +9 ms | `2026-10-08T09:05:00.013Z` | INFO http.request: POST /v1/invoices/inv_juniper_1005/pay -> 500 (48 ms) [account_id=acct_juniper] |

## Evidence

- **E1** (database check at 2026-10-08T12:30:00.000Z, `billing.invoice_lookup`): Invoice inv_juniper_1005 (INV-1005, account acct_juniper): status open, total 12000 EUR cents, sum of lines 12000, successful payments 1, failed payments 0.
- **E2** (database check at 2026-10-08T12:30:00.000Z, `billing.payment_on_unpaid_invoice`): billing.payment_on_unpaid_invoice summary. It lists inv_juniper_1005.
- **E3** (log entry at 2026-10-08T09:05:00.004Z, `billing-api.jsonl:13`): INFO payment.recorded: Payment recorded [account_id=acct_juniper invoice_id=inv_juniper_1005 payment_id=pay_3f2a9c1e5b7d4a60 amount_cents=12000 currency=EUR]
- **E4** (log entry at 2026-10-08T09:05:00.011Z, `billing-api.jsonl:14`): ERROR unhandled_exception: Unhandled exception - InjectedFault: Lab fault payment_partial_commit: payment pay_3f2a9c1e5b7d4a60 was committed, then marking the invoice as paid failed.

  ````text
  Traceback (most recent call last):
    File "/app/.venv/lib/python3.13/site-packages/billing_api/middleware.py", line 50, in __call__
      await self.app(scope, receive, send_with_status)
    File "/app/.venv/lib/python3.13/site-packages/billing_api/routes.py", line 98, in pay_invoice
      return billing.pay_invoice(connection, account_id, invoice_id, settings.faults)
    File "/app/.venv/lib/python3.13/site-packages/billing_api/billing.py", line 132, in _record_payment_then_fail
      raise InjectedFault(
  billing_api.faults.InjectedFault: Lab fault payment_partial_commit: payment pay_3f2a9c1e5b7d4a60 was committed, then marking the invoice as paid failed.
  ````

- **E5** (log entry at 2026-10-08T09:05:00.013Z, `billing-api.jsonl:15`): INFO http.request: POST /v1/invoices/inv_juniper_1005/pay -> 500 (48 ms) [account_id=acct_juniper]

## Impact

Requests with the same error signature in the logs that were read, counted by distinct request ID rather than by log entry.

| Error signature | Requests | Accounts | Log entries | Scope |
| --- | --- | --- | --- | --- |
| `unhandled_exception: InjectedFault: Lab fault payment_partial_commit: payment <id> was committed, then marking the invoice as paid failed.` | At least 2 (demo-500-a, demo-500-b) | At least 1 (acct_juniper) | At least 2 | Recurring |
| `http.request (500): POST /v1/invoices/<id>/pay` | At least 2 (demo-500-a, demo-500-b) | At least 1 (acct_juniper) | At least 2 | Recurring |

**Window:** from 2026-10-08T08:50:00.004Z to 2026-10-08T09:20:00.004Z.

**Recurring:** Other requests show this error, from at most one known account.

**Coverage:** partial for every signature above, so the counts are lower bounds:

- The logs that were read begin 9 min after the start of the 30-minute impact window.
- The logs that were read end 10 min before the end of the 30-minute impact window; later occurrences aren't counted.

## Recommended next steps

1. Ask the customer not to retry until it has been confirmed whether the first attempt took effect.
2. Escalate to engineering with the request ID, the invoice ID and the cited evidence; correcting billing data requires a reviewed fix.
3. After any fix, re-check the invoice: supportops db run billing.invoice_lookup --param id=inv_juniper_1005
4. Include the request ID, its time and the stack trace in the engineering handoff.

## Escalation

- **Engineering**, high severity
  - Finding 1 (Invoice inv_juniper_1005 has inconsistent payment data): Billing data is inconsistent; correcting it needs engineering and probably a code fix.
  - Finding 2 (The API failed with an unhandled exception): An unhandled exception is a defect or an unexpected condition that only engineering can fix.

## Open questions

The investigation recorded no open questions.

## Sources and limitations

- **Application logs:** billing-api.jsonl; 22 lines read, 22 log entries parsed, 0 skipped.
- **Database:** checked as supportops_ro@127.0.0.1:5433/billing at 2026-10-08T12:30:00.000Z.
- **API health:** not checked.
- **Current state:** Database checks ran at 2026-10-08T12:30:00.000Z. They show the database at that time, not when the request was made.
- **Confidence:** *confirmed* means the request's own server-side log entry names the cause, the logged HTTP status agrees and nothing contradicts it; *likely* means the cause is stated directly but the evidence is incomplete or only linked by time; *possible* means only an indirect pattern was seen. A contradiction lowers a finding by one level.
- **Lab context:** the service reported environment 'lab'. Problems may have been simulated deliberately; this draft doesn't claim a real defect.

*Review this draft before sharing any part of it or using it for a customer-facing decision.*
