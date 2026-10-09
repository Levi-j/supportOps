# Runbook: Service Availability

Use this runbook when the billing API appears unavailable, responds slowly, or fails across multiple endpoints. The aim is to identify where the failure is happening before deciding what to fix or who to involve.

The questions:

1. Can you reach the API?
2. Is it ready to handle requests, or is the process merely running?
3. If readiness fails, can SupportOps reach PostgreSQL independently?

That distinction matters. Restarting an API won't fix a database outage, and restarting PostgreSQL won't fix an incorrect connection string in the API.

## Start with a health check

From the SupportOps repository, run:

```powershell
uv run supportops health
$LASTEXITCODE
```

On Linux or macOS, use `echo $?` to check the exit code.

The command makes two HTTP `GET` requests and runs a single query through a read-only PostgreSQL connection. It does not change application data.

A healthy result reports three checks:

| Check | Healthy result | What it establishes |
|---|---|---|
| API liveness | `GET /health` → `200 OK` | The API process is responding |
| API readiness | `GET /health/ready` → `200 OK` | The API can reach its database |
| PostgreSQL | Reachable through a read-only session | The support machine can reach the database directly |

SupportOps displays response times and an `X-Request-Id` for each HTTP check. Those IDs can be used to find the corresponding requests in the API logs.

Use `uv run supportops health --json` if you need structured output for a script or incident record.

**Exit codes:** `0` means `HEALTHY`; `1` means the command completed with another verdict; `2` indicates invalid configuration.

## Interpreting the verdict

SupportOps compares three pieces of evidence rather than relying on one health endpoint:

- **Liveness:** `/health` shows whether the API process is responding. It does not query PostgreSQL.
- **Readiness:** `/health/ready` checks whether the API can reach PostgreSQL and reports connection errors.
- **Direct database access:** SupportOps connects from your machine using `SUPPORTOPS_DB_URL` and the read-only `supportops_ro` account.

| Verdict | Diagnosis | Evidence | Next step |
|---|---|---|---|
| `HEALTHY` | `api_ready` | Liveness and readiness both return `200` | Investigate the specific failing request |
| `DEGRADED` | `api_cannot_reach_database` | Readiness returns `503`, but PostgreSQL answers SupportOps | Check the API's database configuration and network path |
| `DEGRADED` | `database_outage` | Readiness returns `503`, and SupportOps cannot reach PostgreSQL either | Check the database service and its connectivity |
| `DEGRADED` | `readiness_failing_cause_unknown` | Readiness fails, but the direct database check cannot establish a cause | Check the database URL and connection details |
| `DEGRADED` | `readiness_unanswered` | The API is live, but readiness does not respond | Check API logs, timeouts, and dependency delays |
| `DOWN` | `api_unreachable` | No HTTP response arrives from liveness | Check the API process, port, and network path |
| `INCONCLUSIVE` | `unexpected_liveness_response` | Liveness returns an unexpected HTTP status | Confirm that `SUPPORTOPS_API_URL` targets the intended service |
| `INCONCLUSIVE` | `unexpected_readiness_response` | Readiness returns a status outside the expected behavior | Inspect the response directly |
| `INCONCLUSIVE` | `contradictory_readiness` | Readiness returns `503` while reporting the database as up | Compare the response with the API logs |

These are diagnostic conclusions, not guaranteed root causes. For example, a database may be running but inaccessible from one network path.

**A useful distinction:** If PostgreSQL rejects the username or password used by SupportOps, the server has still answered the connection attempt. That's different from a database host that cannot be reached. Check the credentials in `SUPPORTOPS_DB_URL` separately; a failed support login does not prove the API has the same problem.

An `INCONCLUSIVE` verdict is intentional. It means the available evidence doesn't justify a more specific diagnosis.

## When the API cannot be reached

If the request fails before receiving an HTTP response, SupportOps reports a transport failure instead of an HTTP status.

