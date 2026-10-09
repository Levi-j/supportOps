# Database diagnostics

Use this runbook when a billing issue may involve PostgreSQL: an invoice total looks wrong, a payment is missing, a request times out with `503 DATABASE_BUSY`, or an API outage requires a closer look at the database.

The procedures below are **read-only**. They inspect records, transactions, and database activity without correcting data, changing permissions, or interfering with active sessions.

## Connection and safety model

SupportOps loads its PostgreSQL connection string from `SUPPORTOPS_DB_URL`, either from the environment or from the local `.env` file. It does not accept a password or connection string as a CLI argument.

The lab uses a connection of this form:

```text
postgresql://supportops_ro:<password>@127.0.0.1:5433/billing
```

Each diagnostic connection identifies itself as `supportops` through PostgreSQL's `application_name`. Checks run in separate read-only transactions, with `default_transaction_read_only=on`, a five-second statement timeout, and a connection timeout controlled by `SUPPORTOPS_CONNECT_TIMEOUT_SECONDS`. The connection is closed when the check finishes.

The CLI executes **only named queries from its diagnostic catalog**. There is no free-form SQL execution command, and user-supplied values are bound as parameters rather than concatenated into SQL.

PostgreSQL enforces the read-only transaction setting even if a privileged account is configured. Integration tests verify that attempts to write within these transactions fail for both `supportops_ro` and the lab administrator. This protection complements, rather than replaces, a least-privilege database role.

### Least-privilege access

The local `supportops_ro` role has `SELECT` access to billing data and belongs to `pg_monitor`, allowing it to inspect the activity and locks of other sessions. It has no permission to modify billing records, and its sessions default to read-only.

Run `db.connectivity` to check which role is actually connected:

```powershell
uv run supportops db run db.connectivity
```

The check warns about privileges that are inappropriate for a support account:

| Finding | Why it matters |
| --- | --- |
| Superuser access | The credentials could be used outside SupportOps to change or delete data. |
| Administrative attributes | The role could create roles or databases, bypass row-level security, or start replication. |
| Write access on tables | The role has privileges such as `INSERT`, `UPDATE`, `DELETE`, or `TRUNCATE`. |
| Session is not read-only | The connection does not have the expected transaction protection. |

A privilege warning produces a `WARN` finding and exit code `1`; SupportOps does not modify the role. Arrange appropriate read-only credentials before continuing with sensitive investigations.

Without sufficient monitoring rights, PostgreSQL restricts visibility into other sessions. SupportOps reports the relevant activity checks as **incomplete** rather than treating limited visibility as evidence that no problems exist.

## Available checks

List the catalog, or inspect a predefined query without executing it:

```powershell
uv run supportops db checks
uv run supportops db checks billing.api_key_status --show-sql
```

| Check | Purpose | Parameter |
| --- | --- | --- |
| `db.connectivity` | Server version, connected role, read-only state, and privilege warnings | None |
| `pg.connections` | Database sessions grouped by role, application, and state | None |
| `pg.long_transactions` | Open transactions exceeding the specified duration | `min_seconds` (default `60`) |
| `pg.blocking_sessions` | Sessions waiting for locks and the sessions blocking them | None |
| `billing.invoice_total_mismatch` | Stored invoice totals that disagree with invoice-line totals | None |
| `billing.paid_invoice_without_payment` | Paid invoices without a successful payment | None |
| `billing.payment_on_unpaid_invoice` | Successful payments on invoices not marked paid | None |
| `billing.duplicate_payments` | More than one successful payment associated with an invoice | None |
| `billing.api_key_status` | Account and lifecycle metadata for an API-key prefix | `prefix` (required) |
| `billing.invoice_lookup` | Invoice status, totals, and payment counts | `id` **or** `number` |

Run one or several checks by name, or run all checks that do not need an unavailable lookup value:

```powershell
uv run supportops db run --all
uv run supportops db run billing.invoice_lookup --param number=INV-1003
uv run supportops db run pg.long_transactions pg.blocking_sessions --param min_seconds=30
```

With `--all`, parameter-dependent checks are reported as `SKIPPED` when their required values are absent. SupportOps does not guess an invoice ID or key prefix.

### Understand the results

