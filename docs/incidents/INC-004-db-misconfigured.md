# INC-004: API returns 503 after a database configuration change

> **Simulated incident.** This case was reproduced in the disposable SupportOps scenario lab on 2026-10-09. The harness first started a healthy billing API and database, then recreated **only the scenario API** with its database host set to `localhost`. PostgreSQL remained available throughout. No real maintenance window or production outage occurred.

## Incident details

| Field | Detail |
| --- | --- |
| Incident | INC-004 |
| Date reproduced (UTC) | 2026-10-09 |
| Service | Billing API / PostgreSQL |
| Severity | High — database-backed API requests fail; see the [severity guide](../runbooks/triage-and-escalation.md#severity-guide) |
| Status | Diagnosed; simulated deployment-owner handoff |
| Request IDs | `inc004-cust-01`, `inc004-cust-02` |
| Simulated customer | Juniper Dental Group |
| Finding | `api_cannot_reach_database` — confirmed |
| Escalation | Deployment owner / on-call |

## Customer report

> "Since the maintenance window, every API request has been returning 503. The service appears to be running, but nothing works."

The initial question is whether the API itself is down, its database is down, or the two services can no longer communicate. A running process is not necessarily a ready service.

## Expected versus observed behavior

| Check | Expected | Observed |
| --- | --- | --- |
| `GET /v1/invoices` and `GET /v1/account` with a valid key | `200` | Both returned `503 SERVICE_UNAVAILABLE` at approximately `07:45:48.96Z` |
| `GET /health` | `200` | `200` |
| `GET /health/ready` | `200` | `503`; database connection refused |
| PostgreSQL connection from SupportOps | Reachable | Reachable with the read-only `supportops_ro` role |
| Docker container health | Running and healthy | Reported healthy because the container check uses liveness, not readiness |

Both customer requests failed before authentication could complete. Although the simulator used the Juniper integration, the server logs for these requests do not identify an account.

## Reproduction

Run the following from the repository root with Docker available:

```powershell
uv run supportops-lab start INC-004
```

The harness applies the bad hostname **after** validating a healthy disposable baseline. It verifies the ownership of the replacement API container and updates the generated scenario connection settings before sending customer traffic.

Selected output from the recorded run:

```text
INC-004: Every API request returns 503 after a maintenance window
...
The billing API was recreated with database host localhost.
Health check after the change: DEGRADED (api_cannot_reach_database): The API is running but can't use its database (the API reports: connection_refused), while PostgreSQL answers from this machine. This points to the API's database configuration or to the network path between the API and PostgreSQL.
Customer requests sent to the scenario lab at http://127.0.0.1:61028:
  inc004-cust-01  GET /v1/invoices -> 503 SERVICE_UNAVAILABLE (expected 503 SERVICE_UNAVAILABLE)
  inc004-cust-02  GET /v1/account -> 503 SERVICE_UNAVAILABLE (expected 503 SERVICE_UNAVAILABLE)
Investigate:
  uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc004-cust-01
  uv run supportops --env-file .lab\supportops-scenario\supportops.env health
Recover with 'uv run supportops-lab reset'; it recreates only the scenario lab.
```

The ports in this transcript were assigned by Docker for this particular run. They will differ when the scenario is recreated. The normal persistent billing lab, its configuration, and its database volume are not involved.

## Investigation

### 1. Compare liveness, readiness, and database connectivity

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env health
```

Recorded result (abridged):

```text
DEGRADED  The API is running but can't use its database (the API reports: connection_refused), while PostgreSQL answers from this machine. This points to the API's database configuration or to the network path between the API and PostgreSQL.
│ API liveness  │ GET /health -> 200 OK                                                                                            │ 5 ms  │ supportops-0ccdec49272a │
│ API readiness │ GET /health/ready -> 503 Service Unavailable (database: down, connection_refused)                                │ 3 ms  │ supportops-fbce88746d03 │
│ PostgreSQL    │ reachable as supportops_ro@127.0.0.1:61022/billing (PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2), read-only session) │ 13 ms │                         │
Next steps
  - Find the API's database errors: supportops logs search --event db.unavailable
  - Check the API's database host, port, name and credentials. Inside a container, localhost means the container itself, not the database.
  - After the API's database settings are corrected, whoever operates the service must restart or redeploy it; then run 'supportops health' again.
```

The `DEGRADED` result is more useful than the container's healthy status. The API is responding to `/health`, but its readiness endpoint cannot establish a database connection. Meanwhile, SupportOps can reach PostgreSQL directly.

That rules out a complete database outage in this lab and narrows the investigation to the application's connection configuration or its network path. The health command exits with status `1`, indicating a detected problem.

### 2. Check PostgreSQL directly

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env db run db.connectivity
```

