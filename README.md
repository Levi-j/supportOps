# SupportOps

SupportOps is a Python command-line toolkit for troubleshooting backend services and investigating application incidents.

The goal is to make common support tasks easier, from checking service health and investigating API failures to analyzing logs, diagnosing database issues, and tracking down the cause of an incident.

## Getting Started

### Requirements

- [uv](https://docs.astral.sh/uv/) — manages Python versions and project dependencies.
- Git

SupportOps uses Python 3.13. You don't need to install Python separately; `uv` can download and manage the required version automatically.

### Installation

Clone the repository and navigate into it:

```bash
git clone https://github.com/Levi-j/supportOps.git
cd supportOps
```

Install the dependencies:

```bash
uv sync
```

This creates a virtual environment in `.venv` and installs the packages required by the project.

Next, create your local configuration file.

**Windows PowerShell:**

```powershell
Copy-Item .env.example .env
```

**Linux/macOS:**

```bash
cp .env.example .env
```

You can now run the CLI:

```bash
uv run supportops --help
```

Using `uv run` means you don't have to activate the virtual environment manually.

## CLI Usage

### View configuration

The `config show` command displays the configuration currently being used by SupportOps:

```bash
uv run supportops config show
```

Example output:

| Setting | Value | Source |
|---|---|---|
| SUPPORTOPS_TARGET | billing | env file |
| SUPPORTOPS_API_URL | http://localhost:8000 | env file |
| SUPPORTOPS_API_KEY | bk_placehold*** | env file |
| SUPPORTOPS_DB_URL | postgresql://supportops_ro:***@localhost:5433/billing | env file |
| SUPPORTOPS_HTTP_TIMEOUT_SECONDS | 5.0 | env file |

Sensitive values are masked automatically. API keys show only a short prefix, while database URLs hide passwords but keep useful connection details visible.

The **Source** column helps identify whether a setting came from an environment variable, the `.env` file, or a built-in default. This is useful when troubleshooting configuration issues.

For machine-readable output, use:

```bash
uv run supportops config show --json
```

### Global options

| Option | Description |
|---|---|
| `--help` | Display available commands and options |
| `--version` | Show the installed SupportOps version |
| `--env-file PATH` | Load configuration from a different environment file |
| `--debug` | Show detailed tracebacks when investigating unexpected errors |

Global options should be placed before the command.

For example:

```bash
uv run supportops --env-file other.env config show
```

## Configuration

SupportOps reads settings from environment variables and an optional `.env` file.

Environment variables take priority over values in `.env`. If neither provides a value, SupportOps uses the default when one is available.

| Variable | Default | Description |
|---|---|---|
| `SUPPORTOPS_TARGET` | `billing` | Type of backend service being investigated |
| `SUPPORTOPS_API_URL` | `http://localhost:8000` | Base URL of the target API |
| `SUPPORTOPS_API_KEY` | Not set | API key used for authentication |
| `SUPPORTOPS_DB_URL` | Not set | PostgreSQL connection URL |
| `SUPPORTOPS_HTTP_TIMEOUT_SECONDS` | `5` | HTTP request timeout in seconds |

Credentials should be kept in environment variables or local configuration files, not passed directly as command-line arguments.

The `.env` file is excluded from Git to prevent accidentally committing local credentials.

**Note for Windows PowerShell 5.1:** Environment files must use UTF-8 encoding. PowerShell 5.1 can create UTF-16 files when using output redirection (`>`), so copying `.env.example` is the recommended approach. SupportOps detects UTF-16 configuration files and displays an error explaining the issue.

## Error Handling

SupportOps aims to provide useful error messages without overwhelming the terminal with stack traces.

For example, an invalid API URL produces an error similar to:

```text
Error: Invalid configuration.
SUPPORTOPS_API_URL: Input should be a valid URL.

Hint: Fix the value in the environment or the env file,
then run 'supportops config show'.
```

Unexpected errors are handled separately. By default, the CLI displays a short message. Use `--debug` when more information is needed.

Sensitive information is masked in both normal and debug output.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Command completed successfully |
| `2` | Invalid command usage or configuration |
| `3` | Command failed because of an unexpected error |

Exit codes make it easier to use SupportOps in scripts and automated workflows.

In PowerShell, check the exit code with `$LASTEXITCODE`. On Linux or macOS, use `echo $?`.

## Development

The project uses pytest for testing, Ruff for linting and formatting, and mypy for static type checking.

Run the unit tests:

```bash
uv run pytest
```

Check code quality:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The same checks run automatically through GitHub Actions on pushes and pull requests to `main`.

## Project Structure

The current structure focuses on the CLI foundation:

```text
src/
└── supportops/
    ├── cli/              # CLI commands and entry points
    ├── settings.py       # Configuration loading and validation
    ├── redaction.py      # Sensitive-data masking
    ├── render.py         # Terminal and JSON output
    └── errors.py         # Error handling and exit codes

tests/
└── unit/                 # Unit tests
```

As development continues, SupportOps will expand to include API diagnostics, structured log analysis, PostgreSQL checks, and guided incident investigations.