| Status | Meaning |
| --- | --- |
| `PASS` | The check completed and found no problem under its criteria. |
| `FAIL` | It detected an inconsistency, or a requested lookup returned no match. |
| `WARN` | The configured database account has excessive privileges or another safety concern. |
| `INFO` | Informational evidence, such as a matching invoice or session summary. |
| `ERROR` | The check could not finish; no reliable conclusion is available. |
| `SKIPPED` | A required lookup parameter was not supplied. |

Exit codes are `0` for a successful run without failures or warnings, `1` for a `FAIL` or `WARN`, `2` for invalid usage or configuration, and `3` when a check cannot be completed. In particular, **`FAIL` describes a finding; `ERROR` describes missing evidence**. Do not treat them as interchangeable.

Use `--json` for the complete structured results, or `--show-sql` to display the catalog query alongside the result. Check results include a duration and relevant rows; terminal output may abbreviate large result sets.

### Parameter binding

The SQL is fixed in the catalog. Parameters appear as placeholders:

```sql
WHERE api_key.key_prefix = %(prefix)s
```

For example:

```powershell
uv run supportops db run billing.api_key_status --param prefix=bk_juniper00
```

SupportOps validates the supplied prefix (exactly 12 permitted characters) and passes it to PostgreSQL separately from the SQL. This prevents the value from changing the structure of the query. The parameter takes only a **prefix**, never a full API key.

Unknown check names, invalid parameters, and conflicting invoice identifiers are rejected before the database query runs.

## Investigate an invoice or payment

### Look up the reported invoice

Start with the identifier from the customer or API response:

```powershell
uv run supportops db run billing.invoice_lookup --param number=INV-1003
```

A shortened example from the seeded lab is:

```text
INFO  billing.invoice_lookup  Matching invoices: 1

invoice_id          inv_juniper_1003
number              INV-1003
account_id          acct_juniper
account_name        Juniper Dental Group
status              open
currency            EUR
total_cents         14900
line_total_cents    14900
succeeded_payments  0
failed_payments     1
```

Amounts are stored in **integer cents**: `14900` means EUR 149.00. In this example, the invoice is open and its stored total matches its lines. There is one failed payment attempt and no successful payment recorded. That explains what the database contains; it does not, by itself, establish why the attempt failed.

Invoice numbers are unique **within an account**, not necessarily across the entire database. If a number matches more than one account, use `account_id` to identify the relevant record before drawing conclusions. A lookup with no match returns `FAIL` and exit code `1`.

### Check for inconsistencies

```powershell
uv run supportops db run billing.invoice_total_mismatch billing.paid_invoice_without_payment billing.payment_on_unpaid_invoice billing.duplicate_payments
```

A healthy seed should pass all four checks. If one fails, inspect the affected invoice IDs and the accompanying evidence:

| Failed check | What the result establishes | Usual escalation |
| --- | --- | --- |
| `invoice_total_mismatch` | The stored invoice total does not match its line items. | Engineering |
| `paid_invoice_without_payment` | An invoice is marked paid without a corresponding successful payment. | Engineering / finance |
| `payment_on_unpaid_invoice` | A successful payment exists, but the invoice is not marked paid. | Engineering; assess possible customer impact promptly |
| `duplicate_payments` | Multiple successful payment records exist for one invoice. | Engineering / finance; check potential duplicate charges |

Do not assume a cause from a consistency check alone. Correlate invoice IDs, payment events, timestamps, and request IDs using the [logs runbook](logs-and-request-ids.md). A `500` response, for example, does not prove that the underlying transaction made no changes.

Records added during your own lab testing are included in these checks. They are not automatically considered defects merely because they were not part of the original seed. **Do not reset the database to run a diagnostic.**

## Investigate database sessions and locks

### Review current connections

`pg.connections` summarizes information from `pg_stat_activity`:

```powershell
uv run supportops db run pg.connections
```

| Field | Meaning |
| --- | --- |
| `role` | PostgreSQL user associated with the session |
| `application` | The application's reported name, such as `billing-api` or `supportops` |
| `state` | `active`, `idle`, or `idle in transaction` |
| `sessions` | Number of sessions in that group |

An `idle` session is waiting for work and does not necessarily have an open transaction. `idle in transaction` is different: a transaction remains open even though the session is not currently running a query. Because the lab's billing API opens a connection per request, a quiet lab may have few sessions to report.

