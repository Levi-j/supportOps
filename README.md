# SupportOps

SupportOps is a Python command-line toolkit for troubleshooting backend services and investigating application incidents.

The idea is to bring common support tasks into one place: checking whether an API is healthy, understanding why requests are failing, reading application logs, inspecting database problems, and eventually putting those findings together to identify the cause of an incident.

The project also includes a small billing service and PostgreSQL database that run locally in Docker. They provide a controlled environment for testing diagnostics and reproducing common backend failures without relying on a real production system.

## Getting Started

### Requirements

- [uv](https://docs.astral.sh/uv/) for managing Python and project dependencies

- Git

- [Docker](https://www.docker.com/products/docker-desktop/) for running the local lab and integration tests

SupportOps uses Python 3.13. You don't need to install Python separately, as `uv` can download and manage the required version.

Docker isn't needed for the basic CLI commands, but you'll need it to run the billing API, PostgreSQL database, and integration tests.

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

### Viewing configuration

To see which settings SupportOps is currently using, run:

```bash
uv run supportops config show
```

The output looks something like this:

| Setting | Value | Source |
|---|---|---|
| SUPPORTOPS_TARGET | billing | env file |
| SUPPORTOPS_API_URL | http://localhost:8001 | env file |
| SUPPORTOPS_API_KEY | bk_juniper01*** | env file |
| SUPPORTOPS_DB_URL | postgresql://supportops_ro:***@localhost:5433/billing | env file |
| SUPPORTOPS_HTTP_TIMEOUT_SECONDS | 5.0 | env file |

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
| `SUPPORTOPS_API_URL` | `http://localhost:8001` | Base URL of the target API |
| `SUPPORTOPS_API_KEY` | Not set | API key used for authentication |
| `SUPPORTOPS_DB_URL` | Not set | PostgreSQL connection URL |
| `SUPPORTOPS_HTTP_TIMEOUT_SECONDS` | `5` | Timeout for HTTP requests, in seconds |

The `.env.example` file also includes the database passwords used by the local Docker lab.

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
| `2` | Invalid command usage or configuration |
| `3` | An unexpected error occurred |

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

Both ports are bound to `127.0.0.1`, so the services aren't exposed to other machines on your network.

The billing API uses port 8000 inside its Docker container, but Docker makes it available on port **8001** on your computer. This avoids a conflict with OrderFlow, which uses port 8000.

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

The API never writes full keys, Authorization headers, request bodies, or customer email addresses to its logs.

### Reproducing a broken payment (lab only)

The API includes one intentional fault, `payment_partial_commit`, to reproduce a billing problem that would normally require an engineering investigation.

With this fault enabled, the API saves a payment and then fails before updating the invoice. The client gets `500`, but the invoice still appears unpaid. Retrying adds another payment. This is **deliberately incorrect behavior**, separate from the normal atomic payment implementation.

The fault is **off by default** and works only when `BILLING_ENV=lab`. The API refuses to start if a fault is enabled in another environment, and it logs a `lab.fault_enabled` warning when the lab fault is active.

The safest way to see this behavior is through the isolated integration tests, which use disposable databases and leave your local lab data alone:

```bash
uv run pytest -m integration -k fault
```

You can also try it in the running lab. **Use a newly created invoice, not one of the seeded invoices**, and remember this leaves extra payment records in the local database:

```powershell
$env:BILLING_FAULTS = "payment_partial_commit"
docker compose up -d --wait billing-api

$key = "bk_juniper01_lab_only_not_a_real_key"
$invoice = curl.exe -s -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" `
  --data-binary "@docs/examples/new-invoice.json" http://localhost:8001/v1/invoices | ConvertFrom-Json

curl.exe -i -X POST -H "Authorization: Bearer $key" http://localhost:8001/v1/invoices/$($invoice.id)/pay
curl.exe -i -X POST -H "Authorization: Bearer $key" http://localhost:8001/v1/invoices/$($invoice.id)/pay
curl.exe -s -H "Authorization: Bearer $key" http://localhost:8001/v1/invoices/$($invoice.id)
```

Both payment requests should fail with `500`, and the invoice should still be `open`, even though payments were recorded. Check the logs and the payment table to investigate what happened.

**Turn the fault off when you're done:**

```powershell
Remove-Item Env:BILLING_FAULTS
docker compose up -d --wait billing-api
```

Removing the environment variable alone doesn't change a container that's already running. The second command recreates the API with the fault disabled. It does not reset the database.

If you want to remove the extra lab records, use the [reset procedure](#resetting-the-lab) after confirming that you don't need any data in the SupportOps database volume.

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

The CI workflow also runs PostgreSQL integration tests, validates the Docker Compose configuration, and builds the billing API image.

## Project Structure

```text
src/
└── supportops/             # Main SupportOps CLI
    ├── cli/                # Commands and CLI entry points
    ├── settings.py         # Configuration loading and validation
    ├── redaction.py        # Masking passwords and other sensitive data
    ├── render.py           # Terminal and JSON output
    └── errors.py           # Error handling and exit codes
lab/
├── Dockerfile              # Billing API Docker image
├── pyproject.toml          # Dependencies for the lab service
├── sql/                    # Database roles, schema and sample data
└── src/
    └── billing_api/        # FastAPI billing service
        ├── main.py         # Application setup
        ├── routes.py       # /v1 endpoints
        ├── auth.py         # API-key authentication
        ├── billing.py      # Customers, invoices and payments
        ├── repository.py   # SQL queries, always scoped to one account
        ├── errors.py       # Problem Details responses and error mapping
        ├── faults.py       # Lab-only fault injection
        └── ...             # Settings, logging, middleware, health checks
docs/
└── examples/               # Example JSON request bodies
tests/
├── unit/                   # CLI and API unit tests
└── integration/            # PostgreSQL and API integration tests
compose.yaml                # Local PostgreSQL and billing API services
```

The CLI and billing API are separate Python packages in the same uv workspace. This keeps the CLI independent of the web framework and its dependencies.

The API code is split by responsibility: `routes.py` handles HTTP requests, `billing.py` contains business rules and transaction handling, and `repository.py` holds parameterized SQL. That keeps the endpoints relatively small and makes the behavior easier to test.

The billing API opens a database connection for each request rather than using a pool. For a small diagnostics lab, this keeps connection failures easy to classify—for example, distinguishing a DNS error from a refused connection. A higher-traffic production service would normally use connection pooling.

The API's Docker image is built to include only the packages the service needs, and the application runs inside the container as a non-root user.

## Next builds

The CLI foundation and local billing service are in place, including authenticated accounts, customers, invoices, payments, request tracing, and a lab-only payment fault.

Next comes the main purpose of SupportOps: building the CLI diagnostics that investigate the service. These will cover API health, authentication issues, structured logs, PostgreSQL checks, and eventually guided incident investigations.

The goal is to build a tool that doesn't just report that something failed, but helps explain **what failed, where to look, and what might have caused it**.