# Logs and request IDs

Use this runbook to trace a failed request, identify recurring errors, and collect evidence for an engineering handoff. If you have a request ID, start with a trace. Otherwise, summarize recent activity and narrow the search.

The examples use the local SupportOps billing API. Examples for `400`, `422`, and `500` responses read **synthetic fixture logs** from `tests/fixtures/logs/billing-api.jsonl`; they do not activate faults or modify the lab database.

## Understand the log fields

The billing API writes structured logs as JSON Lines: one JSON object per line. An authentication failure, for example, may look like this:
```json
{"timestamp":"2026-10-08T21:39:12.256Z","level":"WARNING","service":"billing-api","logger":"billing_api.auth","message":"API key rejected","request_id":"ticket-5120","event_name":"auth.rejected","reason":"missing_header"}
```
The most useful fields are:

| Field | What it tells you |
| --- | --- |
| `timestamp` | When the event was recorded; `Z` denotes UTC. |
| `level` | Severity, such as `INFO`, `WARNING`, or `ERROR`. |
| `event_name` | What happened, such as `auth.rejected` or `http.request`. |
| `request_id` | Correlation ID connecting events from one request. |
| `method`, `path`, `status`, `duration_ms` | HTTP method, path, response status, and server-side processing time. |
| `account_id` | Account associated with the request, when known. |
| `reason`, `error`, `fields` | Event-specific details, including rejection reasons and validation fields. |
| `error_type`, `error_message`, `stack_trace` | Exception details when an unexpected failure is recorded. |

The billing API does not log full API keys, Authorization headers, request bodies, query strings, or customer email addresses. Other services may handle these fields differently, so review excerpts before sharing them.

SupportOps also understands OrderFlow's ECS JSON fields, including `requestId`, `eventName`, and `durationMs`. The [OrderFlow integration guide](../integrations/orderflow.md) explains the differences between the two targets.

### Follow the request ID

The billing API returns `X-Request-Id` in response headers and includes `request_id` in structured error bodies. Server log events generated while handling the request use the same value.

Make a safe, deliberately unauthenticated request to create a traceable example:
```powershell
uv run supportops api request GET /v1/account --no-auth --request-id ticket-5120
```
The API should return `401 Unauthorized`, and the command exits with code `1` because the response is an error. This is expected for the example; no records are changed.

Request IDs may contain letters, digits, underscores, and hyphens, up to 64 characters. If the supplied ID is invalid, the API replaces it. Always use the ID **returned by the API** when searching its logs.

## Choose a command

| Command | Use it to |
| --- | --- |
| `logs summary` | Review response statuses, event counts, slow requests, and recurring errors. |
| `logs search` | Find entries matching a request ID, event, severity, status, path, text, or time window. |
| `logs trace REQUEST_ID` | Read the events associated with one request in timestamp order. |

All three commands read from Docker, a local file, or standard input. Without an explicit source, they use `SUPPORTOPS_LOG_SOURCE`, which defaults to `docker:supportops-billing-api-1` in the lab.

## Trace a request

After running the unauthenticated request above:
```powershell
uv run supportops logs trace ticket-5120
```
A shortened trace looks like this:
```text
2 log entries for request ID ticket-5120 in docker:supportops-billing-api-1
Access log: GET /v1/account -> 401 in 8 ms
Highlights: authentication
+0 ms  WARNING  auth.rejected  API key rejected  [authentication]
       reason=missing_header
+0 ms  INFO     http.request   GET /v1/account -> 401 (8 ms)
       user_agent=supportops/0.1.0
```
The access log records the final HTTP status and server-side duration. The `+N ms` values indicate how much later each event was **logged**, relative to the first entry; they are not timings for the operations represented by those events. Two entries may have the same offset if they were recorded within the same millisecond.

Tags such as `[authentication]`, `[validation]`, `[database]`, and `[exception]` help surface relevant evidence. They do not establish a root cause by themselves. Recorded stack traces are shown when available, with recognized secrets masked.

If no entries match, `logs trace` exits with code `1`. Before concluding that the request was not handled, check whether it reached the API at all, whether the ID and source are correct, and whether the selected time window covers the event. A recreated Docker container may no longer expose the previous container's logs.

## Investigate common HTTP failures

### 401 — Authentication rejected

The billing API returns the same public `401 UNAUTHENTICATED` response for missing, malformed, unknown, revoked, and expired keys. Its `auth.rejected` event records the internal reason.

