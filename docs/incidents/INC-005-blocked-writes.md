# INC-005: Invoice payments blocked by an open database transaction

> **Simulated incident.** Reproduced in the disposable SupportOps scenario lab on 2026-10-09. The harness started a PostgreSQL session named `invoice-backfill`, locked a fictional invoice row with `SELECT ... FOR UPDATE`, and left the transaction open. No production system or real payment was involved.

## Incident details

| Field | Detail |
| --- | --- |
| Incident | INC-005 |
| Date reproduced (UTC) | 2026-10-09 |
| Service | Billing API / PostgreSQL |
| Severity | High — invoice payments blocked by an open transaction; see the [severity guide](../runbooks/triage-and-escalation.md#severity-guide) |
| Status | Diagnosed; simulated Engineering / DBA handoff |
| Request IDs | `inc005-cust-01` (payment), `inc005-cust-02` (read), `inc005-cust-03` (retry) |
| Simulated customer | Kestrel Logistics (`acct_kestrel`) |
| Invoice | `inv_kestrel_2002` (`INV-2002`) |
| Finding | `lock_contention` — confirmed |
| Escalation | Engineering / DBA |

## Customer report

> "Paying an invoice keeps timing out with a database-busy error, but we can still view our invoices."

The ability to read an invoice while payment writes fail is an important clue. It suggests the API and PostgreSQL are reachable, but a particular database operation may be unable to proceed.

## Expected versus observed behavior

| Operation | Expected | Observed |
| --- | --- | --- |
| `POST /v1/invoices/inv_kestrel_2002/pay` | Record one payment, mark the invoice paid, and return `200` | `503 DATABASE_BUSY` after approximately 3 seconds, with `Retry-After: 5` |
| `GET /v1/invoices/inv_kestrel_2002` | Return the invoice | `200` while the payment was blocked |
| Retry of the payment | No repeated failure once the cause is resolved | A second `503 DATABASE_BUSY` while the same lock remained |
| Invoice state after the failed attempts | Payment and status reflect a successful transaction | Invoice remained `open`, with 0 successful payments recorded |

The first payment response was logged at `2026-10-09T07:46:36.015Z`. The API's lock timeout was configured for 3,000 ms; the observed request took 3,023 ms.

## Reproduction

From the repository root, with Docker running:

```powershell
uv run supportops-lab start INC-005
```

The harness first verifies a healthy disposable baseline. It then starts the lock-holder session inside the **verified scenario PostgreSQL container**. It waits until that session is visibly `idle in transaction` with locks held before sending the customer's requests.

Selected output from the original run:

```text
INC-005: Invoice payments time out while a backfill holds a row lock
...
Customer requests sent to the scenario lab at http://127.0.0.1:57201:
  inc005-cust-01  POST /v1/invoices/inv_kestrel_2002/pay -> 503 DATABASE_BUSY, Retry-After 5 (expected 503 DATABASE_BUSY)
  inc005-cust-02  GET /v1/invoices/inv_kestrel_2002 -> 200 (expected 200)
  inc005-cust-03  POST /v1/invoices/inv_kestrel_2002/pay -> 503 DATABASE_BUSY, Retry-After 5 (expected 503 DATABASE_BUSY)
Lock wait captured at 2026-10-09T07:46:33.143+00:00 with pg.blocking_sessions while inc005-cust-01 was waiting:
  session 183 (billing-api, billing_app) waiting 0 s for Lock:transactionid, blocked by session 113 (invoice-backfill, idle in transaction, transaction open 11 s)
Investigate:
  uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc005-cust-01
  uv run supportops --env-file .lab\supportops-scenario\supportops.env db run pg.long_transactions pg.blocking_sessions
Recover with 'uv run supportops-lab reset'; it recreates only the scenario lab.
```

The response to the read request matters: the API can still retrieve the invoice while the payment is blocked. The lock-wait snapshot is equally important. The harness captured session 183 waiting on session 113 **while the first payment request was in progress**.

Docker-assigned ports and PostgreSQL session IDs are specific to this run and will differ on a fresh reproduction.

### How the scenario holds the lock

The simulated backfill runs a fixed, predefined query:

```sql
BEGIN;
SELECT id, status
FROM billing.invoices
WHERE id = 'inv_kestrel_2002'
FOR UPDATE;
```

It deliberately leaves the transaction open without committing or rolling back. The session's `application_name` is `invoice-backfill`, making it identifiable in PostgreSQL activity views. The query itself changes no invoice data.

Before customer traffic begins, the harness checks that the holder exists, is `idle in transaction`, has locks, and has passed the investigation's 10-second long-transaction threshold. During the first payment, it queries `pg.blocking_sessions` and accepts a capture only when the waiting billing API connection is blocked by that same backfill session.

If the expected wait cannot be observed, the scenario reports an error rather than manufacturing evidence. The holder remains in the disposable database until the scenario is reset or removed; the normal SupportOps CLI never terminates it.

## Investigation

### 1. Trace the timed-out payment

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc005-cust-01
```

The investigation output below contains the original log evidence and later database observations:

```text
Investigation of request inc005-cust-01
Logs: docker:supportops-scenario-billing-api-1 (26 lines, 26 entries)
Request: POST /v1/invoices/inv_kestrel_2002/pay -> 503 in 3,023 ms at 2026-10-09T07:46:36.015Z, account acct_kestrel
Database: checked as supportops_ro@127.0.0.1:57200/billing at 2026-10-09T07:46:47.811Z
API health: not checked
FINDINGS  The request timed out waiting for a database lock (confirmed).
1. The request timed out waiting for a database lock  [CONFIRMED]
   The request waited too long for a database lock and was cancelled.
   Evidence: E1, E2, E3, E4
   Interpretation (not verified):
     - Most likely blocker: session 113 (invoice-backfill, idle in transaction, transaction open 26 s, 7 locks held). This is the database's current state, linked to the request only by time.
     - Invoice inv_kestrel_2002 is still open with 0 successful payments, and this request logged no payment.recorded event, so this attempt doesn't appear to have taken a payment.
   Caveats:
     - Database results show the state when the investigation ran, not at the time of the request.
     - pg.blocking_sessions shows no session waiting now. A lock wait ends when the request times out, so the wait itself can't be seen after the fact.
   Next steps:
     - Check the impact below for other requests that failed the same way.
     - Ask engineering or the DBA to identify and end the blocking session; support's read-only role can't and mustn't do that.
     - Ask the customer not to repeat the payment until the blocking session has been dealt with; each attempt waits for the same lock.
   Escalate to Engineering / DBA (high): Database sessions holding locks can only be handled by engineering or a DBA.
Evidence
  E1  log entry at 2026-10-09T07:46:36.014Z, docker:supportops-scenario-billing-api-1:16
      WARNING db.lock_timeout: Database lock timeout [detail=canceling statement due to lock timeout]
  E2  log entry at 2026-10-09T07:46:36.015Z, docker:supportops-scenario-billing-api-1:17
      INFO http.request: POST /v1/invoices/inv_kestrel_2002/pay -> 503 (3,023 ms) [account_id=acct_kestrel]
  E3  database check at 2026-10-09T07:46:47.811Z, pg.long_transactions
      Transactions open longer than 10 seconds: 1. session 113 (invoice-backfill, idle in transaction, transaction open 26 s, 7 locks held).
  E4  database check at 2026-10-09T07:46:47.811Z, billing.invoice_lookup
      Invoice inv_kestrel_2002 (INV-2002, account acct_kestrel): status open, total 41100 USD cents, sum of lines 41100, successful payments 0, failed payments 0.
```

The request-specific `db.lock_timeout` warning (E1) and matching `503` access log (E2) establish why the payment failed. The database checks performed later show that the backfill transaction was still open and that the invoice had no successful payment records.

The reported 3,023 ms duration is consistent with the configured 3,000 ms PostgreSQL lock timeout.

### 2. Inspect the database sessions

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env db run pg.long_transactions pg.blocking_sessions --param min_seconds=10
```

Recorded output:

```text
FAIL     pg.long_transactions  Transactions open longer than 10 seconds: 1.  (4 ms)
         pid                  113
         role                 billing_app
         application          invoice-backfill
         state                idle in transaction
         transaction_seconds  35
         in_state_seconds     35
         wait_event_type      Client
         locks_held           7
         last_query           SELECT id, status FROM billing.invoices WHERE id = 'inv_kestrel_2002' FOR UPDATE;
PASS     pg.blocking_sessions  No session is waiting for a lock held by another session.  (3 ms)
```

The `FAIL` result is an intentional diagnostic finding: PostgreSQL still has a transaction open beyond the requested threshold. Session 113 is owned by `billing_app`, identifies itself as `invoice-backfill`, and is `idle in transaction`; its most recent statement locked the affected invoice row.

The command returns exit code `1` because `pg.long_transactions` detected that condition.

The default threshold for `pg.long_transactions` is **60 seconds**. Passing `--param min_seconds=10` is important here: at the time of this check, the transaction had been open for 35 seconds and would have been omitted at the default threshold.

### 3. Explain why no waiter appears after the timeout

The same query reports:

```text
PASS     pg.blocking_sessions  No session is waiting for a lock held by another session.  (3 ms)
```

That result is not inconsistent with the earlier failure. `pg.blocking_sessions` shows sessions that are **currently waiting**. After the API gives up at its 3-second lock timeout, its waiting transaction is no longer present for that check to find.

To preserve the missing evidence, the harness queried the activity views during `inc005-cust-01` and captured this relationship:

| Captured field | Observation at `07:46:33.143Z` |
| --- | --- |
| Waiting session | PID 183, `billing-api`, role `billing_app` |
| Wait type | `Lock:transactionid` |
| Waiting query | `SELECT id, status, currency, total_cents FROM billing.invoices WHERE account_id = $1 AND id = $2 FOR UPDATE` |
| Blocking session | PID 113, `invoice-backfill`, role `billing_app` |
| Blocking state | `idle in transaction` |
| Blocking transaction age | 11 seconds |
| Blocker's last statement | `SELECT id, status FROM billing.invoices WHERE id = 'inv_kestrel_2002' FOR UPDATE;` |

A row-lock conflict can appear as a `transactionid` wait because PostgreSQL is waiting for the transaction holding the row to finish. Ordinary reads do not need to acquire the same row lock, so PostgreSQL can continue serving the last committed invoice data using MVCC.

In a real support investigation, this relationship is easiest to establish **while the affected request is still waiting**. A later check can show the remaining holder, but it cannot reconstruct a waiting connection that has already timed out.

## Evidence

| Ref | Source (UTC) | Observation |
| --- | --- | --- |
| E1 | API log `:16`, `07:46:36.014Z` | `db.lock_timeout` reported `canceling statement due to lock timeout` for `inc005-cust-01` |
| E2 | API log `:17`, `07:46:36.015Z` | Payment request returned `503` after 3,023 ms for `acct_kestrel` |
| E3 | `pg.long_transactions`, `07:46:47.811Z` | PID 113, `invoice-backfill`, still `idle in transaction`, 26 seconds old with 7 locks held |
| E4 | `billing.invoice_lookup`, `07:46:47.811Z` | Invoice `inv_kestrel_2002` was `open`, with 0 successful and 0 failed payments |
| Capture | Harness `pg.blocking_sessions`, `07:46:33.143Z` | While the payment was in flight, API session 183 was blocked by session 113 on `Lock:transactionid` |

**Evidence timing matters.** E1 and E2 are historical entries tied to the failed request. E3 and E4 describe the database when the investigation ran. The lock-wait capture was taken earlier, while the customer request was still waiting, and is stored by the harness rather than returned by `supportops investigate`.

## Root cause and confidence

**Confirmed finding — `lock_contention`:** The payment request timed out while waiting for a PostgreSQL lock. The request's own warning and access log agree (E1, E2). The harness also observed the billing API connection blocked by the backfill session during the request.

**Root-cause interpretation:** A backfill left its transaction open after taking a row lock on the invoice. Payment processing needs a conflicting lock before it can continue, so attempts against that row wait and reach the configured timeout. Invoice reads still work because they can access the last committed version of the row. The aborted payment transactions did not create payment records in this reproduction.

SupportOps alone identifies the likely blocker from its later SQL snapshot and links it to the request by timing. The separate, live lock-wait capture provides the direct connection between the API waiter and the backfill session. This distinction is preserved in the report rather than presented as if the CLI had reconstructed the past wait.

**Confidence:** Confirmed for the payment request's lock timeout. In this controlled scenario, the live capture and the known setup establish the blocking transaction as the cause.

## Impact

Two payment attempts failed with the same `db.lock_timeout` pattern: `inc005-cust-01` and its retry, `inc005-cust-03`. Both belonged to `acct_kestrel`, so the investigation classified the failure as **recurring** within the available logs.

The impact window ran from approximately `07:31:36Z` to `08:01:36Z`. Coverage was **partial** because the scenario container's logs began partway through the window; the observed counts are lower bounds.

Only the affected invoice's payment path was demonstrated as blocked. Reading that invoice returned `200`. The scenario did not test payments on other invoices, so the report makes no claim about their behavior.

## Resolution or workaround

**Customer guidance:** Avoid repeated payment attempts until the blockage is cleared. Retries against the same locked invoice will wait and fail again. At the time of the investigation, the invoice was still open and the billing database showed **no successful payment records for these attempts**. That state should be checked again before any customer-facing confirmation or a new payment attempt.

**Engineering / DBA action:** Identify the owner of `invoice-backfill` and determine whether the transaction can be completed or rolled back safely. If the owner is unavailable, a DBA may need to terminate the session after evaluating its work and operational impact. SupportOps intentionally offers no session-termination command.

After the blocking transaction is released, verify that the long-transaction and blocking-session checks are clear, inspect the current invoice/payment state, and confirm that a supervised payment attempt succeeds.

In the **disposable scenario lab**, recovery is deliberately simpler:

```powershell
uv run supportops-lab reset
```

The reset recorded this clean baseline:

```text
Scenario lab supportops-scenario was recreated with a fresh database.
Baseline: clean (health HEALTHY; db.connectivity pass, billing.invoice_total_mismatch pass, billing.paid_invoice_without_payment pass, billing.payment_on_unpaid_invoice pass, billing.duplicate_payments pass, pg.long_transactions pass, pg.blocking_sessions pass)
```

Reset recreates only the verified disposable containers. It does not change the persistent SupportOps lab or its database.

## Escalation

**Owner:** Engineering / DBA; **priority:** High

The handoff should identify the affected customer and invoice, and provide enough evidence to act without rerunning the full investigation:

- Failed request `inc005-cust-01` (`503 DATABASE_BUSY` at `07:46:36.015Z`) and retry `inc005-cust-03`.
- Account `acct_kestrel`, invoice `inv_kestrel_2002` (`INV-2002`).
- Blocking PID 113, `application_name=invoice-backfill`, role `billing_app`, `idle in transaction`, with its last `FOR UPDATE` statement.
- Captured wait from PID 183, alongside log evidence E1–E2 and database evidence E3–E4.
- Current invoice status `open`, with no successful payment recorded at the time of the check.

**Requested action:** Have the maintenance-job owner safely finish or roll back the transaction. Confirm the block is gone and the payment path has recovered before advising the customer to retry.

## Customer update

> Hello,
>
> We've investigated the payment errors and found that a database operation on our side is preventing the invoice from being updated. This explains why payment requests are timing out even though you can still view the invoice.
>
> Our billing records currently show no successful payment for the attempts we reviewed. Please avoid retrying the payment while our engineering team works to clear the issue; we'll verify the invoice and payment status again before asking you to try.
>
> We'll share another update by **[agreed UTC time]**, or earlier if the issue is resolved.
>
> Best regards,\
> Support team

*Draft for a simulated customer. No message was sent; verify payment state again before using similar wording in a real case.*

## Prevention and follow-up

| Action | Owner | Status |
| --- | --- | --- |
| Run backfills in short transactions that commit regularly; do not leave maintenance transactions open | Engineering | Proposed |
| Configure an appropriate `idle_in_transaction_session_timeout` for maintenance roles | DBA | Proposed |
| Monitor for unexpected long transactions and active blockers | DBA / Support | Proposed |
| Assign descriptive `application_name` values to maintenance jobs for easier identification | Engineering | Proposed |
| Explain the importance of checking for blocking sessions *during* a timeout in the [database diagnostics runbook](../runbooks/database-diagnostics.md) | Support | Done |
