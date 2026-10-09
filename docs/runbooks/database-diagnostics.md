# Database diagnostics

Use this runbook when a billing issue may involve PostgreSQL: an unexpected invoice total, a missing payment, a `503 DATABASE_BUSY` response, or requests failing while the API itself remains available.

**The diagnostic workflow is read-only.** Collect evidence first. Data repairs, role changes, and decisions about active database sessions belong to authorized engineers or DBAs.

## Connection and safety model

SupportOps reads `SUPPORTOPS_DB_URL` from the environment or your local `.env`; it does not accept database passwords on the command line. The billing lab uses a connection shaped like:

```text
postgresql://supportops_ro:<password>@127.0.0.1:5433/billing
```

Every check runs predefined SQL in a read-only PostgreSQL transaction with bound parameters, a five-second statement timeout, and the connection name `supportops`. There is no arbitrary-SQL command. These protections complement the database role's permissions; they do not replace them.

### Least-privilege access

The lab's `supportops_ro` role can read billing tables but cannot modify them. It also has `pg_monitor`, which allows the diagnostic session checks to see activity from other PostgreSQL roles.

Verify the current connection:

```powershell
uv run supportops db run db.connectivity
```

The check warns if the account is a superuser, has administrative attributes or table-write privileges, or is not operating read-only. It does **not** attempt to fix permissions.

Without `pg_monitor`, PostgreSQL may hide other roles' sessions. Activity checks then mark the evidence **incomplete**, not clean. The [OrderFlow integration](../integrations/orderflow.md) intentionally makes that monitoring grant optional.

## Available checks

```powershell
uv run supportops db checks
uv run supportops db checks billing.api_key_status --show-sql
```

| Check | Purpose | Parameter |
| --- | --- | --- |
| `db.connectivity` | Connection, current role, and permission safety | — |
| `pg.connections` | Sessions grouped by role, application, and state | — |
| `pg.long_transactions` | Transactions open beyond a threshold | `min_seconds`, default `60` |
| `pg.blocking_sessions` | Waiting sessions and their blockers | — |
| `billing.invoice_total_mismatch` | Invoice totals that differ from line totals | — |
| `billing.paid_invoice_without_payment` | Paid invoices with no successful payment | — |
| `billing.payment_on_unpaid_invoice` | Successful payment on an unpaid invoice | — |
| `billing.duplicate_payments` | Multiple successful payments for one invoice | — |
| `billing.api_key_status` | Lifecycle and account metadata for a key prefix | `prefix` required |
| `billing.invoice_lookup` | Invoice, line, and payment details | `id` or `number` |

Run all applicable non-parameterized checks or select individual checks:

```powershell
uv run supportops db run --all
uv run supportops db run billing.invoice_lookup --param number=INV-1003
uv run supportops db run pg.long_transactions pg.blocking_sessions --param min_seconds=30
```