| Failure | What happened | Where to look |
|---|---|---|
| `dns_failure` | The hostname could not be resolved | Check the hostname, DNS, and VPN configuration |
| `connection_refused` | The host rejected the connection to that port | Confirm that the service is running and the published port is correct |
| `connect_timeout` | The connection could not be established before the deadline | Check routing, firewalls, address, and host availability |
| `read_timeout` | The connection succeeded, but the response took too long | Check application logs, dependency delays, and database locks |
| `tls_error` | The TLS connection or certificate validation failed | Confirm the URL scheme and certificate configuration |
| `connection_closed` | The connection ended before the response was complete | Check for a crash, restart, or proxy interruption |

The CLI uses separate settings for connection and response timeouts:

- `SUPPORTOPS_CONNECT_TIMEOUT_SECONDS` defaults to `3`.
- `SUPPORTOPS_HTTP_TIMEOUT_SECONDS` defaults to `5`.

Keeping the timeouts separate helps distinguish a connection problem from a service that accepts connections but responds slowly.

### Check the service directly

These commands help confirm what SupportOps is reporting.

**Windows PowerShell:**

```powershell
docker compose ps
curl.exe -i http://127.0.0.1:8001/health
curl.exe -i http://127.0.0.1:8001/health/ready
Test-NetConnection 127.0.0.1 -Port 5433
docker compose logs billing-api --tail 50
docker compose logs billing-api | Select-String db.unavailable
```

**Linux/macOS:**

```bash
docker compose ps
curl -i http://127.0.0.1:8001/health
curl -i http://127.0.0.1:8001/health/ready
nc -zv 127.0.0.1 5433
docker compose logs billing-api --tail 50
docker compose logs billing-api | grep db.unavailable
```

For DNS problems, use `Resolve-DnsName <hostname>` on Windows or `getent hosts <hostname>` on Linux. Replace `<hostname>` with the actual host from your configuration.

In Windows PowerShell 5.1, use `curl.exe` rather than `curl`, which is an alias for `Invoke-WebRequest`.

### Why the lab uses `127.0.0.1`

The lab publishes its ports on IPv4 loopback. On the Windows machine used to test M4, connecting through `localhost` caused an initial IPv6 attempt and noticeably slower connections.

| Connection | Observed using `localhost` | Observed using `127.0.0.1` |
|---|---:|---:|
| First API request | About 2,000 ms | Under 20 ms |
| PostgreSQL connection | About 3,000 ms | About 15 ms |
| `curl.exe` request | About 200 ms | About 2 ms |

These measurements are from that local setup, not general performance guarantees.

For the SupportOps lab, prefer `http://127.0.0.1:8001` in `SUPPORTOPS_API_URL` and `127.0.0.1:5433` in `SUPPORTOPS_DB_URL`. If your `.env` still contains `localhost`, update just the hostname; keep your existing database credentials.

## When the API is running but not ready

A live API with failed readiness usually points to a dependency or configuration issue.

### PostgreSQL is reachable from SupportOps

If the diagnosis is `api_cannot_reach_database`, the database answered from the support machine, but the API couldn't use it. Start with the application's configuration and network path.

Check recent database errors and the configuration recorded at startup. Both commands only read the configured log source (`SUPPORTOPS_LOG_SOURCE`):

```powershell
uv run supportops logs search --event db.unavailable
uv run supportops logs search --event app.started
```

The startup event includes the database host, port, name, and username without exposing the password. `supportops investigate REQUEST_ID` cites it automatically when a request failed with `db.unavailable`.

**Container networking is a common cause.** Inside the billing API container, `localhost` refers to that container, not the PostgreSQL container. The correct database host in this Compose environment is `postgres`, on port `5432`.

Changing the configuration and restarting the API is the service owner's decision. If you make a configuration change to the **persistent local lab** yourself, recreate only its API service, naming the project explicitly so the command can't reach anything else:

```powershell
docker compose --project-name supportops up -d billing-api
```

Then run `uv run supportops health` again. Do not apply the same restart procedure blindly to a shared or production environment.

