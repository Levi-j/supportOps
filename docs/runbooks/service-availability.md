# Runbook: Service Availability

Use this runbook when the billing API is unreachable, fails readiness checks, or returns errors across several endpoints. The aim is to establish **what failed and from where**, before deciding who should act.

Three questions guide the investigation: Is the API responding? Is it ready to serve requests? Can SupportOps reach PostgreSQL independently? A running API can have an unavailable dependency, and two clients can have different paths to the same database.

## Start with a health check

From the repository root:

```powershell
uv run supportops health
$LASTEXITCODE
```

The billing target checks two HTTP endpoints and, if `SUPPORTOPS_DB_URL` is configured, opens a separate read-only PostgreSQL connection.

| Check | Healthy result | What it tells you |
| --- | --- | --- |
| Liveness | `GET /health` → `200` | The API process responds |
| Readiness | `GET /health/ready` → `200` | The API can reach its database |
| Direct PostgreSQL probe | Connection succeeds | PostgreSQL answers from the machine running SupportOps |

The output includes request IDs, response times, a verdict, and suggested next steps. For a structured result, use `uv run supportops health --json`.

Exit code `0` means `HEALTHY`; `1` means the check completed with another verdict; `2` means invalid configuration.

## Interpreting the verdict

The following diagnoses are for the **billing target**. OrderFlow has different Actuator health responses and [target-specific diagnoses](../integrations/orderflow.md#health).

| Verdict | Diagnosis | Evidence | Next step |
| --- | --- | --- | --- |
| `HEALTHY` | `api_ready` | Liveness and readiness pass | Investigate the specific failed request |
| `DEGRADED` | `api_cannot_reach_database` | Readiness fails, but direct PostgreSQL access succeeds | Check the API's database configuration and network path |
| `DEGRADED` | `database_outage` | Readiness fails and the direct database probe can't connect | Inspect PostgreSQL availability and connectivity |
| `DEGRADED` | `readiness_failing_cause_unknown` | Readiness fails without enough evidence of why | Review the readiness body and service logs |
| `DEGRADED` | `readiness_unanswered` | API is live, but readiness doesn't answer | Check dependency delays and timeouts |
| `DOWN` | `api_unreachable` | No response from liveness | Check the process, address, and published port |
| `INCONCLUSIVE` | `unexpected_liveness_response` | Unexpected liveness status | Confirm the configured target |
| `INCONCLUSIVE` | `unexpected_readiness_response` | Unexpected readiness status | Inspect the raw response |
| `INCONCLUSIVE` | `contradictory_readiness` | Readiness status conflicts with its reported database state | Compare logs and direct database observations |

A diagnosis describes the evidence; it isn't necessarily a root cause. For example, PostgreSQL may answer SupportOps but reject the application's credentials. Conversely, failure from this machine doesn't establish what the API's network path can reach.

**Authentication errors are different from connection failures.** If PostgreSQL rejects the diagnostic username or password, the database did respond. Verify the support role's configuration before treating that result as an outage.

## When the API cannot be reached

A transport failure has no HTTP status. SupportOps classifies it separately:

| Failure | Likely area to inspect |
| --- | --- |
| `dns_failure` | Hostname, DNS, VPN, or service-name configuration |
| `connection_refused` | Service process or published port |
| `connect_timeout` | Firewall, routing, address, or unavailable host |
| `read_timeout` | Slow application or dependency, including locks |
| `tls_error` | URL scheme and certificate validation |
| `connection_closed` | Application restart, crash, or proxy interruption |

By default, `SUPPORTOPS_CONNECT_TIMEOUT_SECONDS` is `3` and `SUPPORTOPS_HTTP_TIMEOUT_SECONDS` is `5`. These separate connection establishment from waiting for a response.

### Check the service directly

The following commands **only inspect** the persistent billing lab:

```powershell
docker compose --project-name supportops ps
curl.exe -i http://127.0.0.1:8001/health
curl.exe -i http://127.0.0.1:8001/health/ready
Test-NetConnection 127.0.0.1 -Port 5433
docker compose --project-name supportops logs billing-api --tail 50
```

For a suspected dependency failure:

```powershell
uv run supportops logs search --event db.unavailable
uv run supportops logs search --event app.started
```

Use `curl.exe` in Windows PowerShell 5.1 because `curl` can resolve to `Invoke-WebRequest`. On Linux or macOS, use `curl`; for a quick TCP port check, use `nc -zv 127.0.0.1 5433` if available.

### Why the lab uses `127.0.0.1`

The lab publishes its ports on IPv4 loopback. On some Windows systems, `localhost` may first resolve to IPv6 and delay the connection. Using `127.0.0.1:8001` for the API and `127.0.0.1:5433` for PostgreSQL removes that ambiguity. If you change only these hostnames in `.env`, preserve your existing credentials.

## When the API is running but not ready

A liveness `200` combined with readiness `503` is a useful clue: the application process is responding, but a dependency or readiness condition is failing. It doesn't mean restarting the API will help.

### PostgreSQL is reachable from SupportOps

If the diagnosis is `api_cannot_reach_database`, inspect the API's own configuration and the `db.unavailable` events. The `app.started` event records the configured database host, port, name, and user without printing the password.

Inside the billing API container, `localhost` means **that container**, not the PostgreSQL service. The Compose service hostname is `postgres`, on its internal port `5432`; the host machine uses `127.0.0.1:5433`.

Do not restart or reconfigure the persistent lab as part of routine diagnosis. Any correction should be deliberate and handled by the service owner.

### Practise it safely: INC-004

[INC-004](../incidents/INC-004-db-misconfigured.md) reproduces the API-side connection failure in an **ownership-verified disposable scenario lab**:

```powershell
uv run supportops-lab up
uv run supportops-lab start INC-004
uv run supportops --env-file .lab\supportops-scenario\supportops.env health
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc004-cust-01
uv run supportops-lab reset
```

Expected: liveness `200`, readiness `503`, PostgreSQL reachable from the support machine, and diagnosis `api_cannot_reach_database`. The fault is an incorrect database hostname **inside the disposable API**, not a stopped PostgreSQL server.

The harness manages only verified scenario resources. It does not reset the persistent billing lab, and a bare `docker compose` command from the repository root must not be used as a substitute for `supportops-lab`.

### PostgreSQL is unreachable from both sides

If readiness is failing and SupportOps cannot reach PostgreSQL, check the database service's state and recent logs without changing it:

```powershell
docker compose --project-name supportops ps postgres
docker compose --project-name supportops logs postgres --tail 50
```

Capture the timestamps, connection failures, and affected request IDs. If another team operates the database, escalate these observations rather than making changes yourself.

## Lab drill: PostgreSQL outage

For routine practice, **do not stop the persistent `supportops` PostgreSQL container**. Doing so interrupts the running billing lab and may affect other work. A disposable scenario provides a safer way to investigate lost database connectivity.

For a safe hands-on exercise, use [INC-004](#practise-it-safely-inc-004) above. It reproduces a loss of database connectivity **from the API's perspective** while leaving PostgreSQL available to SupportOps. This demonstrates why liveness can pass while readiness fails; it does **not** reproduce a full PostgreSQL shutdown or the `database_outage` branch where both paths fail.

A true database-outage drill should be performed only in a separately provisioned, explicitly disposable environment with an approved recovery procedure—not against an active shared or persistent lab.

For the distinction between detecting a problem and changing system state, see the [database diagnostics runbook](database-diagnostics.md) and [incident report INC-004](../incidents/INC-004-db-misconfigured.md).