`--all` marks lookups without required parameters as `SKIPPED`; it never guesses which invoice or key to inspect. The target determines which checks are available: billing and OrderFlow can each run the generic PostgreSQL pack plus **their own** application-specific checks. Cross-target selections are rejected before connecting. See the [OrderFlow guide](../integrations/orderflow.md#database-checks) for its four order and inventory checks.

### Understand the results

| Status | Meaning |
| --- | --- |
| `PASS` | The check completed and found no matching problem within its scope |
| `FAIL` | An inconsistency was found, or a requested record wasn't found |
| `WARN` | Safety concern such as excessive account privileges |
| `INFO` | Informational evidence, including limited session visibility |
| `SKIPPED` | Required lookup parameter wasn't supplied |
| `ERROR` | Check couldn't complete; no clean result can be inferred |

Exit codes are `0` for no reported failures or warnings, `1` for `FAIL` or `WARN`, `2` for invalid usage or configuration, and `3` when a check cannot be completed. **A `FAIL` is a finding; an `ERROR` is a gap in evidence.** Use `--json` for structured results, including completeness flags and returned rows.

### Parameter binding

User values are validated and passed separately from the SQL, for example:

```sql
WHERE api_key.key_prefix = %(prefix)s
```

```powershell
uv run supportops db run billing.api_key_status --param prefix=bk_juniper00
```

This lookup accepts a **12-character prefix**, not a full API key. Unknown checks, invalid parameters, and conflicting invoice identifiers are rejected before executing SQL.

## Investigate an invoice or payment

### Look up the reported invoice

Start with an ID or invoice number from the customer's report:

```powershell
uv run supportops db run billing.invoice_lookup --param number=INV-1003
```

For the seeded lab, the result includes values such as:

```text
invoice_id          inv_juniper_1003
number              INV-1003
account_id          acct_juniper
status              open
currency            EUR
total_cents         14900
line_total_cents    14900
succeeded_payments  0
failed_payments     1
```

Amounts are stored in integer cents: `14900` means EUR 149.00. This snapshot shows one failed payment attempt and no successful payment; it does **not** explain why the attempt failed.

Invoice numbers are unique within an account, not necessarily across the whole database. If a number matches multiple accounts, use the account ID to identify the right record.

### Check for inconsistencies

```powershell
uv run supportops db run billing.invoice_total_mismatch billing.paid_invoice_without_payment billing.payment_on_unpaid_invoice billing.duplicate_payments
```

| Finding | What it establishes | Escalation |
| --- | --- | --- |
| Total mismatch | Stored amount disagrees with invoice lines | Engineering |
| Paid without payment | Invoice is paid without a successful payment record | Engineering / finance |
| Payment on unpaid invoice | Successful payment exists while invoice remains unpaid | Engineering; assess customer impact |
| Duplicate payments | Multiple successful payments are recorded | Engineering / finance; assess duplicate charges |

Use the invoice ID, request IDs, timestamps, and [application logs](logs-and-request-ids.md) to investigate causes. A `500` does **not** guarantee that no data was written. Likewise, current database state does not necessarily describe what existed when an earlier request ran.

Don't reset the database just to get a clean diagnostic result. Records created during legitimate local testing are part of the current state.

## Investigate database sessions and locks

### Review current connections

```powershell
uv run supportops db run pg.connections
```

The check summarizes role, application, state, and session count. An `idle` connection is simply waiting for work; `idle in transaction` means a transaction remains open and may be retaining locks.

### Find long-running transactions

```powershell
uv run supportops db run pg.long_transactions --param min_seconds=30
```

This reports open transactions older than the threshold, including their age, state, lock count, and a shortened query excerpt. A long-lived `idle in transaction` session deserves attention, but its age alone does not prove that it is blocking another request.

### Identify blocking sessions

```powershell
uv run supportops db run pg.blocking_sessions
```

Using PostgreSQL's `pg_blocking_pids()`, this check identifies sessions **currently waiting** and the sessions blocking them. It provides application names, transaction ages, wait information, and truncated query excerpts. The excerpts are redacted where recognizable patterns match; review them before sharing.

**Timing matters:** when the billing API's three-second lock timeout expires, the waiting query disappears. A later `pg.blocking_sessions` check may be empty even though the blocking transaction remains visible in `pg.long_transactions`. A row-lock wait often appears as `Lock:transactionid`.

### Practise it safely: INC-005

[INC-005](../incidents/INC-005-blocked-writes.md) demonstrates lock contention in the disposable scenario environment—not the persistent billing database.

```powershell
uv run supportops-lab start INC-005
uv run supportops-lab status
uv run supportops --env-file .lab\supportops-scenario\supportops.env db run pg.long_transactions pg.blocking_sessions --param min_seconds=10
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc005-cust-01
uv run supportops-lab reset
```

A simulated `invoice-backfill` session holds a row lock while a payment waits. The payments return `503 DATABASE_BUSY` with `Retry-After: 5`, but an ordinary invoice read succeeds. The harness captures the lock wait while it exists; `reset` restores the **disposable** environment.

### What support must not do

Do not terminate sessions with `pg_terminate_backend()` or `pg_cancel_backend()`, edit billing rows, change database grants or timeouts, or attempt an unapproved correction. Collect the evidence and ask an authorized engineer or DBA to decide what is safe.

## Read-only inspection with `psql`

The billing container includes `psql`, so a separate local installation isn't necessary. These commands only inspect state:

```powershell
docker compose --project-name supportops exec postgres psql -U supportops_ro -d billing -c "SELECT id, number, status, total_cents FROM billing.invoices ORDER BY created_at DESC LIMIT 5;"
docker compose --project-name supportops exec postgres psql -U supportops_ro -d billing -c "SELECT usename, application_name, state FROM pg_stat_activity WHERE datname = 'billing';"
```

Even a SQL statement beginning with `SELECT` can have side effects if it calls a function. Prefer the approved catalog rather than improvising SQL in an unfamiliar database.

## Troubleshoot diagnostic failures

| Symptom | What to check |
| --- | --- |
| Login rejected | Verify the source of `SUPPORTOPS_DB_URL` without exposing its password |
| Connection refused or timed out | Check the intended host and port; inspect `docker compose --project-name supportops ps postgres` for the lab |
| Statement timeout | Investigate locks, query duration, and long transactions |
| Permission denied | Ask the database owner to confirm read-only grants |
| Table or schema missing | Verify that the diagnostic URL points to the correct service database |
| Session visibility incomplete | Report the limitation or request approved monitoring access |

SupportOps masks recognized secrets, but exported results still require review before they are attached to tickets or shared externally.

## Escalation checklist

Involve **engineering** for data inconsistencies or repeated `DATABASE_BUSY` errors, a **DBA/on-call engineer** for unexplained locks and availability issues, and **security or access-control owners** if a diagnostic account has unexpected write or administrative privileges.

Include the environment, timestamp, exact check names and statuses, relevant invoice/account and request IDs, evidence rows, and any `ERROR`, `SKIPPED`, or incomplete-visibility caveats. Do not include passwords or raw customer credentials.