### Find long-running transactions

```powershell
uv run supportops db run pg.long_transactions --param min_seconds=30
```

This check looks for transactions that have been open longer than the threshold. It reports their age, state, lock count, and a short excerpt of the last query. A connection that is simply idle **without** an open transaction is not listed.

Long-lived `idle in transaction` sessions deserve particular attention. Depending on what they have done, they may retain locks and prevent other operations from progressing. In the billing API, a blocked write can eventually return `503 DATABASE_BUSY` when the lock timeout is reached, even while ordinary invoice reads continue.

### Identify blocking sessions

```powershell
uv run supportops db run pg.blocking_sessions
```

The check uses PostgreSQL's `pg_blocking_pids()` to pair each waiting session with its blocker. It reports relevant application names, session states, transaction ages, wait information, and limited excerpts of query text. Query excerpts are truncated to 120 characters and passed through SupportOps' redaction logic; review them before sharing because masking cannot cover every possible literal.

A common pattern is an API request waiting behind a maintenance script or console session left `idle in transaction`. The check identifies what PostgreSQL is observing; it does not determine whether the blocking work is safe to cancel.

### What support must not do

Once you have identified a possible blocker or consistency issue, **collect evidence and escalate**. Do not:

- Cancel or terminate other database sessions with `pg_cancel_backend()` or `pg_terminate_backend()`.
- Update, delete, or otherwise repair invoices, payments, customers, or keys yourself.
- Change PostgreSQL roles, privileges, timeouts, or other settings.
- Run unapproved corrective SQL against production systems.

An engineer, DBA, or authorized owner must decide how to resolve the condition and assess the impact of any intervention.

## Read-only inspection with `psql`

You do not need to install `psql` on your machine; the lab container includes it. These examples use the read-only role and do not change application data:

```powershell
docker compose --project-name supportops exec postgres psql -U supportops_ro -d billing -c "SELECT id, number, status, total_cents FROM billing.invoices ORDER BY created_at DESC LIMIT 5;"

docker compose --project-name supportops exec postgres psql -U supportops_ro -d billing -c "SELECT usename, application_name, state, now() - xact_start AS transaction_age FROM pg_stat_activity WHERE datname = 'billing';"

docker compose --project-name supportops exec postgres psql -U supportops_ro -d billing -c "SELECT pid, pg_blocking_pids(pid) AS blocked_by, wait_event_type FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;"
```

Use `supportops db checks --show-sql` if you want to inspect the exact query behind a catalog check. The examples above are read-only; not every statement beginning with `SELECT` is necessarily harmless if it invokes a side-effecting function. Use approved diagnostic queries rather than improvising SQL in an unfamiliar environment.

## Troubleshoot diagnostic failures

| Result or error | Likely issue | Next check |
| --- | --- | --- |
| PostgreSQL rejects the login (exit `3`) | Wrong user or password | Verify the source of `SUPPORTOPS_DB_URL` without exposing the password. |
| Connection refused or timed out (exit `3`) | Database stopped, incorrect host or port, or connectivity issue | Run `docker compose ps postgres`; the lab uses `127.0.0.1:5433`. |
| Statement timeout | Query exceeded the five-second limit | Inspect locks and long transactions; escalate persistent timeouts. |
| Permission denied | Role lacks access to a required table or view | Verify grants with the database owner. |
| Missing table or schema | Wrong database or incomplete environment | Confirm the connection target. |
| Activity result marked incomplete | Monitoring visibility is restricted | Request appropriate monitoring rights or describe the limitation in the handoff. |

SupportOps masks recognized passwords and connection-string secrets in terminal and JSON output. Do not attach raw connection details or copied exception traces without reviewing them first.

## Escalation checklist

Contact **engineering** for billing-data inconsistencies, mismatches between database and API responses, or repeated `DATABASE_BUSY` failures. Involve a **DBA or on-call engineer** for unexplained locks, unusually long transactions, statement timeouts, or database availability problems. Treat unexpected use of a superuser or write-capable diagnostic account as a **security or access-control concern**.

Include the time of the check, environment, check names and statuses, relevant invoice/account IDs, request IDs, and concise evidence rows or redacted JSON output. State any gaps in monitoring visibility or checks that returned `ERROR` or `SKIPPED`.
