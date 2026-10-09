# SupportOps

SupportOps is a Python command-line toolkit for investigating problems in backend APIs. It combines HTTP checks, structured logs, and read-only PostgreSQL diagnostics to help engineers follow a reported failure from its request ID to a finding supported by evidence.

The project includes a fictional billing API with realistic authentication, invoice, and payment behavior. A separate scenario environment reproduces five support incidents, including revoked credentials, malformed requests, payment inconsistencies, database connectivity failures, and blocked transactions. This makes it possible to practice the investigation without touching a production service.

### Architecture

```mermaid
flowchart LR
    support([Support engineer])
    cli["supportops CLI<br/>diagnostics and investigations"]
    harness["supportops-lab<br/>scenario harness"]

    subgraph persistent["Persistent billing lab — compose.yaml"]
        api1["billing-api<br/>127.0.0.1:8001"]
        db1[("PostgreSQL<br/>127.0.0.1:5433")]
        api1 --> db1
    end

    subgraph scenario["Disposable scenario lab — compose.scenario.yaml"]
        api2["billing-api<br/>Docker-assigned port"]
        db2[("PostgreSQL on tmpfs<br/>Docker-assigned port")]
        api2 --> db2
    end

    support --> cli
    support --> harness
    cli -->|"HTTP checks, Docker logs,<br/>read-only SQL"| persistent
    cli -->|"same diagnostics via --env-file"| scenario
    harness -->|"verify ownership, reproduce incidents, reset"| scenario
```

The two environments serve different purposes. The persistent lab is useful for ordinary API and database exploration, while `supportops-lab` creates and manages a disposable environment for fault reproduction. The harness checks resource ownership before making changes and cannot manage the persistent lab. The diagnostic CLI remains independent of the harness; its database queries are read-only, and API writes require explicit approval against a verified local lab.

## Getting Started

### Requirements