| Logged reason | Meaning | Next check |
| --- | --- | --- |
| `missing_header` | No Authorization header arrived. | Confirm the client supplies credentials. |
| `malformed_header` | Header or key format is invalid. | Check how the client constructs `Bearer <key>`. |
| `unknown_key` | The submitted credential was not recognized. | Check typos, rotation, and target environment. |
| `revoked_key` | The matching key has been revoked. | Verify rotation history and the deployed credential. |
| `expired_key` | The matching key has expired. | Check whether a current key was issued. |

Search other authentication failures from the same period:
```powershell
uv run supportops logs search --event auth.rejected --since 1h
uv run supportops logs summary --since 1h
```
Repeated `revoked_key` events may indicate an integration still using a rotated-out credential. Compare the prefix, account, and rotation history before recommending a change. See [Authentication troubleshooting](authentication.md) to inspect a credential and its read-only database metadata.

### 400 and 422 — Request body problems

A `400 MALFORMED_REQUEST` means the API could not parse the request body as JSON. Inspect the corresponding `request.invalid_json` event with the included fixture:
```powershell
uv run supportops logs trace demo-400 tests\fixtures\logs\billing-api.jsonl
```
```text
+0 ms  WARNING  request.invalid_json  Request body is not valid JSON  [validation]
       error_position=1 content_type=application/json content_length=52
+1 ms  INFO     http.request         POST /v1/customers -> 400
```
The error position and decoder details can help identify incorrect quoting or serialization. Windows PowerShell 5.1 can alter quotes passed to native commands, so inspect the bytes the client actually sent rather than relying only on its source code. See [API errors and reproduction](api-errors-and-reproduction.md#powershell-51-quoting).

A `422 VALIDATION_FAILED` has a different meaning: the JSON was parsed, but one or more fields did not satisfy the endpoint's schema.
```powershell
uv run supportops logs trace demo-422 tests\fixtures\logs\billing-api.jsonl
```
```text
+0 ms  WARNING  request.validation_failed  Request validation failed  [validation]
       fields=body.email error_types=missing
+1 ms  INFO     http.request            POST /v1/customers -> 422
```
The log identifies the field and validation category without including the customer's submitted value.

### 500 — An exception during processing

Use the fixture to inspect an exception associated with a deliberately broken payment flow:
```powershell
uv run supportops logs trace demo-500-a tests\fixtures\logs\billing-api.jsonl
```
The relevant sequence is:
```text
Access log: POST /v1/invoices/inv_juniper_1005/pay -> 500
Highlights: exception
+0 ms  INFO   payment.recorded      Payment recorded
+7 ms  ERROR  unhandled_exception  InjectedFault: Lab fault payment_partial_commit
       Stack trace:
       Traceback (most recent call last):
       ...
+9 ms  INFO   http.request         POST /v1/invoices/inv_juniper_1005/pay -> 500
```
**The payment was recorded before the exception.** A `500` response does not guarantee that an operation left no changes behind. In this synthetic fault scenario, retrying can create a second payment while the invoice remains unpaid. Check invoice and payment records before recommending a retry. See [Database diagnostics](database-diagnostics.md) for read-only consistency checks.

For escalation, preserve the request ID, timestamp, HTTP status, relevant business events, exception type, and stack trace. Distinguish what the logs show from any explanation you infer.

### 503 — Database unavailable or busy

Start by looking for database-specific events and comparing them with service health:
```powershell
uv run supportops logs search --event db.unavailable --since 1h
uv run supportops health
```
Inspect categories such as `dns_failure` or `connection_refused` and look for `db.lock_timeout` or related timeout events. The [service availability runbook](service-availability.md) helps distinguish a dependency outage from an API configuration problem; [database diagnostics](database-diagnostics.md#investigate-database-sessions-and-locks) covers long transactions and blocking sessions.

## Find patterns across requests

When no request ID is available, begin with a recent summary and narrow the scope:
```powershell
uv run supportops logs summary --since 15m
uv run supportops logs search --status 5xx --since 1h
uv run supportops logs search --status 4xx --path /v1 --since 1h
uv run supportops logs search --level warning --since 30m
```
`logs summary` counts severity levels, event names, and HTTP statuses, highlights slow requests, and groups similar warning/error messages. It normalizes variable IDs and numbers to help spot patterns. Grouping is heuristic; compare example request IDs before assuming two events have the same cause.

`logs search` accepts combinable filters for request ID, level, event, status, path, free text, and time range. `--status 4xx` matches client errors, while `--event "auth.*"` matches event names beginning with `auth.`. `--level warning` includes warnings and more severe levels. Results show the newest 50 matches by default, adjustable with `--limit`.

**The `--path` filter belongs to `logs search`, not `logs summary`.** Routine `/health` requests can dominate an unfiltered summary, so filter for `/v1` traffic or a relevant status when investigating customer calls.

Relative time arguments include `30s`, `15m`, `2h`, and `1d`. ISO timestamps are also supported. `--since` is inclusive, `--until` is exclusive, and entries without usable timestamps are excluded from time-filtered results.

## Read logs from different sources

### Docker
```powershell
uv run supportops logs summary docker:supportops-billing-api-1 --since 1h
uv run supportops logs trace ticket-5120 docker:supportops-billing-api-1
```
SupportOps invokes `docker logs` directly, avoiding PowerShell's text-pipeline encoding issues. Docker must be installed and the named container must exist. Logs from a stopped container are still readable, but a new container has its own log history. Docker reads are limited to the most recent **100,000 lines**.

### Local files

To save a local copy without changing the running service:
```powershell
docker compose --project-name supportops logs --no-log-prefix billing-api > "$env:TEMP\supportops-billing-api.log"
uv run supportops logs summary "$env:TEMP\supportops-billing-api.log"
```
Windows PowerShell 5.1 commonly writes UTF-16 through output redirection. The log reader recognizes UTF-8, UTF-8 with a BOM, and UTF-16. The raw exported file may contain sensitive content that would be masked in SupportOps' formatted output; review it before attaching it to a ticket.

### Standard input

Use `-` as the source to consume piped log lines:
```powershell
docker compose --project-name supportops logs --no-log-prefix billing-api | uv run supportops logs summary -
```
PowerShell 5.1 may re-encode piped text and replace some non-ASCII characters. When exact bytes matter, prefer the `docker:` source or a saved file. SupportOps can also recognize the standard `billing-api-1 |` prefix produced by Docker Compose when log prefixes are enabled.

## Inspect logs with standard command-line tools

You can perform quick checks without SupportOps, but these commands **do not automatically redact secrets**.

**Windows PowerShell 5.1:**
```powershell
docker logs supportops-billing-api-1 | Select-String 'ticket-5120'
docker logs supportops-billing-api-1 |
    ForEach-Object { $_ | ConvertFrom-Json } |
    Where-Object { $_.event_name -eq 'auth.rejected' } |
    Select-Object timestamp, request_id, reason, key_prefix
docker logs supportops-billing-api-1 |
    ForEach-Object { $_ | ConvertFrom-Json } |
    Where-Object { $_.event_name -eq 'http.request' } |
    Group-Object status | Select-Object Name, Count
```
`ConvertFrom-Json` reports errors for lines that are not valid JSON. Other services may emit plain-text logs or multiline stack traces, so these commands may require adjustment.

**Linux/macOS:**
```bash
docker logs supportops-billing-api-1 | grep 'ticket-5120'
docker logs supportops-billing-api-1 \
  | jq -c 'select(.event_name == "auth.rejected") | {timestamp, request_id, reason, key_prefix}'
docker logs supportops-billing-api-1 \
  | jq -r 'select(.event_name == "http.request") | .status' \
  | sort | uniq -c
```
These Unix examples require `jq` for structured filtering.

## Interpret evidence carefully

- **No matching log does not prove that nothing happened.** Check the correct source, request ID, time window, and whether the server received the request.
- **HTTP status and log severity are different.** An access event may be logged at `INFO` even when the response is `401` or `500`. Look for related warning or exception events.
- **Trace offsets and request durations are different measurements.** Offsets indicate when log lines were written; `duration_ms` comes from the API. A client-side duration includes additional overhead.
- **A summary is only as complete as its source.** Check the time coverage, skipped lines, and missing timestamps before relying on totals.
- **Not all log lines are valid structured JSON.** Malformed, incomplete, or oversized entries are counted and skipped. Unrecognized fields are retained as additional details where possible.
- **Redaction is best-effort.** Recognized credentials and sensitive fields are masked in rendered text and JSON, but unfamiliar secret formats may evade detection.

Combine logs with API responses, service-health observations, and approved read-only database checks before forming a root-cause assessment. For an escalation, include the request ID, timestamps, relevant events, and an explicit note about any missing evidence.