### Practise it safely: INC-004

[INC-004](../incidents/INC-004-db-misconfigured.md) reproduces this failure in the disposable scenario lab, never in the persistent `supportops` lab:

```powershell
uv run supportops-lab start INC-004
uv run supportops --env-file .lab\supportops-scenario\supportops.env health
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc004-cust-01
uv run supportops-lab reset
```

Expect `DEGRADED` with diagnosis `api_cannot_reach_database` and exit code `1`:
- liveness `200`;
- readiness `503` with `connection_refused`;
- PostgreSQL reachable from the support machine.

Docker still reports the API container as `healthy`, because its health check only calls `/health`.

Use `supportops-lab reset` to recover the scenario lab. Plain `docker compose` commands without `--project-name` and `--file compose.scenario.yaml` target the persistent lab instead.

### PostgreSQL is unreachable from both sides

If the diagnosis is `database_outage`, inspect the database service first:

```powershell
docker compose --project-name supportops ps postgres
docker compose --project-name supportops logs postgres --tail 50
```

Check whether PostgreSQL stopped, failed its health check, or has a connection problem.

For a database operated by another team, escalate with the health-check output and relevant timestamps. Include what the API reported and what the direct connection test found.

## Lab drill: PostgreSQL outage

This exercise deliberately stops PostgreSQL to demonstrate the difference between **liveness** and **readiness**.

**Local SupportOps lab only.** The commands target the `supportops` Compose project and stop only its `postgres` service. `docker compose stop` retains the container and database volume; it does not delete customers, invoices, or payments.

The API will be unable to access PostgreSQL during the drill. Avoid running it while other work depends on the lab. Do not use `docker compose down`, `--volumes`, `rm`, or Docker volume-cleanup commands as part of this exercise.

Run the following from `C:\dev\supportOps`.

### 1. Confirm the lab is healthy

```powershell
docker compose --project-name supportops ps
uv run supportops health
$LASTEXITCODE
```

Confirm that `supportops-postgres-1` and `supportops-billing-api-1` are healthy. The CLI should report `HEALTHY` and exit code `0`.

### 2. Stop PostgreSQL

```powershell
docker compose --project-name supportops stop postgres
```

This affects the SupportOps database only. The OrderFlow project is separate.

### 3. Check the diagnosis

```powershell
uv run supportops health
$LASTEXITCODE
```

Expected: `DEGRADED`, diagnosis `database_outage`, and exit code `1`.

In this Docker lab, the API may report `dns_failure` when the stopped PostgreSQL container is no longer resolvable by service name. A direct connection from Windows may report a timeout; on other systems it may be refused. The important evidence is that neither path can reach the database.

### 4. Compare liveness and readiness

```powershell
curl.exe -i http://127.0.0.1:8001/health
curl.exe -i http://127.0.0.1:8001/health/ready
docker compose --project-name supportops logs billing-api --tail 20
```

Expected:

- `/health` returns `200` because the API process is still running.
- `/health/ready` returns `503` because the database is unavailable.
- The application logs contain database connectivity errors, including `db.unavailable`.

This is why restarting the API is not the appropriate fix for a stopped database.

### 5. Restore PostgreSQL

```powershell
docker compose --project-name supportops start postgres
docker compose --project-name supportops ps
```

Wait for `supportops-postgres-1` to report `healthy`.

**If you stop the drill early, run the `start postgres` command before doing anything else.**

### 6. Verify recovery

```powershell
uv run supportops health
$LASTEXITCODE
```

Expected: `HEALTHY` and exit code `0`.

The API opens a new database connection for each request, so it should recover once PostgreSQL accepts connections. No API restart or database reset should be necessary.

If recovery fails, inspect the database logs:

```powershell
docker compose --project-name supportops logs postgres --tail 50
```

Do not reset the database as a routine troubleshooting step. A reset removes the lab's stored changes; the [README reset procedure](../../README.md#resetting-the-lab) explains the consequences if a reset is ever genuinely needed.