```text
PASS     db.connectivity  Connected to billing as supportops_ro (PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2)). The session is read-only.  (3 ms)
```

This is an independent read-only connection to the scenario database, not a query routed through the billing API. It confirms that PostgreSQL is reachable from the support machine; it does **not** prove that the API has the correct connection settings.

### 3. Trace the reported request

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc004-cust-01
```

The investigation produced the following evidence (output shortened to the relevant lines):

```text
Investigation of request inc004-cust-01
Logs: docker:supportops-scenario-billing-api-1 (22 lines, 22 entries)
Request: GET /v1/invoices -> 503 in 24 ms at 2026-10-09T07:45:48.964Z
Database: not needed for this request
API health: checked at 2026-10-09T07:45:59.121Z
FINDINGS  The API can't reach its database, but PostgreSQL is up (confirmed).
1. The API can't reach its database, but PostgreSQL is up  [CONFIRMED]
   The API couldn't use its database while handling the request (error: connection_refused): connection failed: connection to server at "127.0.0.1", port 5432 failed: Connection refused.
   Evidence: E1, E2, E3, E4
   Interpretation (not verified):
     - Health check now: The API is running but can't use its database (the API reports: connection_refused), while PostgreSQL answers from this machine. ...
     - The API started with database host 'localhost', and its logs come from a Docker container. Inside a container, localhost is the container itself, not the database, which produces exactly this error.
   Caveats:
     - The health check shows the state when the investigation ran, not at the time of the request.
   Escalate to Deployment owner / on-call (high): The API can't use a database that answers support, which points to the API's configuration or network path.
Evidence
  E1  log entry at 2026-10-09T07:45:48.964Z, docker:supportops-scenario-billing-api-1:11
      ERROR db.unavailable: Database unavailable [error=connection_refused detail=connection failed: connection to server at "127.0.0.1", port 5432 failed: Connection refused]
  E2  log entry at 2026-10-09T07:45:48.964Z, docker:supportops-scenario-billing-api-1:12
      INFO http.request: GET /v1/invoices -> 503 (24 ms)
  E3  API health check at 2026-10-09T07:45:59.121Z, GET /health, GET /health/ready and a PostgreSQL probe
      DEGRADED (api_cannot_reach_database): ... Readiness answered 503. The API reports its database as down (connection_refused). PostgreSQL from this machine: reachable.
  E4  log entry at 2026-10-09T07:45:46.208Z, docker:supportops-scenario-billing-api-1:3
      INFO app.started: Billing API started [environment=lab database_host=localhost database_port=5432 database_name=billing database_user=billing_app db_statement_timeout_ms=10000 db_lock_timeout_ms=3000 faults=]
```

The request's `db.unavailable` event and matching HTTP `503` establish the failure. The startup event supplies the strongest explanation for it: the API was configured to connect to `localhost:5432`.

The health probe in E3 happened after the customer request. Its result confirms the condition still existed when the investigation ran, but it is not a historical measurement of the request itself.

### 4. Check whether the failure recurred

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env logs search --event db.unavailable
```

```text
6 matching entries in docker:supportops-scenario-billing-api-1
Filters: event db.unavailable
2026-10-09T07:45:48.589Z  WARNING   db.unavailable  request_id=supportops-1be7c98d4883  Database unavailable  error=connection_refused ...
2026-10-09T07:45:48.964Z  ERROR     db.unavailable  request_id=inc004-cust-01  Database unavailable  error=connection_refused ...
2026-10-09T07:45:48.967Z  ERROR     db.unavailable  request_id=inc004-cust-02  Database unavailable  error=connection_refused ...
2026-10-09T07:45:57.552Z  WARNING   db.unavailable  request_id=supportops-fbce88746d03  Database unavailable  error=connection_refused ...
2026-10-09T07:45:59.127Z  WARNING   db.unavailable  request_id=supportops-943038fbca79  Database unavailable  error=connection_refused ...
2026-10-09T07:46:00.006Z  WARNING   db.unavailable  request_id=supportops-f1420729b484  Database unavailable  error=connection_refused ...
```

The two `ERROR` entries belong to the simulated customer requests. The `WARNING` entries came from readiness probes performed by the harness or SupportOps. They are relevant to diagnosis but must not be counted as additional customer failures.

## Evidence