- [uv](https://docs.astral.sh/uv/) for managing Python and project dependencies
- Git
- [Docker](https://www.docker.com/products/docker-desktop/) for the local lab, integration tests, and end-to-end scenarios

SupportOps uses Python 3.13. You don't need to install Python separately, as `uv` can download and manage the required version.

Docker isn't required for offline CLI commands or unit tests. You'll need it for the billing API, PostgreSQL, integration tests, and incident scenarios.

### Installation

Clone the repository:

```bash
git clone https://github.com/Levi-j/supportOps.git
cd supportOps
```

Install the project dependencies:

```bash
uv sync
```

This creates a virtual environment in `.venv` and installs the packages used by the project.

Next, create a local configuration file.

**Windows PowerShell:**

```powershell
Copy-Item .env.example .env
```

**Linux/macOS:**

```bash
cp .env.example .env
```

The example file contains the settings needed for the local lab, including development-only database credentials. You can change these values to match your environment.

If you already have a `.env` file from an earlier version of SupportOps, compare it with `.env.example` and add any missing settings rather than overwriting your existing configuration.

Check that the CLI works:

```bash
uv run supportops --help
```

Using `uv run` means you don't have to activate the virtual environment yourself.

## Using the CLI

This section covers the basics. The diagnostics commands are described in [Diagnosing the API](#diagnosing-the-api), after the local lab they work against.

### Viewing configuration

To see which settings SupportOps is currently using, run:

```bash
uv run supportops config show
```

The output looks something like this:

| Setting | Value | Source |
|---|---|---|
| SUPPORTOPS_TARGET | billing | env file |
| SUPPORTOPS_API_URL | http://127.0.0.1:8001/ | env file |
| SUPPORTOPS_API_KEY | bk_juniper01*** | env file |
| SUPPORTOPS_DB_URL | postgresql://supportops_ro:***@127.0.0.1:5433/billing | env file |
| SUPPORTOPS_CONNECT_TIMEOUT_SECONDS | 3.0 | default |
| SUPPORTOPS_HTTP_TIMEOUT_SECONDS | 5.0 | env file |
| SUPPORTOPS_SLOW_REQUEST_MS | 1000.0 | default |
| SUPPORTOPS_LOG_SOURCE | docker:supportops-billing-api-1 | env file |

Passwords and API keys are masked so they aren't accidentally exposed in terminal output. Database URLs still show useful details, such as the hostname, port, and username, but not the password.

The **Source** column shows where each setting came from. This can be useful when a configuration value isn't what you expected.

SupportOps checks environment variables first, then the `.env` file, and finally its built-in defaults.

You can also get the configuration as JSON:

```bash
uv run supportops config show --json
```

This is useful when you want to process the output with another tool or script.

### Global options

| Option | Description |
|---|---|
| `--help` | Show available commands and options |
| `--version` | Display the installed version |
| `--env-file PATH` | Load settings from a different environment file |
| `--debug` | Display additional information when troubleshooting errors |

Global options go before the command. For example:

```bash
uv run supportops --env-file other.env config show
```

## Configuration

SupportOps uses environment variables and an optional `.env` file for configuration.

The main settings are:

| Variable | Default | Description |
|---|---|---|
| `SUPPORTOPS_TARGET` | `billing` | Type of service being investigated |
| `SUPPORTOPS_API_URL` | `http://127.0.0.1:8001` | Base URL of the target API |
| `SUPPORTOPS_API_KEY` | Not set | API key used for authentication |
| `SUPPORTOPS_DB_URL` | Not set | PostgreSQL connection URL for health checks and read-only database diagnostics |
| `SUPPORTOPS_CONNECT_TIMEOUT_SECONDS` | `3` | How long to wait for a connection to open, in seconds |
| `SUPPORTOPS_HTTP_TIMEOUT_SECONDS` | `5` | How long to wait for the API to respond once connected, in seconds |
| `SUPPORTOPS_SLOW_REQUEST_MS` | `1000` | Response time above which `api latency` reports a problem, in milliseconds |
| `SUPPORTOPS_LOG_SOURCE` | `docker:supportops-billing-api-1` | Where the `logs` commands read from when no source is given |

The `.env.example` file also includes the database passwords used by the local Docker lab.

The examples use `127.0.0.1` instead of `localhost` to avoid unnecessary IPv6 connection attempts on some Windows systems. If you created `.env` from an older example, update the host in `SUPPORTOPS_API_URL` and `SUPPORTOPS_DB_URL`. Keep your existing database password unchanged.

Keep credentials in your environment or local configuration files rather than passing them directly as command-line arguments.

The `.env` file is excluded from Git, so your local settings won't be committed accidentally.

**Windows PowerShell 5.1 note:** Environment files should use UTF-8 encoding. PowerShell 5.1 can create UTF-16 files when using output redirection (`>`), which can cause configuration loading to fail. Copying `.env.example` is the simplest way to avoid this. SupportOps also detects UTF-16 files and explains the problem.

## Error Handling

SupportOps tries to make errors easy to understand without filling the terminal with unnecessary stack traces.

For example, if the API URL is invalid, you might see:

```text
Error: Invalid configuration.
SUPPORTOPS_API_URL: Input should be a valid URL.
Hint: Fix the value in the environment or the env file,
then run 'supportops config show'.
```

Unexpected errors are handled separately. By default, the CLI displays a short message explaining that something went wrong.

If you're investigating a problem and need more detail, use `--debug` to see the traceback.

Sensitive values are masked in both normal and debug output.

### Exit codes

SupportOps uses exit codes to indicate whether a command succeeded:

| Code | Meaning |
|---|---|
| `0` | Command completed successfully |
| `1` | The command ran, but found a problem or found nothing, such as an unhealthy service, an error response, or no matching log entries |
| `2` | Invalid command usage or configuration, including refused write requests |
| `3` | A diagnostic could not complete, such as when a log source or database was unavailable |

These codes are useful when running SupportOps from scripts or automated workflows.

To check the exit code in PowerShell:

```powershell
$LASTEXITCODE
```

On Linux or macOS:

```bash
echo $?
```

## Local Lab

SupportOps includes a small backend environment that you can run on your own machine.

It consists of two services:

- **Billing API:** A fictional invoicing service built with FastAPI.
- **PostgreSQL 18:** A database containing sample customers, invoices, payments, and related billing records.

The data is fictional, and the lab is designed for local development and troubleshooting.

Having a real API and database to work with makes it possible to reproduce problems, inspect logs, test database permissions, and practice diagnosing failures.

### Starting the lab

Make sure Docker Desktop or Docker Engine is running and that you've created your `.env` file.

From the root of the SupportOps repository, run:

```bash
docker compose up -d --build --wait
```

This builds the API image, starts both containers, and waits for their health checks to pass.

To see the running services:

```bash
docker compose ps
```

The services are available at:

| Service | Address |
|---|---|
| Billing API | http://localhost:8001 |
| PostgreSQL | `localhost:5433` |

Both ports are bound to `127.0.0.1`, so the services aren't exposed to other machines on your network. You can use either `localhost` or `127.0.0.1` in your browser and with curl, but SupportOps uses `127.0.0.1` because it connects faster on Windows (see [Configuration](#configuration)).

The billing API listens on port 8000 inside its container and is available on port **8001** on the host. The separate host port helps avoid conflicts with other local services.

You can explore the API's interactive documentation here:

[http://localhost:8001/docs](http://localhost:8001/docs)

### Checking the API

The API provides two health endpoints.

**Windows PowerShell:**

```powershell
curl.exe -i http://localhost:8001/health
curl.exe -i -H "X-Request-Id: my-first-check" http://localhost:8001/health/ready
```

**Linux/macOS:**

```bash
curl -i http://localhost:8001/health
curl -i -H "X-Request-Id: my-first-check" http://localhost:8001/health/ready
```

On Windows PowerShell, using `curl.exe` makes sure you're calling curl rather than PowerShell's `curl` alias.

The two endpoints check different things:

| Endpoint | Purpose | Checks PostgreSQL? |
|---|---|---|
| `GET /health` | Confirms the API process is running | No |
| `GET /health/ready` | Confirms the API can reach its database | Yes |

The difference matters when troubleshooting.

For example, the billing API might still be running even though PostgreSQL has stopped. In that situation:

- `/health` returns `200`, because the API process is alive.
- `/health/ready` returns `503`, because the API can't connect to its database.

Readiness errors also indicate the type of connection problem, such as `dns_failure`, `connection_refused`, `authentication_failed`, or `database_missing`.

This helps distinguish an application failure from a problem with one of its dependencies.

Docker's health check uses `/health` rather than `/health/ready`. That way, a database outage doesn't automatically make the API container appear unhealthy when restarting the API wouldn't fix the database.

### Request IDs and logging

Every API response includes an `X-Request-Id` header.

A request ID makes it easier to connect an API response to the log entries generated while handling that request.

You can provide your own ID:

```powershell
curl.exe -i -H "X-Request-Id: billing-check-1" http://localhost:8001/health/ready
```

The response will include the same ID, and the related logs will contain it as well.

If you don't provide one, the API generates one automatically.

Custom IDs can contain letters, numbers, underscores, and hyphens, up to 64 characters. Invalid IDs are replaced rather than echoed back or written to logs.

To view the logs:

```bash
docker compose logs billing-api
```

The billing API uses structured JSON logging, with each log entry written on a separate line.

For example:

```json
{
  "timestamp": "2026-10-08T14:07:26.574Z",
  "level": "INFO",
  "service": "billing-api",
  "logger": "billing_api.http",
  "message": "HTTP request",
  "request_id": "billing-check-1",
  "event_name": "http.request",
  "method": "GET",
  "path": "/health/ready",
  "status": 200,
  "duration_ms": 28,
  "user_agent": "curl/8.21.0"
}
```

Structured logs are easier to search and filter than free-form text, especially when investigating a particular request.

A few details are worth knowing:

- Each request produces one `http.request` access log entry.
- Logs include the URL path, but not query strings.
- When the API starts, an `app.started` entry records connection details such as the database host and username, without exposing the password.
- If an unexpected error occurs, the client receives a generic error response while the traceback is kept in the logs.

### Error responses

The API uses the standard [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) format for errors.

For example, requesting an endpoint that doesn't exist returns a `404` response with a body similar to:

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

These responses use the `application/problem+json` content type.

The error code helps identify what went wrong, and the request ID gives you a way to find the related logs.

### Database users and permissions

The lab uses separate PostgreSQL users instead of connecting everything with an administrator account.

| User | Purpose | Permissions |
|---|---|---|
| `lab_admin` | Initial database setup | Superuser |
| `billing_app` | Billing API database access | Read, insert, and update billing data |
| `supportops_ro` | SupportOps diagnostics | Read-only access and PostgreSQL monitoring views |

The `billing_app` user cannot delete records or change the database schema.

The `supportops_ro` user is even more restricted. It is intended for investigating database problems without accidentally changing application data.

Read-only access is enforced in two ways:

1. Database sessions are read-only by default.
2. The user has no write permissions on the billing tables.

You can test those restrictions yourself.

First, run a read-only query:

```powershell
docker compose exec postgres psql -U supportops_ro -d billing -c "SELECT count(*) FROM billing.invoices;"
```

The initial sample database contains nine invoices.

Now try deleting them:

```powershell
docker compose exec postgres psql -U supportops_ro -d billing -c "DELETE FROM billing.invoices;"
```

PostgreSQL rejects the command because the session is read-only:

```text
ERROR: cannot execute DELETE in a read-only transaction
```

Even if you try disabling the session's read-only setting, the user still doesn't have permission to delete anything:

```powershell
docker compose exec postgres psql -U supportops_ro -d billing -c "SET default_transaction_read_only = off" -c "DELETE FROM billing.invoices;"
```

The result is:

```text
ERROR: permission denied for table invoices
```

This is useful for a diagnostics tool because it provides protection against accidental changes while inspecting a database.

The database users, schema, and sample records are defined in `lab/sql/`. PostgreSQL runs those initialization scripts when it creates a new database volume.

### Stopping the lab

When you're finished, run:

```bash
docker compose down
```

This stops and removes the lab containers but keeps the database volume.

Your data will still be there the next time you start the lab.

### Resetting the lab

Sometimes you'll want to return the database to its original state, especially after testing failures or changing sample records.

A reset removes the SupportOps database volume and recreates the database using the SQL files in `lab/sql/`.

**Warning:** This deletes any changes you've made to the SupportOps lab database. Don't use it if there's data you want to keep.

The reset commands below explicitly target the `supportops` Compose project to avoid interfering with other Docker projects, including OrderFlow.

From the SupportOps repository root, first check which containers belong to the project:

```bash
docker compose --project-name supportops ps
```

Then inspect its volumes:

```bash
docker volume ls --filter label=com.docker.compose.project=supportops
```

For the standard setup, the database volume should be named `supportops_postgres-data`.

Once you've confirmed you're looking at the correct project and volume, remove the lab and its data:

```bash
docker compose --project-name supportops down --volumes --remove-orphans
```

Then rebuild it:

```bash
docker compose --project-name supportops up -d --build --wait
```

PostgreSQL will recreate the schema and load the original sample data.

The initialization scripts run when a new database volume is created. They don't automatically run again against an existing database.

## Using the Billing API

The billing API is the practice service that SupportOps will eventually troubleshoot. It has three fictional business accounts, each with its own customers and invoices. You can make requests, inspect responses, and follow those requests through the logs and database.

The API is available at [http://localhost:8001/docs](http://localhost:8001/docs) when the lab is running. The Swagger page lets you explore all eight endpoints and make requests without writing a client.

### Lab API keys

The sample database includes these keys:

| Key | Account | Status |
|---|---|---|
| `bk_juniper01_lab_only_not_a_real_key` | Juniper Dental Group | Active |
| `bk_juniper00_lab_only_not_a_real_key` | Juniper Dental Group | Revoked |
| `bk_kestrel01_lab_only_not_a_real_key` | Kestrel Logistics | Active |
| `bk_alderfin1_lab_only_not_a_real_key` | Alder & Finch Studio | Account suspended |

These are public test credentials for a local lab, not secrets for a real service. `.env.example` uses the active Juniper key. If you created `.env` during M2, compare the two files and update `SUPPORTOPS_API_KEY` in your local `.env` when you want to use the CLI with authenticated requests.

All `/v1` endpoints require an API key sent as a bearer token:

```text
Authorization: Bearer bk_juniper01_lab_only_not_a_real_key
```

In Swagger, select **Authorize** and enter `Bearer ` followed by an active lab key.

The API doesn't store the full key in PostgreSQL. It stores a 12-character prefix and a SHA-256 hash, then compares hashes in constant time. The prefix makes it possible to investigate an authentication problem without storing or logging the complete credential.

A missing, malformed, unknown, revoked, or expired key always gets the same `401 UNAUTHENTICATED` response. The API keeps the exact reason in an `auth.rejected` log entry, where a support engineer can investigate it without giving information to an unauthenticated caller. A valid key for a suspended account instead gets `403 ACCOUNT_SUSPENDED`.

### Available endpoints

| Method | Endpoint | What it does |
|---|---|---|
| `GET` | `/v1/account` | Identifies the account associated with the API key |
| `GET` | `/v1/customers` | Lists customers with `limit` and `offset` |
| `POST` | `/v1/customers` | Creates a customer |
| `GET` | `/v1/customers/{id}` | Retrieves one customer |
| `GET` | `/v1/invoices` | Lists invoices, with status, customer, and pagination filters |
| `POST` | `/v1/invoices` | Creates an invoice from line items |
| `GET` | `/v1/invoices/{id}` | Retrieves an invoice and its lines |
| `POST` | `/v1/invoices/{id}/pay` | Pays an open invoice in full |

List endpoints return results in a `data` array, along with `has_more` to indicate whether another page is available.

### Make a few requests

**Windows PowerShell:**

```powershell
$key = "bk_juniper01_lab_only_not_a_real_key"
curl.exe -s -H "Authorization: Bearer $key" http://localhost:8001/v1/account
curl.exe -s -H "Authorization: Bearer $key" "http://localhost:8001/v1/invoices?status=open"
```

**Linux/macOS:**

```bash
key="bk_juniper01_lab_only_not_a_real_key"
curl -s -H "Authorization: Bearer $key" http://localhost:8001/v1/account
curl -s -H "Authorization: Bearer $key" "http://localhost:8001/v1/invoices?status=open"
```

To create a customer, use the example request in `docs/examples/new-customer.json`:

**Windows PowerShell:**

```powershell
curl.exe -s -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" `
  --data-binary "@docs/examples/new-customer.json" http://localhost:8001/v1/customers
```

**Linux/macOS:**

```bash
curl -s -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
  --data-binary "@docs/examples/new-customer.json" http://localhost:8001/v1/customers
```

That last request **adds a customer to your local database**. The example files also include intentionally invalid bodies you can use to investigate `400` and `422` responses.

Using JSON files is especially helpful in Windows PowerShell 5.1, where passing JSON directly through `curl.exe` can strip double quotes.

### Keeping customers separate

The API takes the account ID from the verified key, not from a value supplied in the request. Every customer and invoice query is scoped to that account.

For example, Kestrel can't retrieve a Juniper invoice:

```powershell
curl.exe -i -H "Authorization: Bearer bk_kestrel01_lab_only_not_a_real_key" http://localhost:8001/v1/invoices/inv_juniper_1003
```

The response is `404 Not Found`, just as it would be for an invoice ID that doesn't exist. Returning `403` here would reveal that another customer's invoice exists.

An invoice cannot be created for a customer belonging to another account either; the API rejects that reference during validation.

### Invoices and payments

Invoice amounts are stored as **integer cents**. When you create an invoice, you supply its lines and unit prices; the server calculates the total. It won't accept a client-supplied `total_cents` field. See `docs/examples/invoice-with-total.json` for an example that is intentionally rejected.

New invoices start as `open` and receive a number within their account, such as `INV-1005`.

Paying an invoice does two things: it records a payment and changes the invoice status to `paid`. Normally, both changes happen in **one PostgreSQL transaction**. If something fails halfway through, neither change is saved.

The API also locks the invoice row while processing a payment. This prevents two normal requests from paying the same invoice at once. An invoice that's already paid, still a draft, or otherwise not payable returns `409 INVOICE_NOT_PAYABLE`.

If another transaction holds the row lock too long, the API returns `503 DATABASE_BUSY` with a `Retry-After` header. The default lock timeout is three seconds. Ordinary invoice reads can continue while the write is blocked.

### Understanding API errors

Errors follow the [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) format described above, including a request ID you can look up in the logs.

| HTTP status | Code | Meaning |
|---|---|---|
| `400` | `MALFORMED_REQUEST` | The request body isn't valid JSON |
| `401` | `UNAUTHENTICATED` | The key is missing or invalid |
| `403` | `ACCOUNT_SUSPENDED` | The account isn't allowed to make requests |
| `404` | `RESOURCE_NOT_FOUND` | The requested resource isn't available to this account |
| `409` | `INVOICE_NOT_PAYABLE` | The invoice can't be paid in its current state |
| `422` | `VALIDATION_FAILED` | A field is missing, invalid, or not supported |
| `500` | `INTERNAL_ERROR` | An unexpected server error occurred |
| `503` | `SERVICE_UNAVAILABLE` | A required dependency, such as PostgreSQL, is unavailable |
| `503` | `DATABASE_BUSY` | A database operation timed out, including lock contention |

The distinction between `400` and `422` is useful when helping someone debug an integration. A `400` means the body couldn't be parsed as JSON. A `422` means the JSON itself was valid, but didn't match the endpoint's expected fields.

Validation responses identify the problem field without copying the value the client sent. Unknown fields are rejected rather than silently ignored.

Try these examples (they should not create records):

```powershell
curl.exe -i -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" --data-binary "@docs/examples/broken-customer.json" http://localhost:8001/v1/customers
curl.exe -i -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" --data-binary "@docs/examples/customer-missing-email.json" http://localhost:8001/v1/customers
```

The first should return `400 MALFORMED_REQUEST`; the second should return `422 VALIDATION_FAILED`.

### Following a request through the logs

In addition to the access log, the API records events for authentication failures, invalid requests, business operations, and database problems.

| Event | What to look for |
|---|---|
| `auth.rejected` | Why a key was rejected, plus its safe prefix |
| `request.invalid_json` | JSON parsing error and position, without the body |
| `request.validation_failed` | Which fields failed validation |
| `customer.created` / `invoice.created` | IDs of newly created records |
| `payment.recorded` / `invoice.paid` | Successful payment processing |
| `payment.rejected` | An invoice couldn't be paid |
| `db.unavailable` | Connection or dependency failure |
| `db.lock_timeout` / `db.statement_timeout` | A database operation timed out |

Authenticated request logs include an `account_id`, so you can narrow an investigation to one account. Each request also has an `X-Request-Id` that ties its response to the related events.

For example, a revoked key generates an `auth.rejected` event with `reason: revoked_key`, but the caller still sees only the generic 401 error.

```powershell
docker compose logs billing-api --tail 50
```

SupportOps can also read these logs for you and put one request's entries in order; see [Investigating logs](#investigating-logs).

The API never writes full keys, Authorization headers, request bodies, or customer email addresses to its logs.

### Reproducing a broken payment (lab only)

The billing API includes one deliberate fault, `payment_partial_commit`, to demonstrate a payment inconsistency. With the fault enabled, the API records a payment but fails before marking the invoice paid. A retry can create a second payment record while the invoice remains open. This behavior is intentional and is separate from the API's normal atomic payment handling.

The fault works only in `BILLING_ENV=lab`; outside that environment, the service refuses to start with the fault enabled.

**Use the disposable scenario lab to reproduce this case**, rather than enabling the fault in the persistent billing lab. Incident INC-003 runs the payment attempts against an isolated database, captures the request IDs, and provides a complete investigation trail:

```powershell
uv run supportops-lab start INC-003
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc003-cust-02
```

See [INC-003: Payment recorded, invoice still open](docs/incidents/INC-003-payment-recorded-invoice-open.md) for the evidence and escalation notes. The corresponding integration tests also exercise the fault using disposable databases:

```bash
uv run pytest -m integration -k fault
```

Neither workflow requires modifying the persistent lab's invoice records or recreating its containers.

## Diagnosing the API

SupportOps provides three commands for day-to-day API troubleshooting: `health` checks availability, `api request` reproduces a call, and `api latency` measures response times. The examples below use the local billing lab. Start it first and make sure your `.env` points to the running service.

The CLI assigns an `X-Request-Id` to each HTTP request and shows it in the result. That gives you a reliable way to find the corresponding server logs.

### Check service availability

```bash
uv run supportops health
```

This checks the API's liveness (`/health`) and readiness (`/health/ready`) endpoints, then connects directly to PostgreSQL using a read-only account. Checking both paths to the database helps narrow down the cause of an outage.

| Verdict | Interpretation |
|---|---|
| `HEALTHY` | The API is live, ready, and able to use PostgreSQL. |
| `DEGRADED` | The API is running, but a dependency or readiness check has failed. |
| `DOWN` | The API cannot be reached. |
| `INCONCLUSIVE` | The checks returned insufficient or conflicting evidence. |

For example, if the API cannot reach PostgreSQL but SupportOps can connect directly, the API's configuration or network path deserves investigation. If both connections fail, a database outage is more likely. These are diagnostic clues, not automatic root-cause conclusions.

The output includes the result and duration of each check, relevant request IDs, and suggested next steps. The command does not change database records. It returns exit code `0` for `HEALTHY` and a nonzero code otherwise.

For scripts, use the JSON output:

```bash
uv run supportops health --json
```

See the [service availability runbook](docs/runbooks/service-availability.md) for a safe, supervised database-outage exercise.

### Reproduce an API request

```bash
uv run supportops api request GET /v1/account
```

SupportOps displays the HTTP status, elapsed time, request ID, relevant response headers, and response body. It interprets RFC 9457 Problem Details errors and offers a next step where possible. Credentials are masked in the output.

To investigate a missing-key response, send the request without authentication and give it a recognizable ID:

```bash
uv run supportops api request GET /v1/account --no-auth --request-id demo-auth-401
```

The API should return `401 UNAUTHENTICATED`. You can then find `demo-auth-401` in the billing API's logs to see the internal rejection reason, for example with `uv run supportops logs trace demo-auth-401`. The caller receives the generic error, while the server log contains the troubleshooting detail.

The most useful options are:

| Option | Purpose |
|---|---|
| `--request-id ID` | Supply a correlation ID instead of having one generated. |
| `--key-env NAME` | Read an API key from a named environment variable. |
| `--no-auth` | Omit the Authorization header. |
| `--data-file PATH` / `--data JSON` | Provide a request body, validated as JSON before sending. |
| `--raw` | Send the supplied body without JSON validation to reproduce malformed requests. |
| `--header "Name: value"` | Add a non-credential request header. |
| `--full` | Display more than the default 4,000-character response limit. |
| `--json` | Return machine-readable diagnostic output. |
| `--yes` | Explicitly approve a state-changing request against the local lab. |

API keys are read from configuration or environment variables rather than command-line arguments. Requests do not follow redirects automatically, so the reported response is the one the target API actually returned.

If inline JSON is malformed, SupportOps stops before sending it. This is particularly helpful on Windows PowerShell 5.1, where quoting JSON passed to native commands can be unreliable. Using `--data-file` is usually simpler.

### Make changes in the lab safely

SupportOps is primarily a diagnostic tool. A `POST`, `PUT`, `PATCH`, or `DELETE` request is allowed only when all three conditions are met:

1. You include `--yes`.
2. The target points to the local machine (`127.0.0.1`, `localhost`, or `::1`).
3. The service's `/health` response identifies the environment as `lab`.

The local-address and confirmation checks happen before sending the request. If the conditions are not met, SupportOps refuses the operation. It never automatically retries writes: after a timeout, you should check whether the original request succeeded before trying again.

For example, the following command **creates a customer in your local database**:

```bash
uv run supportops api request POST /v1/customers --data-file docs/examples/new-customer.json --yes
```

Leave out `--yes` when you only want to confirm that the safety guard works; the request will be rejected without creating anything.

### Measure response times

```bash
uv run supportops api latency /v1/invoices --count 20
```

This sends 20 sequential `GET` requests and reports the status-code distribution, median (`p50`), 95th-percentile (`p95`) and maximum response time. It also identifies the slowest requests by their IDs.

An example summary might look like:

```text
Responses: 200 x20
Successful: 20 of 20   p50 11.8 ms   p95 13.5 ms   max 18.2 ms
All requests succeeded within the threshold.
```

The times will depend on your machine and network. By default, the threshold is set by `SUPPORTOPS_SLOW_REQUEST_MS` (1,000 ms); you can override it with `--threshold-ms`. The command returns exit code `1` if a request fails, returns a non-2xx status, or the p95 exceeds the threshold.

Requests are sent one at a time, with a maximum count of 100. This is intended for lightweight troubleshooting, not load testing. On Windows, an unusually slow first request can also be caused by connection setup rather than the API itself.

### Troubleshooting runbooks

The runbooks provide fuller procedures, including PowerShell and Linux examples:

- [Service availability](docs/runbooks/service-availability.md) covers the health verdicts, connection failures, and a supervised PostgreSQL outage drill.
- [API errors and reproduction](docs/runbooks/api-errors-and-reproduction.md) covers HTTP errors, request IDs, malformed JSON, safe requests, and latency checks.
- [Logs and request IDs](docs/runbooks/logs-and-request-ids.md) covers reading structured logs, following one request, and investigating 401, 400/422 and 500 errors.
- [Database diagnostics](docs/runbooks/database-diagnostics.md) explains the SQL check catalog, invoice and payment consistency, long transactions, and lock contention.
- [Authentication](docs/runbooks/authentication.md) covers API-key failures, 401 and 403 responses, and how to confirm a rejection using logs.
- [Triage and escalation](docs/runbooks/triage-and-escalation.md) covers assessing incident impact, collecting evidence, escalating issues, and preparing customer updates.

## Investigating logs

An HTTP status tells you what the API returned, but not necessarily what happened inside the service. SupportOps can read the billing API's structured logs to find related events, spot recurring errors, and follow individual requests without searching through raw JSON by hand.

| Command | What it shows |
|---|---|
| `logs summary` | Activity by log level, event type, HTTP status, recurring error pattern, and slowest request |
| `logs search` | Entries matching a request ID, severity, event, status, path, text, or time range |
| `logs trace REQUEST_ID` | A chronological timeline of events recorded for one request |

These commands are read-only. They don't change the service, its database, or the original logs.

### Trace a failed request

For example, send a request without an API key and give it a recognizable ID:

```powershell
uv run supportops api request GET /v1/account --no-auth --request-id demo-auth-401
uv run supportops logs trace demo-auth-401
```

The first command intentionally receives `401 UNAUTHENTICATED`. The trace then brings together the server-side events for that request. A shortened example looks like this:

```text
2 log entries for request ID demo-auth-401 in docker:supportops-billing-api-1
Highlights: authentication
+0 ms  WARNING  auth.rejected  API key rejected
       reason=missing_header
+0 ms  INFO     http.request   GET /v1/account -> 401
```

The API response doesn't reveal why authentication failed; the `auth.rejected` event records that the Authorization header was missing. This distinction is useful when troubleshooting a customer's `401` without exposing credential details in the response.

Events appear in timestamp order, with offsets relative to the first matching log entry. An exception's stack trace is included when one was logged. The timeline is evidence, not an automatic root-cause diagnosis. If the request ID isn't found, `logs trace` exits with code `1`.

### Summarize activity and search for errors

```powershell
uv run supportops logs summary --since 15m
uv run supportops logs search --status 4xx --since 15m
uv run supportops logs search --event "auth.*" --path /v1/account
uv run supportops logs search --request-id demo-auth-401
```

`logs summary` counts events, levels and HTTP statuses, highlights slow requests, and groups similar warnings and errors into patterns. Variable IDs and numbers are normalized for grouping, while meaningful distinctions such as status codes and failure reasons remain separate. Treat these patterns as a starting point for investigation, not proof that every event has the same cause.

Search filters can be combined. `--level error` includes errors and more severe events; `--status` accepts either a code (`401`) or a class (`4xx`); and an event ending in `*` matches a prefix. Search returns the newest 50 matches by default, adjustable with `--limit`. Both `--since` and `--until` accept relative durations such as `15m` and ISO timestamps; the start is inclusive and the end exclusive.

The lab generates frequent `/health` requests, which can dominate a summary. Use **`logs search --path /v1`** to narrow results to API traffic. The `summary` command does not have a `--path` filter.

### Read logs from Docker, files, or stdin

By default, log commands use `SUPPORTOPS_LOG_SOURCE`, which points to `docker:supportops-billing-api-1` in the local lab. You can override it with a file path, `-` for standard input, or an explicit Docker container source:

```powershell
uv run supportops logs summary docker:supportops-billing-api-1 --since 1h
uv run supportops logs summary tests/fixtures/logs/billing-api.jsonl
docker compose --project-name supportops logs --no-log-prefix billing-api | uv run supportops logs summary -
```

The reader supports UTF-8 and UTF-16, including files produced by Windows PowerShell 5.1. Invalid or incomplete JSON lines are counted and skipped rather than stopping the whole operation. It recognizes the billing API's log fields and common ECS-style fields from other services.

Docker sources read up to the most recent 100,000 lines. If Docker isn't available, a container can't be found, or a file can't be read, SupportOps reports a source error rather than silently returning an empty result. Log output is redacted for common credentials and personal identifiers, including in JSON output and stack traces; as with any pattern-based masking, sensitive logs should still be handled carefully.

For more examples, see the [Logs and request IDs runbook](docs/runbooks/logs-and-request-ids.md).

## Checking the database

SupportOps includes a catalog of read-only PostgreSQL checks for investigating data inconsistencies and database performance problems. The CLI uses the connection configured in `SUPPORTOPS_DB_URL`; the local lab connects as `supportops_ro`, a role with read-only access to billing data.

Start by listing the available checks, then run the checks that do not require an invoice ID or key prefix:

```powershell
uv run supportops db checks
uv run supportops db run --all
```

The catalog covers ten checks:

| Check | Purpose |
|---|---|
| `db.connectivity` | Confirm connectivity, inspect the connected role and transaction settings, and flag excessive privileges. |
| `pg.connections` | Summarize database sessions by role, application, and state. |
| `pg.long_transactions` | Find transactions open longer than `min_seconds` (60 seconds by default). |
| `pg.blocking_sessions` | Identify sessions waiting on locks and the sessions blocking them. |
| `billing.invoice_total_mismatch` | Compare recorded invoice totals with their line items. |
| `billing.paid_invoice_without_payment` | Find paid invoices with no successful payment. |
| `billing.payment_on_unpaid_invoice` | Find successful payments attached to invoices that are not marked paid. |
| `billing.duplicate_payments` | Find invoices with more than one successful payment. |
| `billing.api_key_status` | Inspect key status and account metadata using a 12-character prefix. |
| `billing.invoice_lookup` | Look up an invoice by `id` or `number`, including its status, total, and payment counts. |

Checks that need an identifier accept it through `--param`. You can also inspect the predefined SQL before running a check:

```powershell
uv run supportops db run billing.invoice_lookup --param number=INV-1003
uv run supportops db checks billing.invoice_lookup --show-sql
uv run supportops db run --all --json
```

`--all` runs the applicable checks and marks parameter-dependent lookups as `SKIPPED`; it does not choose an invoice or API key on your behalf.

### Interpreting the results

| Status | Meaning |
|---|---|
| `PASS` | The check completed without finding the condition it was designed to detect. |
| `FAIL` | A potential problem was found, or a requested record was not found. |
| `WARN` | The database role has more privileges than are appropriate for routine diagnostics. |
| `INFO` | The check returned information for review rather than a pass/fail judgment. |
| `SKIPPED` | The check needs a parameter that was not supplied. |
| `ERROR` | The check could not run; its result should not be treated as a clean bill of health. |

The command exits with `0` when no failures or warnings are reported, `1` when a check returns `FAIL` or `WARN`, `2` for invalid usage, and `3` when a check cannot complete.

**Database access is intentionally restricted.** SupportOps executes predefined `SELECT` statements with bound parameters inside read-only transactions; it does not accept arbitrary SQL. The lab's `supportops_ro` role adds a separate permissions boundary. If a more privileged account is configured, `db.connectivity` warns about it rather than silently accepting it as the recommended setup.

See the [database diagnostics runbook](docs/runbooks/database-diagnostics.md) for SQL examples, investigating payment discrepancies, and interpreting PostgreSQL sessions and locks.

## Troubleshooting authentication

When an API request fails with `401` or `403`, `auth check` brings together three pieces of evidence: whether the configured API key looks correctly formatted, what the API returns from `GET /v1/account`, and what PostgreSQL knows about the key prefix.

To check the key in your current configuration:

```powershell
uv run supportops auth check
```

You can also investigate a different credential without putting the key itself in a command argument. For example, the local lab includes a deliberately revoked test key:

```powershell
$env:CUSTOMER_KEY = "bk_juniper00_lab_only_not_a_real_key"
uv run supportops auth check --key-env CUSTOMER_KEY
Remove-Item Env:CUSTOMER_KEY
```

The revoked key should receive the API's generic `401` response. Its database record also shows that the prefix belongs to a revoked key, but **the prefix alone cannot verify the full credential**. A mistyped key can share the same prefix, so SupportOps keeps that distinction clear instead of presenting a guess as a confirmed cause.

For an unsuccessful request, note the request ID printed by `auth check` and trace it in the logs:

```powershell
uv run supportops logs trace YOUR_REQUEST_ID
```

The corresponding `auth.rejected` event can identify a missing header, malformed credential, unknown key, revoked key, or expired key. A valid key associated with a suspended account instead produces `403`. Neither the full credential nor its stored hash is included in diagnostic output.

See the [authentication runbook](docs/runbooks/authentication.md) for a step-by-step investigation, common configuration mistakes, and guidance on handling credentials safely.

## Guided investigation

When a customer reports a failed API request, the request ID is a useful starting point. `supportops investigate` brings together the relevant logs, read-only database checks, and initial troubleshooting findings so the engineer can work from a single evidence trail.

To investigate a request using the configured log source:

```powershell
uv run supportops investigate REQUEST_ID
```

You can also investigate saved logs without querying a database. This example uses a request from the repository's sample log file:

```powershell
uv run supportops investigate demo-400 tests/fixtures/logs/billing-api.jsonl --no-db
```

The command reconstructs the request timeline, identifies relevant account and billing references, and selects diagnostic checks based on what the logs contain. For example, an authentication rejection can trigger an API-key status lookup, while an invoice-related payment failure can trigger a scoped invoice lookup and consistency checks. Database error events can also prompt service-health checks. No customer request is replayed.

### Findings and supporting evidence

Findings reference numbered evidence items (`E1`, `E2`, and so on), making it possible to check how each conclusion was reached. The confidence label reflects the strength of that evidence:

| Confidence | What it means |
|---|---|
| `confirmed` | The request's server-side events identify the cause, the recorded HTTP status agrees, and no evidence contradicts it. |
| `likely` | There is a direct indication of the cause, but part of the supporting evidence is missing or only reflects current conditions. |
| `possible` | The available signals suggest a cause without establishing it directly. |

Conflicting evidence lowers confidence and is called out explicitly. If the request cannot be found or the available information doesn't support a finding, the result is `INCONCLUSIVE` rather than an invented root cause.

The distinction between historical and current evidence matters. Logs describe the request as it happened; a database lookup describes the state of the data **when the investigation runs**. A key that is active now, for example, may have been revoked when the original request failed. The output keeps these observations separate from interpretations and suggested next steps.

### Assessing impact

For each relevant error pattern, SupportOps examines the 15 minutes before and after the investigated request and counts distinct request IDs and accounts, rather than counting every log entry as a separate failure.

The result also describes **log coverage**. If the window extends beyond the available logs, a source was truncated, lines were skipped, or the search was restricted by a time filter, the counts are treated as minimums. Incomplete coverage cannot support an `isolated` conclusion; the scope remains `undetermined` unless other affected requests are actually observed.

Impact estimates apply only to the sources examined. They are useful for initial triage, but they are not a service-wide incident metric.

### Options and report drafts

| Option | Purpose |
|---|---|
| `[SOURCE]...` | Read from explicit log files, stdin, or Docker instead of the configured source. |
| `--since`, `--until` | Restrict which log entries are considered. |
| `--no-db` | Skip database queries. |
| `--json` | Return the investigation as structured JSON. |
| `--report FILE` | Write a Markdown incident-report draft. |

For example, to save a draft based on the sample malformed-JSON request:

```powershell
uv run supportops investigate demo-400 tests/fixtures/logs/billing-api.jsonl --no-db --report reports/demo-400.md
```

The draft includes the request summary, findings, timeline, evidence, observed impact, outstanding questions, and recommended next steps or escalation. It is clearly marked for human review, and the command refuses to overwrite an existing report. The generated `reports/` directory is excluded from Git.

The report writer applies secret redaction and checks for configured credentials before writing, but generated reports still need review before they are shared. In particular, investigation data may contain internal account details that should not appear in a customer-facing update.

### Safety and limitations

`investigate` does not modify the target database. It uses only predefined, parameterized, read-only SQL checks; its HTTP probes are limited to unauthenticated `GET /health` and `GET /health/ready` requests. It does not send the configured API key or repeat the customer's original request.

`--no-db` disables database queries, **not all network access**: logs showing database failures can still trigger health GETs to the configured API. Keep that in mind when investigating historical logs against a live environment. Redaction is also a safeguard rather than a substitute for reviewing sensitive output.

| Exit code | Meaning |
|---|---|
| `0` | One or more findings were produced. |
| `1` | The investigation was inconclusive. |
| `2` | Invalid input, unsafe short configured credentials, or an existing report destination. |
| `3` | A required log source could not be read, or the report failed its secret check. |

For a full triage workflow, including severity, escalation, and customer updates, see the [triage and escalation runbook](docs/runbooks/triage-and-escalation.md). Use the [incident report template](docs/incidents/TEMPLATE.md) when preparing a reviewed incident write-up.

## Incident scenarios

The included scenarios start with problems a support engineer might receive from a customer. Each one reproduces the reported behavior in a disposable billing environment and produces request IDs that can be traced through the API logs and database checks.

The `supportops-lab` harness can rebuild that environment between investigations. It does not operate on the persistent `supportops` database.

### Reproduce and investigate an incident

With Docker running, open Windows PowerShell in the repository root:

```powershell
uv run supportops-lab scenarios
uv run supportops-lab up
uv run supportops-lab start INC-003
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc003-cust-02
uv run supportops-lab status
```

`scenarios` shows the available cases. The `start` command begins with a fresh disposable lab, applies the selected fault, and replays the customer's requests. It prints the observed responses and request IDs along with a command for investigating them. Running the same scenario again starts from a fresh set of sample data.

| Incident | Customer report | Finding |
| --- | --- | --- |
| [INC-001: Revoked API key](docs/incidents/INC-001-revoked-api-key.md) | An integration suddenly receives `401` responses. | `key_revoked` (confirmed). The customer needs to update the credential used by the integration. |
| [INC-002: Malformed JSON from PowerShell](docs/incidents/INC-002-powershell-malformed-json.md) | Creating a customer fails with `400` when the request is sent from PowerShell. | `malformed_json` (confirmed). Reproducing the outgoing request reveals a client-side quoting problem. |
| [INC-003: Payment recorded, invoice still open](docs/incidents/INC-003-payment-recorded-invoice-open.md) | Two payment attempts return `500`, but both payments are recorded and the invoice remains open. | `payment_invoice_inconsistent` and `unhandled_exception` (confirmed). High-severity engineering escalation. |
| [INC-004: Wrong database host after maintenance](docs/incidents/INC-004-db-misconfigured.md) | Requests fail with `503` even though the API process is running. | `api_cannot_reach_database` (confirmed). Readiness fails while direct PostgreSQL access succeeds; escalate to the deployment owner or on-call engineer. |
| [INC-005: Payments blocked by an open transaction](docs/incidents/INC-005-blocked-writes.md) | Payments time out with `503 DATABASE_BUSY`, but invoice reads still work. | `lock_contention` (confirmed). A transaction left open by a backfill process is holding a row lock; escalate to Engineering / DBA. |

The [incident reports](docs/incidents/README.md) document the customer's symptoms, reproduction steps, HTTP responses, log and SQL evidence, investigation findings, and recommended resolution. All five cases are simulated; they do not represent incidents from a real customer environment.

You can also save an internal investigation draft. For example:

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc003-cust-02 --report reports\INC-003.md
```

Reports can include internal identifiers and other operational context, even after automatic redaction. Review them before sharing, and prepare a separate customer-facing update where appropriate. The report command will not overwrite an existing file.

When you're done, either restore the clean scenario baseline or remove the disposable environment:

```powershell
uv run supportops-lab reset
uv run supportops-lab down
```

`reset` recreates the scenario database and verifies service health, consistency checks, and the absence of lingering lock waits or long transactions. `down` removes the scenario containers and their temporary data. Neither command resets the persistent `supportops` lab.

### Sample investigation: a payment blocked by a database transaction

In INC-005, a customer reports that payments repeatedly return a database-busy error, although they can still retrieve their invoices. That combination suggests the service is available for reads but that a write operation is waiting on something in PostgreSQL.

Reproduce the incident in the disposable scenario lab:

```powershell
uv run supportops-lab start INC-005
```

The harness creates a backfill session that holds a row lock, then sends two payment attempts and an invoice read. The following is an excerpt from an actual scenario run:

```text
  inc005-cust-01  POST /v1/invoices/inv_kestrel_2002/pay -> 503 DATABASE_BUSY, Retry-After 5 (expected 503 DATABASE_BUSY)
  inc005-cust-02  GET /v1/invoices/inv_kestrel_2002 -> 200 (expected 200)
  inc005-cust-03  POST /v1/invoices/inv_kestrel_2002/pay -> 503 DATABASE_BUSY, Retry-After 5 (expected 503 DATABASE_BUSY)
Lock wait captured at 2026-10-09T07:46:33.143+00:00 with pg.blocking_sessions while inc005-cust-01 was waiting:
  session 183 (billing-api, billing_app) waiting 0 s for Lock:transactionid, blocked by session 113 (invoice-backfill, idle in transaction, transaction open 11 s)
```

The lock-wait snapshot matters because a waiting session can disappear from `pg.blocking_sessions` as soon as its request times out. Capturing the relationship while the request is in flight preserves evidence that would otherwise be unavailable during a later investigation.

Use the first failed request ID to investigate:

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc005-cust-01
```

A shortened excerpt from that investigation shows the finding and the evidence behind it:

```text
FINDINGS  The request timed out waiting for a database lock (confirmed).

1. The request timed out waiting for a database lock  [CONFIRMED]
   Interpretation (not verified):
     - Most likely blocker: session 113 (invoice-backfill, idle in transaction, transaction open 26 s, 7 locks held). This is the database's current state, linked to the request only by time.
     - Invoice inv_kestrel_2002 is still open with 0 successful payments, and this request logged no payment.recorded event, so this attempt doesn't appear to have taken a payment.
   Escalate to Engineering / DBA (high): Database sessions holding locks can only be handled by engineering or a DBA.

Evidence
  E1  log entry at 2026-10-09T07:46:36.014Z, docker:supportops-scenario-billing-api-1:16
      WARNING db.lock_timeout: Database lock timeout [detail=canceling statement due to lock timeout]
  E2  log entry at 2026-10-09T07:46:36.015Z, docker:supportops-scenario-billing-api-1:17
      INFO http.request: POST /v1/invoices/inv_kestrel_2002/pay -> 503 (3,023 ms) [account_id=acct_kestrel]
  E3  database check at 2026-10-09T07:46:47.811Z, pg.long_transactions
      Transactions open longer than 10 seconds: 1. session 113 (invoice-backfill, idle in transaction, transaction open 26 s, 7 locks held).
```

The request's `db.lock_timeout` event and matching HTTP `503` establish that this payment attempt timed out waiting for a database lock. A later SQL check shows that the `invoice-backfill` session is still open and holding locks. The earlier snapshot directly shows the backfill session blocking an API payment query; its association with this particular request relies on the timing of the capture, which the report makes explicit.

The invoice remained open, with no successful payment recorded. The appropriate next step is to hold off on further payment attempts and escalate the blocking transaction to Engineering / DBA for review. SupportOps collects the evidence but does not terminate database sessions.

The complete [INC-005 incident report](docs/incidents/INC-005-blocked-writes.md) includes the reproduction, investigation, escalation handoff, and draft customer response.

### Isolation and safety

Scenario runs are deliberately separate from the persistent development lab. They use `compose.scenario.yaml` and the `supportops-scenario` Docker Compose project, rather than `compose.yaml` and the persistent `supportops` project.

The isolation is enforced at several levels:

- **Resources and data:** Scenario containers and networks are separate from the persistent lab. Their PostgreSQL database uses tmpfs storage, so it has no persistent Docker volume.
- **Network access:** Docker assigns loopback-only host ports. The harness verifies those bindings and rejects the persistent lab's reserved ports, `8001` and `5433`.
- **Credentials and configuration:** Scenario settings are generated under `.lab/supportops-scenario/`, which is excluded from Git and Docker builds. The harness does not read or replace the normal `.env`.
- **Ownership:** Before changing scenario resources, the harness checks the project, expected services, ownership labels, and unique lab ID. SQL setup targets the verified scenario PostgreSQL container, and customer requests target the verified scenario API.
- **Cleanup:** If the recorded ownership information does not match the resources found in Docker, the harness refuses automatic deletion and provides recovery instructions. It does not use broad volume or orphan-removal flags.

To investigate a scenario, include `--env-file .lab\supportops-scenario\supportops.env`. This selects the scenario API, database, and log source rather than the persistent lab. Shell environment variables can take precedence over an environment file, so check the effective target if you have `SUPPORTOPS_*` variables set. `supportops-lab status` warns about conflicting values.

The generated `.lab/` files contain temporary credentials in plain text. Keep them local and out of reports or commits, even though the directory is Git-ignored.

INC-004 and INC-005 exercise two additional failure modes. INC-004 recreates only the disposable API with an incorrect database hostname; the replacement must pass ownership and port checks before use. INC-005 leaves a transaction open inside the verified disposable PostgreSQL container so its effects can be observed and investigated.

Use `supportops-lab reset` to remove either fault and restore a verified baseline. Plain `docker compose` commands, unless explicitly scoped to the scenario project and file, refer to the persistent lab.

INC-003 also writes inconsistent payment records, but only within the disposable database. The scenario harness never applies faults to the persistent development data.

## Development

SupportOps uses:

- **pytest** for automated tests
- **Ruff** for linting and formatting
- **mypy** for static type checking
- **GitHub Actions** for continuous integration

### Unit tests

Run the regular test suite with:

```bash
uv run pytest
```

These tests don't require Docker.

### Integration tests

The integration tests check behavior against a real PostgreSQL 18 database:

```bash
uv run pytest -m integration
```

Docker must be running for these tests.

The tests start a temporary PostgreSQL container and initialize it using the same SQL scripts as the local lab. They don't use or modify the lab's existing database.

Tests that call the billing API get their own fresh copy of the sample database, cloned from a pristine template, and the copy is dropped afterwards. That way, a test that creates invoices or records payments can't affect any other test.

This lets the tests verify database roles, permissions, sample data, authentication, tenant isolation, payments, lock timeouts, and the lab fault without relying on the state of your running lab.

Integration tests also start the billing API on a temporary local port and exercise the diagnostics against it. This checks the CLI's behavior with a real API and PostgreSQL without using your running lab.

The log sources are tested the same way: a short-lived container prints log lines for `docker logs` to read, and a separate SupportOps process reads UTF-16 input from standard input.

Database diagnostics are tested against those disposable PostgreSQL instances. The tests introduce invoice and payment discrepancies, create blocked sessions, and check read-only enforcement under both restricted and privileged roles. They also verify how diagnostics behave when monitoring access is limited. None of these tests changes the persistent local lab.

Guided investigation tests exercise authentication failures, malformed requests, payment inconsistencies, lock contention, and database connectivity problems against disposable API and PostgreSQL instances. They verify the findings and evidence, including that running an investigation does not alter application data.

### End-to-end tests

The end-to-end suite runs all five incidents against a real billing API and a disposable PostgreSQL database:

```bash
uv run pytest -m e2e
```

Docker is required. The tests use a separate Compose project, `supportops-scenario-e2e`, and their own temporary state. They do not read your normal `.env`, connect to the persistent billing database, or reuse a manually started scenario lab.

For each incident, the suite starts from clean sample data, reproduces the customer requests, checks the expected findings and supporting evidence, and verifies the escalation decision. It then resets the environment and confirms that the baseline is healthy.

INC-004 is tested both from the API's perspective and through a direct database connection: the API fails readiness while PostgreSQL remains reachable. INC-005 tests the captured lock wait, the continuing blocking transaction, the investigation's findings, and the absence of a successful payment. Other lifecycle tests cover ownership checks, safe cleanup, and report redaction.

The suite cleans up its resources after a normal run or a handled failure. An abrupt termination can leave disposable resources behind; if their ownership state cannot be verified, the harness stops rather than deleting them automatically.

### Code quality checks

Run Ruff's linting checks:

```bash
uv run ruff check .
```

Check formatting:

```bash
uv run ruff format --check .
```

Run mypy:

```bash
uv run mypy
```

These checks also run in GitHub Actions when changes are pushed or a pull request is opened against `main`.

CI also runs PostgreSQL integration tests, validates both Compose configurations, and builds the billing API image. A separate end-to-end job runs the five incidents on a GitHub-hosted runner using a disposable scenario project. Cleanup uses the same ownership checks as the local harness.

## Project Structure

```text
src/
├── supportops/             # Diagnostic CLI
│   ├── cli/                # CLI commands
│   ├── db/                 # Read-only PostgreSQL check catalog
│   ├── investigation/      # Evidence, findings and Markdown reports
│   ├── logs/               # Log sources, parsing, search and tracing
│   ├── auth_checks.py      # Authentication diagnostics
│   ├── health.py           # Service health checks
│   ├── http_checks.py      # API request diagnostics
│   ├── latency.py          # Response-time measurement
│   ├── write_guard.py      # Guardrails for explicit API writes
│   ├── targets.py          # Service-specific endpoint definitions
│   ├── settings.py         # Configuration and validation
│   ├── redaction.py        # Sensitive data masking
│   ├── render.py           # Terminal and JSON output
│   └── errors.py           # Error handling and exit codes
└── supportops_lab/         # Disposable scenario harness
    ├── cli.py              # Lab management commands
    ├── lab.py              # Lab lifecycle
    ├── scenarios.py        # Incident definitions
    ├── customer.py         # Customer request simulator
    ├── contention.py       # Lock-holding session and lock-wait capture
    ├── ownership.py        # Resource verification
    └── ...                 # Compose, state and path helpers
lab/
├── Dockerfile              # Billing API container image
├── pyproject.toml          # Billing API dependencies
├── sql/                    # Roles, schema and seed records
└── src/billing_api/        # FastAPI service, auth and billing logic
docs/
├── examples/               # API request bodies
├── incidents/              # Incident index, template and INC-001 to INC-005
└── runbooks/               # Troubleshooting guides
tests/
├── fixtures/               # Logs and golden report fixtures
├── unit/                   # CLI, API and harness unit tests
├── integration/            # Real API and PostgreSQL integration tests
└── e2e/                    # Reproducible scenario tests
compose.yaml                # Persistent local billing lab
compose.scenario.yaml       # Isolated, disposable scenario lab
```

The diagnostic CLI and scenario harness are separate packages: `supportops` does not import `supportops_lab`. The billing API is a separate package in the same uv workspace, so the CLI does not depend on the web framework.

The API code is split by responsibility: `routes.py` handles HTTP requests, `billing.py` contains business rules and transaction handling, and `repository.py` holds parameterized SQL. That keeps the endpoints relatively small and makes the behavior easier to test.

The billing API opens a database connection for each request rather than using a pool. For a small diagnostics lab, this keeps connection failures easy to classify—for example, distinguishing a DNS error from a refused connection. A higher-traffic production service would normally use connection pooling.

The API's Docker image is built to include only the packages the service needs, and the application runs inside the container as a non-root user.

## Next steps

SupportOps now covers the full investigation workflow, from an initial customer report to correlated API and database evidence, a confidence-qualified finding, and a documented response or engineering handoff. Its five reproducible incidents span authentication failures, malformed requests, payment consistency, service availability, and PostgreSQL lock contention.

An optional future integration could extend the diagnostics to another backend service. The existing billing lab and all five scenarios remain self-contained.
