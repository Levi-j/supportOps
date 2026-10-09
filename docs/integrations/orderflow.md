# OrderFlow integration

SupportOps can investigate **OrderFlow**, an independently developed Java 21 / Spring Boot service for authentication, inventory, and order processing. This optional target uses the same diagnostic CLI as the billing lab, with stricter safeguards: **OrderFlow is read-only from SupportOps, even when `--yes` is supplied.**

> **Verification status:** This integration has been tested with synthetic ECS log fixtures and disposable PostgreSQL databases built from OrderFlow's V1–V6 migrations. It has **not yet been exercised against a live OrderFlow deployment**. SupportOps itself, its tests, and CI do not require the OrderFlow repository or its running services.

## What it provides

| Area | What SupportOps can inspect |
| --- | --- |
| Health | Spring Actuator liveness and aggregate health, optionally compared with direct PostgreSQL connectivity |
| HTTP | Read-only requests, response times, status codes, and `X-Request-Id` correlation |
| Authentication | JWT structure and unverified claims, plus the service's `/api/v1/users/me` response |
| Logs | ECS JSON entries, request IDs, events, durations, and exception fields |
| Database | Four schema-specific read-only SQL checks, alongside generic PostgreSQL diagnostics |
| Investigations | Insufficient stock, failed logins, rejected credentials, inconsistent orders, and generic error cases |

SupportOps does **not** log in to OrderFlow, verify JWT signatures, repair data, or change OrderFlow's containers or database. The service's aggregate health response does not identify the failed component, so the diagnostic output does not treat a `DOWN` status as proof of a database outage.

## Configure

From the **SupportOps repository root**, create a local configuration file:

```powershell
Copy-Item orderflow.env.example orderflow.env
uv run supportops --env-file orderflow.env config show
```

The configuration should report `SUPPORTOPS_TARGET=orderflow` and the OrderFlow API URL, normally `http://127.0.0.1:8080`. `orderflow.env` is Git-ignored. Supplying `--env-file` explicitly keeps your normal billing `.env` separate; note that shell environment variables take precedence over values in either file.

You can start with only the API, then enable additional evidence sources:

| Mode | Configuration | Available diagnostics |
| --- | --- | --- |
| API only | `SUPPORTOPS_TARGET`, `SUPPORTOPS_API_URL` | Health, HTTP requests and latency; auth check with a token |
| API and logs | Add `SUPPORTOPS_LOG_SOURCE` | Log searches and investigations with `--no-db` |
| Full diagnostics | Add `SUPPORTOPS_DB_URL` for a restricted role | Catalog SQL, database comparison, and investigations with database evidence |

Without a database URL, `db run` exits with a configuration hint; health and investigations label database evidence as **not checked** rather than making assumptions.

## Start OrderFlow (in its own repository)

If you want a local instance, follow the instructions in **OrderFlow's own README**. From the OrderFlow repository—not SupportOps—its Compose application profile is started with:

```powershell
docker compose --profile app up -d --build
docker compose --profile app ps
```

OrderFlow's Compose setup exposes the API on `127.0.0.1:8080` and PostgreSQL on `127.0.0.1:5432`, with structured ECS console logs. Its application container may be named `orderflow-backend-app-1`; confirm the actual name with `docker ps` before setting `SUPPORTOPS_LOG_SOURCE`. **Starting OrderFlow is a separate, user-controlled action; SupportOps does not do it.**

## Get a token

OrderFlow issues HS256 JWTs through `POST /api/v1/auth/login`, valid for 30 minutes. SupportOps does not call that write-method endpoint; use OrderFlow's own login flow, then pass the resulting token by environment-variable name.

PowerShell example, using your own test-account email:

```powershell
$password = Read-Host "OrderFlow password" -AsSecureString
$login = @{
    email = "you@example.com"
    password = [System.Net.NetworkCredential]::new("", $password).Password
} | ConvertTo-Json

$env:ORDERFLOW_TOKEN = (
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/api/v1/auth/login" `
        -ContentType "application/json" -Body $login
).accessToken