| Ref | Source (UTC) | Observation |
| --- | --- | --- |
| E1 | API log `:11`, `07:45:48.964Z` | `db.unavailable` for `inc004-cust-01`; connection refused at `127.0.0.1:5432` |
| E2 | API log `:12`, `07:45:48.964Z` | `GET /v1/invoices` returned `503` in 24 ms |
| E3 | Health check, `07:45:59.121Z` | Liveness `200`, readiness `503`; PostgreSQL reachable from the support machine |
| E4 | API startup log `:3`, `07:45:46.208Z` | `database_host=localhost`, database port `5432`, user `billing_app` |

E1, E2, and E4 were recorded by the API. E3 describes the state observed later by the investigator.

## Root cause and confidence

**Confirmed finding — `api_cannot_reach_database`:** The API could not establish its PostgreSQL connection while handling the reported request. Its own log identifies `connection_refused`, and the access log records the corresponding `503`. The direct database probe shows that PostgreSQL was still accessible from outside the API container.

**Root-cause interpretation:** The startup log shows `database_host=localhost`. Inside the API container, that hostname resolves to the API container itself, not to the separate PostgreSQL service. No database server was listening at `127.0.0.1:5432` in that container, so the connection was refused. In this Docker Compose deployment, the correct hostname is `postgres`.

The harness deliberately introduced this configuration, so the mechanism is known in the simulation. In a real incident, the deployment owner would still need to confirm the active configuration and compare it with the previous deployment before declaring the configuration change the root cause.

**Confidence:** Confirmed for the *database connection failure*. The incorrect hostname is an evidence-supported explanation, explicitly distinguished from the finding returned by SupportOps.

## Impact

In this configuration, endpoints that require a database connection cannot complete, although liveness continues to return `200`. The recorded evidence demonstrates two failed customer requests: `inc004-cust-01` and `inc004-cust-02`.

SupportOps examined a 30-minute impact window, from approximately `07:30:48Z` to `08:00:48Z`. It counted two distinct customer request IDs with the database-unavailable pattern; the access-log signature for `GET /v1/invoices -> 503` covered one. Authentication did not complete, so the server could not attribute the failures to an account.

**Coverage was partial.** The replacement container's logs started about 14 minutes into the analysis window, so the counts are lower bounds for the logs available. They should not be presented as measurements of a real production incident.

## Resolution or workaround

There is no client-side fix for the database connection setting, and repeated requests will encounter the same failure until it is corrected.

The deployment owner should:

1. Confirm the active database URL and replace `localhost` with the correct Docker service hostname, `postgres`, for this Compose environment.
2. Restart or redeploy the affected API through the service's approved deployment process.
3. Verify `supportops health` reports `HEALTHY`, including readiness `200` and successful direct database connectivity.
4. Confirm that a database-backed endpoint responds successfully before asking the customer to retry.

The read-only SupportOps diagnostics do not change configuration or restart applications.

**For this simulated incident only**, use:

```powershell
uv run supportops-lab reset
```

The harness recreates its disposable containers with the correct database host and verifies the clean baseline. Do not use unqualified `docker compose` recovery commands: from the repository root, those refer to the persistent `supportops` project rather than this scenario environment.

## Escalation

**Owner:** Deployment owner / on-call; **priority:** High

The handoff should include the two request IDs (`inc004-cust-01`, `inc004-cust-02`), their `503` responses at `07:45:48Z`, the connection-refused error at `127.0.0.1:5432`, the liveness/readiness discrepancy, direct PostgreSQL connectivity, and the `app.started` evidence showing `database_host=localhost` (E1–E4).

**Requested action:** Validate the deployed database configuration, correct it through the deployment process, and report back once readiness and a database-backed API request succeed.

## Customer update

> Hello,
>
> Thank you for reporting the failed requests. We've confirmed that the API is running but cannot currently connect to its database, which explains the `503` responses you're seeing.
>
> We've passed the connection details to our on-call team for correction. There is no change needed to your integration at this stage, and we recommend holding off on retries until we've verified the service is responding normally.
>
> We'll provide another update by **[agreed UTC time]**, or sooner if service is restored.
>
> Best regards,\
> Support team

*Draft for a simulated customer. No message was sent.*

## Prevention and follow-up

| Action | Owner | Status |
| --- | --- | --- |
| Use readiness checks as deployment gates, rather than relying on liveness alone | Deployment / Engineering | Proposed |
| Validate the database hostname at startup and reject unintended loopback hosts in container deployments | Engineering | Proposed |
| Add a post-deployment smoke test against a database-backed endpoint | Deployment owner | Proposed |
| Document the difference between container liveness, readiness, and database reachability in the [service availability runbook](../runbooks/service-availability.md) | Support | Done |
