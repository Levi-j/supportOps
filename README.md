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
| SUPPORTOPS_API_KEY | bk_placehold*** | env file |
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

This lets the tests verify database roles, permissions, sample data, and connection handling without relying on the state of your running lab.

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

tests/
├── unit/                   # CLI and API unit tests
└── integration/            # PostgreSQL integration tests

compose.yaml                # Local PostgreSQL and billing API services
```

The CLI and the billing API are separate Python packages in the same uv workspace.

The API's Docker image is built to include only the packages the service needs, and the application runs inside the container as a non-root user.

## What's Next

SupportOps is being built in stages.

The CLI foundation and local billing environment are in place. The next steps are to add more realistic billing operations and build the diagnostics that will investigate them.

The planned features include API health and authentication checks, structured log analysis, PostgreSQL diagnostics, and guided incident investigations.

The goal is to build a tool that doesn't just report that something failed, but helps explain **what failed, where to look, and what might have caused it**.