Remove-Variable password, login
uv run supportops --env-file orderflow.env auth check --key-env ORDERFLOW_TOKEN
```

The password is briefly converted to plain text in memory to build the login request. The example avoids putting it in command-line arguments or shell history. Never paste the resulting token into a support ticket or public log. When finished:

```powershell
Remove-Item Env:ORDERFLOW_TOKEN
```

`auth check` decodes the JWT header and claims **without verifying its signature**. Its output separates three things:

- **Evidence:** What SupportOps observed, such as an HTTP status, the `WWW-Authenticate` header, and the ID and role returned by OrderFlow.
- **Unverified claims:** What the JWT *says* about its subject, role, issuer, issue time, and expiration.
- **Interpretation:** What might explain a failure. For example, an expired claim could explain `401`, but a wrong signing secret would produce the same HTTP response.

The service expects issuer `orderflow` and a single role claim of `ADMIN` or `CUSTOMER`. Spring Security's default clock-skew allowance is 60 seconds; SupportOps explains that allowance without presenting decoded values as trustworthy. It does not include the bearer token or the email from `/api/v1/users/me` in the authentication report.

## Set up the read-only database role (administrator)

This step is for an **OrderFlow database administrator**, not the SupportOps CLI. Skip it when you only need HTTP or log diagnostics. The [role setup script](orderflow-readonly-role.sql) creates `supportops_orderflow_ro` and grants:

- `CONNECT` to the database where the script is run.
- `USAGE` on `public` and `SELECT` on `products`, `inventory_items`, `inventory_movements`, `orders`, and `order_items`.
- Read-only transactions by default, with a 5-second statement timeout.

The role receives **no access to `users`**, which holds emails and password hashes. The script does not modify existing OrderFlow roles, tables, or rows, and it contains no password. However, running it **does create a role and grant privileges**, so only an authorized administrator should perform these steps.

For a local Docker setup with the two repositories in sibling directories, start in the OrderFlow repository and adjust the relative file path if yours differs:

```powershell
# Run from the OrderFlow repository, not from SupportOps.
docker compose cp ../supportOps/docs/integrations/orderflow-readonly-role.sql postgres:/tmp/supportops-ro.sql
docker compose exec postgres psql -U orderflow -d orderflow -f /tmp/supportops-ro.sql

