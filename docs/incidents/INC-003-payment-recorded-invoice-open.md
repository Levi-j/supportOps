# INC-003: Payment recorded twice while the invoice stays open

> **Simulated incident.** Reproduced with `supportops-lab start INC-003` in the disposable SupportOps scenario lab. The lab-only `payment_partial_commit` fault deliberately commits a payment before failing to mark the invoice as paid; it is guarded by `BILLING_ENV=lab`. All payment entries are fictional database records. No money moved and no production defect is being claimed.

## Incident details

| Field | Value |

| --- | --- |

| Incident ID | INC-003 |

| Reproduced (UTC) | 2026-10-09 |

| Severity | High — duplicate payment records and an unpaid invoice ([severity guide](../runbooks/triage-and-escalation.md#severity-guide)) |

| Status | Diagnosed; engineering escalation recommended (simulation) |

| Service | billing-api |

| Request IDs | `inc003-cust-01`, `inc003-cust-02` |

| Affected accounts | `acct_juniper` (Juniper Dental Group), invoice `inv_juniper_1003` (INV-1003) |

| Findings | `payment_invoice_inconsistent` and `unhandled_exception` — both confirmed |

## Customer report

> "We tried paying an invoice, received an error, tried again, and now there appear to be two payments while the invoice is still open."

## Expected vs. observed behavior

| | Behavior |

| --- | --- |

| Expected | A successful `POST /v1/invoices/inv_juniper_1003/pay` records one payment, marks the invoice paid, and returns `200`. If processing fails, normal atomic behavior prevents a partial payment update. |

| Observed | Both `inc003-cust-01` and `inc003-cust-02` returned `500 INTERNAL_ERROR` around `2026-10-09T06:00:10Z`. The invoice remained `open` with two successful payment records. |

## Reproduction

```powershell
uv run supportops-lab start INC-003
```

```text
INC-003: Payment recorded twice while the invoice stays open
...
Customer requests sent to the scenario lab at http://127.0.0.1:59582:
  inc003-cust-01  POST /v1/invoices/inv_juniper_1003/pay -> 500 (expected 500)
  inc003-cust-02  POST /v1/invoices/inv_juniper_1003/pay -> 500 (expected 500)
Investigate:
  uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc003-cust-02
```

These writes occur only in the disposable scenario database. `supportops-lab reset`, or starting another scenario, restores a clean seed without touching the persistent billing lab.

## Investigation

### 1. Investigate the second payment attempt

The excerpt below includes the relevant database checks and the final stack-trace frames. Impact and log coverage are covered separately.

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc003-cust-02
```

```text
Investigation of request inc003-cust-02
Logs: docker:supportops-scenario-billing-api-1 (16 lines, 16 entries)
Request: POST /v1/invoices/inv_juniper_1003/pay -> 500 in 12 ms at 2026-10-09T06:00:10.799Z, account acct_juniper
Database: checked as supportops_ro@127.0.0.1:61889/billing at 2026-10-09T06:00:11.600Z
API health: not checked
FINDINGS  Invoice inv_juniper_1003 has inconsistent payment data (confirmed). One more finding follows.
1. Invoice inv_juniper_1003 has inconsistent payment data  [CONFIRMED]
   Invoice inv_juniper_1003 is inconsistent in the database: it has a successful payment but isn't marked paid; it has more than one successful payment. Current record: status open; total 14900 cents; line total 14900 cents; 2 successful and 1 failed payments. This request's logs record payment pay_ad701eaa81e51ae5 for the invoice and no 'invoice.paid' event.
   Evidence: E1, E2, E3, E4
   Interpretation (not verified):
     - The payment was committed, but the invoice wasn't marked as paid. The customer may have been charged while the invoice still shows as unpaid, and a retry could create another payment.
     - The customer was probably charged more than once.
   Next steps:
     - Ask the customer not to retry until it has been confirmed whether the first attempt took effect.
     - Escalate to engineering with the request ID, the invoice ID and the cited evidence; correcting billing data requires a reviewed fix.
     - After any fix, re-check the invoice: supportops db run billing.invoice_lookup --param id=inv_juniper_1003
   Escalate to Engineering (high): Billing data is inconsistent; correcting it needs engineering and probably a code fix.
2. The API failed with an unhandled exception  [CONFIRMED]
   The API failed with an unhandled exception: InjectedFault: Lab fault payment_partial_commit: payment pay_ad701eaa81e51ae5 was committed, then marking the invoice as paid failed. The stack trace is included in the evidence.
   Evidence: E5, E6, E4, E7
   Caveats:
     - The service started with lab fault(s) enabled (payment_partial_commit), which inject failures deliberately.
   Escalate to Engineering (high): An unhandled exception is a defect or an unexpected condition that only engineering can fix.
Evidence
  E1  database check at 2026-10-09T06:00:11.600Z, billing.invoice_lookup
      Invoice inv_juniper_1003 (INV-1003, account acct_juniper): status open, total 14900 EUR cents, sum of lines 14900, successful payments 2, failed payments 1.
  E2  database check at 2026-10-09T06:00:11.600Z, billing.payment_on_unpaid_invoice
      Unpaid invoices with a successful payment: 1. It lists inv_juniper_1003.
  E3  database check at 2026-10-09T06:00:11.600Z, billing.duplicate_payments
      Invoices with more than one successful payment: 1. It lists inv_juniper_1003. Payments: pay_f8af2cbd8c1a907f, pay_ad701eaa81e51ae5.
  E4  log entry at 2026-10-09T06:00:10.797Z, docker:supportops-scenario-billing-api-1:14
      INFO payment.recorded: Payment recorded [account_id=acct_juniper invoice_id=inv_juniper_1003 payment_id=pay_ad701eaa81e51ae5 amount_cents=14900 currency=EUR]
  E5  log entry at 2026-10-09T06:00:10.797Z, docker:supportops-scenario-billing-api-1:15
      ERROR unhandled_exception: Unhandled exception - InjectedFault: Lab fault payment_partial_commit: payment pay_ad701eaa81e51ae5 was committed, then marking the invoice as paid failed.
  E6  log entry at 2026-10-09T06:00:10.799Z, docker:supportops-scenario-billing-api-1:16
      INFO http.request: POST /v1/invoices/inv_juniper_1003/pay -> 500 (12 ms) [account_id=acct_juniper]
  E7  log entry at 2026-10-09T06:00:08.255Z, docker:supportops-scenario-billing-api-1:3
      INFO app.started: Billing API started [environment=lab database_host=postgres ... faults=payment_partial_commit]
Timeline (stack trace trimmed)
       +0 ms  INFO payment.recorded: Payment recorded [... payment_id=pay_ad701eaa81e51ae5 amount_cents=14900 currency=EUR]
       +0 ms  ERROR unhandled_exception: Unhandled exception - InjectedFault: ...
              Traceback (most recent call last):
                ...
                File "/app/.venv/lib/python3.13/site-packages/billing_api/billing.py", line 102, in pay_invoice
                  _record_payment_then_fail(connection, account_id, invoice_id)
                File "/app/.venv/lib/python3.13/site-packages/billing_api/billing.py", line 132, in _record_payment_then_fail
                  raise InjectedFault(
              billing_api.faults.InjectedFault: Lab fault payment_partial_commit: payment pay_ad701eaa81e51ae5 was committed, then marking the invoice as paid failed.
       +2 ms  INFO http.request: POST /v1/invoices/inv_juniper_1003/pay -> 500 (12 ms) [account_id=acct_juniper]
```

### 2. Confirm the first attempt

The trace for `inc003-cust-01` shows that another payment was recorded before the same injected exception:

```text
       +0 ms  INFO     payment.recorded     Payment recorded
              account_id=acct_juniper invoice_id=inv_juniper_1003 payment_id=pay_f8af2cbd8c1a907f amount_cents=14900 currency=EUR
       +1 ms  ERROR    unhandled_exception  Unhandled exception - InjectedFault: Lab fault payment_partial_commit: payment pay_f8af2cbd8c1a907f was committed, then marking the invoice as paid failed.   [exception]
       +5 ms  INFO     http.request         POST /v1/invoices/inv_juniper_1003/pay -> 500 (26 ms)
```

### 3. Verify payment and invoice state

Use the predefined read-only catalog checks:

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env db run billing.invoice_lookup billing.payment_on_unpaid_invoice billing.duplicate_payments --param id=inv_juniper_1003
```

```text
FAIL     billing.payment_on_unpaid_invoice  Unpaid invoices with a successful payment: 1.  (2 ms)
         invoice_id          inv_juniper_1003
         status              open
         total_cents         14900
         succeeded_payments  2
         paid_cents          29800
FAIL     billing.duplicate_payments         Invoices with more than one successful payment: 1.  (2 ms)
         invoice_id          inv_juniper_1003
         payment_ids         pay_f8af2cbd8c1a907f, pay_ad701eaa81e51ae5
         first_payment_at    2026-10-09 06:00:10 UTC
         last_payment_at     2026-10-09 06:00:10 UTC
Result: 2 fail, 1 info
```

The command exits with `1` because two checks found inconsistencies. No records are changed by these queries.

## Evidence

| ID | Source and time (UTC) | Observation |

| --- | --- | --- |

| E1 | Database check `billing.invoice_lookup`, 06:00:11.600Z | `inv_juniper_1003` is `open`, total 14900 EUR cents, 2 successful and 1 failed payments. |

| E2 | Database check `billing.payment_on_unpaid_invoice`, 06:00:11.600Z | Lists `inv_juniper_1003` as unpaid with a successful payment. |

| E3 | Database check `billing.duplicate_payments`, 06:00:11.600Z | Lists `inv_juniper_1003` with payments `pay_f8af2cbd8c1a907f` and `pay_ad701eaa81e51ae5`. |

| E4 | Log entry `:14`, 06:00:10.797Z | `payment.recorded` for `pay_ad701eaa81e51ae5` (request `inc003-cust-02`). |

| E5 | Log entry `:15`, 06:00:10.797Z | `unhandled_exception` `InjectedFault` with stack trace. |

| E6 | Log entry `:16`, 06:00:10.799Z | Access log: `POST /v1/invoices/inv_juniper_1003/pay -> 500`. |

| E7 | Log entry `:3`, 06:00:08.255Z | `app.started` in environment `lab` with `faults=payment_partial_commit`. |

The single failed payment shown by `billing.invoice_lookup` is seeded lab data that predates this scenario. It is distinct from the two successful payment records created during the reproduction.

## Root cause and confidence

**Confirmed:** Both attempts produced a `payment.recorded` event, followed by an `InjectedFault` exception and an HTTP `500` (E4–E6 and the `inc003-cust-01` trace). Neither request recorded an `invoice.paid` event. The database showed invoice `INV-1003` still `open`, with two successful payment records totaling **29,800 EUR cents** against an invoice total of **14,900 EUR cents** (E1–E3). Both payment IDs match the request logs. The API started with `payment_partial_commit` explicitly enabled (E7).

**Cause in this simulation:** The injected fault commits each payment before the invoice update, leaving the invoice open. The second request repeats the partial operation, creating a duplicate successful-payment record. The fault is intentionally confined to the lab; the normal payment path is transactional.

**Confidence: confirmed** for `payment_invoice_inconsistent` and `unhandled_exception`. Request-specific logs corroborate the database state, with no identified contradiction. As with any live database lookup, the SQL results describe the state **at investigation time**.

The evidence does **not** establish any real-world financial charge. These are simulated entries, not transactions with a payment provider.

## Impact

The same injected-exception signature appeared for **two distinct requests**, `inc003-cust-01` and `inc003-cust-02`, from one identified account (`acct_juniper`) within the window `2026-10-09T05:45:10Z` to `06:15:10Z`. It is **recurring within the reproduction**, but log coverage was **partial**; the observed request count is a lower bound for those logs.

At investigation time, the read-only consistency checks identified only `inv_juniper_1003` in the `billing.payment_on_unpaid_invoice` and `billing.duplicate_payments` results. This is the observed state of the disposable lab, not a claim about production exposure.

## Resolution or workaround

**Immediate guidance:** Do not retry payment while its status is unresolved. A failed HTTP response does not prove that the underlying operation was rolled back.

**Engineering action (simulated handoff):** Review both payment records and their relationship to the invoice, determine the appropriate correction, and approve any required data or payment adjustments. Support must not edit billing records or decide on a refund independently. Re-run `billing.invoice_lookup` and the consistency checks after a correction to verify the outcome.

For this disposable simulation, `supportops-lab reset` restores the clean seed; it is not a substitute for investigating or correcting a real billing incident.

## Escalation

**Escalation: Engineering — high severity.** The handoff should include request IDs `inc003-cust-01` and `inc003-cust-02`, invoice `inv_juniper_1003`, account `acct_juniper`, payment IDs `pay_f8af2cbd8c1a907f` and `pay_ad701eaa81e51ae5`, the stack trace, and evidence E1–E7. The requested review covers the inconsistent data, payment atomicity, and retry protection.

## Customer update

> Hello,
>
> Thanks for flagging this. We found that both payment attempts were recorded before the API returned an error, while the invoice still appears unpaid.
>
> Please don't retry the payment while we review its status. We've referred the case to our engineering team to verify the payment records and determine the appropriate correction. We'll share another update by **[agreed UTC time]**, even if the review is still ongoing.
>
> Best regards,
> Support team

*Draft for a simulated customer; no message was sent. This wording is illustrative and must be checked against the evidence before customer use.*

## Prevention and follow-up

| Action | Owner | Status |

| --- | --- | --- |

| Record the payment and mark the invoice paid in one database transaction | Engineering | Proposed |

| Support idempotency keys on payment requests so a retry can't create a second payment | Engineering | Proposed |

| Alert on `billing.payment_on_unpaid_invoice` and `billing.duplicate_payments` findings | Engineering / Support | Proposed |

| Keep the "don't retry until reviewed" guidance in the payment-failure runbook | Support | Ongoing |