# Open an interactive psql session to set the new role's password.
docker compose exec postgres psql -U orderflow -d orderflow
```

At the `psql` prompt, enter:

```text
\password supportops_orderflow_ro
\q
```

`\password` prompts for the password rather than echoing it or placing it in shell history. Check the database name, service name, and administrator role against your OrderFlow Compose configuration before running these commands. This script is intended for one-time setup; rerunning it without checking for the existing role will fail.

In your local Git-ignored `orderflow.env`, configure a URL for that restricted role (URL-encode any password characters that are special in a URI):

```dotenv
SUPPORTOPS_DB_URL=postgresql://supportops_orderflow_ro:<url-encoded-password>@127.0.0.1:5432/orderflow
```

`pg_monitor` is optional and is **not** granted by default. Without it, the generic session/lock checks cannot see all other users' sessions. SupportOps reports those results as **incomplete**, not as a clean pass.

Role removal is also an administrator action; review dependencies and privileges before using `DROP OWNED` or `DROP ROLE`. Neither removal nor creation is part of SupportOps' diagnostic commands.

## Commands and what to expect

With OrderFlow running and the appropriate settings enabled, use the commands that match your configuration:

```powershell
uv run supportops --env-file orderflow.env health
uv run supportops --env-file orderflow.env api request GET /api/v1/products
uv run supportops --env-file orderflow.env logs search --level ERROR --since 15m
uv run supportops --env-file orderflow.env db run --all
uv run supportops --env-file orderflow.env db run orderflow.order_lookup --param id=42
uv run supportops --env-file orderflow.env investigate REQUEST_ID
```

The `db` commands require the restricted database configuration. Log commands require a usable Docker or file log source. Investigations can still run without database access using `--no-db`.

### Health

SupportOps queries `/actuator/health/liveness` and the aggregate `/actuator/health`. The aggregate response includes PostgreSQL but generally reports only `UP` or `DOWN`, without component-level details.

| Observation | Reported interpretation |
| --- | --- |
| Liveness and aggregate health report `200 UP` | `HEALTHY`: OrderFlow reports itself available |
| Liveness endpoint cannot be reached | `DOWN` / `api_unreachable`: the API did not answer |
| Liveness returns `404` | `INCONCLUSIVE` / `liveness_endpoint_unavailable`: this probe may not be enabled |
| Aggregate returns `503 DOWN`, direct PostgreSQL probe succeeds | `DEGRADED` / `health_down_database_reachable`: cause unknown; compare the service's own database connectivity and other health components |
| Aggregate returns `503 DOWN`, direct probe also fails | `DEGRADED` / `health_down_database_unreachable`: both observations suggest investigating PostgreSQL, but do not confirm causation |
| Aggregate returns `503 DOWN`, no database URL configured | `DEGRADED` / `health_down_database_not_checked`: PostgreSQL was not checked independently |

A direct probe observes connectivity **from the machine running SupportOps**, which may have a different network path from OrderFlow. Treat these results as starting points, not a root-cause verdict.

### Authentication

Use `uv run supportops --env-file orderflow.env auth check --key-env ORDERFLOW_TOKEN` to compare unverified JWT claims with the API's own authentication response. The findings distinguish observed evidence from claims and possible explanations (see [Get a token](#get-a-token)). An authenticated response returns exit code `0`; a rejection or other authentication problem returns `1`, a missing configured token `2`, and an unreachable service `3`.

### Database checks

| Check | Purpose |
| --- | --- |
| `orderflow.inventory_mismatch` | Compare stock on hand with the sum of inventory movements |
| `orderflow.order_total_mismatch` | Compare order totals with summed line-item totals |
| `orderflow.orders_without_items` | Find orders that have no items |
| `orderflow.order_lookup` | Inspect one order's status, totals, item counts, and placed/cancelled stock movements |

All checks are predefined, parameterized, and executed in read-only transactions. The target cannot run billing-specific SQL, including from an investigation. `db run --all` skips `orderflow.order_lookup` unless you supply `--param id=...`; it does not invent a lookup ID. The lookup reports whether an idempotency key exists, **not the key or request hash itself**, and never retrieves user emails.

### Investigations

| Finding | Supporting evidence | Confidence |
| --- | --- | --- |
| `insufficient_stock` | `inventory.insufficient_stock` event and a matching `409` | Confirmed |
| `login_failed` | `auth.login_failed` event and a matching `401` | Confirmed |
| `credentials_rejected` | A non-login `401`, without a logged reason for token rejection | Possible |
| `order_inconsistent` | Current SQL data disagrees with expected order, item, or movement invariants | Confirmed or likely **for the current data state**, depending on corroboration |

An `order_inconsistent` finding is **not** a claim that the investigated request caused the discrepancy. A successful order event may be unrelated to a later data change; even a failed request linked to the same order is only a *possible association*. The support response is to preserve evidence and escalate to the service owners, not edit order or inventory data.

For a completely offline demonstration, investigate the included ECS fixture:

```powershell
$env:SUPPORTOPS_TARGET = "orderflow"
uv run supportops investigate of-stock-409 tests/fixtures/logs/orderflow-ecs.jsonl --no-db
Remove-Item Env:SUPPORTOPS_TARGET
```

That fixture demonstrates the `insufficient_stock` finding without running OrderFlow or connecting to a database. The other investigation rules have unit and isolated integration tests.

## Safeguards

- **No HTTP writes:** Only `GET`, `HEAD`, and `OPTIONS` are permitted against this target. `POST`, `PUT`, `PATCH`, and `DELETE` are refused before any request, including with `--yes`.
- **No arbitrary SQL:** Only catalog queries can run, and OrderFlow checks cannot access the `users` table through the documented role.
- **Credential protection:** Tokens, Authorization headers, database passwords, and `/users/me` email values are excluded or redacted from diagnostics. Review sensitive logs before sharing them even when redaction is enabled.
- **Structured logs required:** OrderFlow emits ECS JSON through its Compose setup or `LOGGING_STRUCTURED_FORMAT_CONSOLE=ecs`. Plain-text console logs are not supported.
- **Limited impact assessment:** OrderFlow access logs do not carry a user identifier, so request counts cannot be treated as affected-user counts.
- **Schema drift:** SQL checks reflect migrations V1–V6 at OrderFlow commit `e878ddf`. If the schema changes, they may return `ERROR`; the fixture and catalog must then be reviewed against the new migrations.

## Return to the billing lab

Omit `--env-file orderflow.env` to use your normal billing `.env`. Clear any `SUPPORTOPS_*` shell variables that override the file, then check the effective target with `uv run supportops config show`.

## Limits and schema drift

OrderFlow's structured log format, JWT settings, and health-probe availability depend on its configuration. The ECS error field shape and Spring Security's 60-second clock-skew allowance are framework expectations, not guarantees for every future deployment. SQL checks follow OrderFlow migrations V1–V6 at commit `e878ddf`; schema changes may cause diagnostic `ERROR` results until the catalog and fixture are updated.

For the broader CLI workflow, start with the [main README](../../README.md).
