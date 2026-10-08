# Logs and request IDs

Use this runbook to investigate a failed API request, look for recurring errors, or collect evidence for an escalation. Start with a request ID if you have one. Otherwise, use a log summary to understand recent activity, then search for the events that matter.

The examples use the local SupportOps billing API. The 400, 422, and 500 examples read synthetic events from `tests/fixtures/logs/billing-api.jsonl`; they do not change the lab or trigger faults.

## What the logs contain

The billing API writes structured logs: one JSON object per line, with named fields rather than an unstructured message. For example:

```json
{"timestamp":"2026-10-08T21:39:12.256Z","level":"WARNING","service":"billing-api","logger":"billing_api.auth","message":"API key rejected","request_id":"ticket-5120","event_name":"auth.rejected","reason":"missing_header"}
```

The fields most useful during an investigation are:

| Field | What it tells you |
| --- | --- |
| `timestamp` | When the event was logged. `Z` indicates UTC. |
| `level` | The severity of the log entry, such as `INFO`, `WARNING`, or `ERROR`. |
| `event_name` | The type of event, such as `auth.rejected` or `http.request`. |
| `request_id` | The correlation ID linking entries from the same request. |
| `method`, `path`, `status`, `duration_ms` | What the HTTP request did, how it ended, and how long the server took. |
| `account_id` | The account associated with an authenticated request, when available. |
| `reason`, `error`, `fields` | Event-specific context, such as an authentication rejection or validation failure. |
| `error_type`, `error_message`, `stack_trace` | Exception information, when an unexpected error is logged. |

The billing API is designed not to log full API keys, Authorization headers, request bodies, or customer email addresses. Still, treat logs from other services as potentially sensitive and review excerpts before sharing them.

### Why the request ID matters

The billing API returns `X-Request-Id` in its responses and includes `request_id` in error bodies. Log entries produced while handling the request carry the same ID. That lets you isolate one request from routine health checks and other customer traffic.

SupportOps generates a request ID if you do not provide one. For a reproducible test, use an ID associated with your ticket:

```powershell
uv run supportops api request GET /v1/account --no-auth --request-id ticket-5120
```

This request intentionally omits credentials, so the expected response is `401 Unauthorized`. The request ID is `ticket-5120`; the API request command exits with code `1` because it received an error response.

A supplied ID can contain letters, digits, underscores, and hyphens, up to 64 characters. The billing API replaces invalid IDs with a generated value. Use the ID returned by the API when correlating logs.

## Choose the right log command

| Command | Use it when |
| --- | --- |
| `logs summary` | You need an overview of recent statuses, events, warnings, or repeated errors. |
| `logs search` | You know what to filter for: a status class, request ID, event name, path, or time range. |
| `logs trace REQUEST_ID` | You want the ordered events for one specific request. |

All three commands read logs without changing the source. If you do not specify a source, they use `SUPPORTOPS_LOG_SOURCE`, which defaults to `docker:supportops-billing-api-1` in the lab.

## Trace a request from start to finish

After sending the request above, run:

```powershell
uv run supportops logs trace ticket-5120
```

A typical result is:

```text
2 log entries for request ID ticket-5120 in docker:supportops-billing-api-1
Access log: GET /v1/account -> 401 in 8 ms
First entry at 2026-10-08T21:39:12.256Z, spanning 0 ms
Highlights: authentication

+0 ms  WARNING  auth.rejected  API key rejected  [authentication]
       reason=missing_header
+0 ms  INFO     http.request   GET /v1/account -> 401 (8 ms)
       user_agent=supportops/0.1.0
```

The access log records the method, path, final status, and server-side duration. The `+N ms` offsets show when each entry was written relative to the first entry; they are not measurements of how long an individual operation took. Two events can both appear at `+0 ms` when their timestamps are within the same millisecond.

Markers such as `[authentication]`, `[validation]`, `[database]`, and `[exception]` highlight events worth inspecting. They describe the recorded evidence; they do not establish a root cause on their own. Additional fields appear under their events, with recognized secrets masked.

If no matching entries exist, `logs trace` exits with code `1`. Before concluding the request was never processed, check that:

- The request reached the API. Connection and DNS failures can happen before the server receives anything.
- You have the correct container or file, and the exact request ID.
- The selected time window includes the request.
- The container has not been recreated since the event; its previous logs may no longer be available through `docker logs`.

A missing access-log entry also limits what you can conclude about the final HTTP response. Use the client response and other evidence rather than guessing.

## Investigate common HTTP errors

### 401: Authentication rejected

The billing API returns the same public `401 UNAUTHENTICATED` response for missing, malformed, unknown, revoked, or expired keys. The server's `auth.rejected` log entry records the specific reason without exposing the full key.

| Logged `reason` | Meaning | What to check |
| --- | --- | --- |
| `missing_header` | No Authorization header arrived. | Whether the client sends credentials. |
| `malformed_header` | The header does not have the expected Bearer format. | Header construction and the `Bearer ` prefix. |
| `unknown_key` | The supplied key is not recognized. | Typos, environment mix-ups, or outdated configuration. |
| `revoked_key` | The key was revoked. | Recent key rotation and which key the client deployed. |
| `expired_key` | The key is past its expiry. | Whether a current key has been issued and deployed. |

The `key_prefix` field helps distinguish keys without revealing the full credential. To find other rejections around the same time:

```powershell
uv run supportops logs search --event auth.rejected --since 1h
uv run supportops logs summary --since 1h
```

A recurring `revoked_key` reason may point to an integration still using an old key, but confirm the account and rotation history before advising a change.

### 400 and 422: Request body problems

A `400 MALFORMED_REQUEST` indicates that the billing API could not parse the JSON body. Inspect the corresponding `request.invalid_json` event:

```powershell
uv run supportops logs trace demo-400 tests\fixtures\logs\billing-api.jsonl
```

The fixture includes an event similar to:

```text
+0 ms  WARNING  request.invalid_json  Request body is not valid JSON  [validation]
       error_position=1 content_type=application/json content_length=52
+1 ms  INFO     http.request          POST /v1/customers -> 400 (3 ms)
```

The error position and decoder message can help identify a quoting or formatting problem. In Windows PowerShell 5.1, inline JSON passed to a native program can lose its double quotes. Compare the exact bytes sent by the client rather than assuming its source text was transmitted unchanged. See [API errors and request reproduction](api-errors-and-reproduction.md#powershell-51-quoting).

A `422 VALIDATION_FAILED` means the JSON was parsed, but its fields failed validation:

```powershell
uv run supportops logs trace demo-422 tests\fixtures\logs\billing-api.jsonl
```

```text
+0 ms  WARNING  request.validation_failed  Request validation failed  [validation]
       fields=body.email error_types=missing
+1 ms  INFO     http.request           POST /v1/customers -> 422 (11 ms)
```

The log identifies the affected field and the type of validation failure without recording the customer's submitted value.

### 500: An exception during a request

Use the synthetic fixture to examine a failure without activating the lab's fault injection:

```powershell
uv run supportops logs trace demo-500-a tests\fixtures\logs\billing-api.jsonl
```

The example contains the following sequence:

```text
Access log: POST /v1/invoices/inv_juniper_1005/pay -> 500 in 48 ms
Highlights: exception

+0 ms  INFO   payment.recorded      Payment recorded
+7 ms  ERROR  unhandled_exception  Unhandled exception - InjectedFault: Lab fault payment_partial_commit  [exception]
       Stack trace:
       Traceback (most recent call last):
       ...
+9 ms  INFO   http.request         POST /v1/invoices/inv_juniper_1005/pay -> 500 (48 ms)
```

The key detail is the order: the payment was recorded **before** the error. A `500` does not necessarily mean the operation had no effect. In this deliberately faulty scenario, retrying can create a duplicate payment. Do not advise a retry until the payment and invoice state have been checked.

For an engineering escalation, include the request ID, timestamp, status, relevant business events, exception type, and stack trace. Keep factual observations separate from conclusions about the cause.

### 503: Service or database unavailable

Search for `db.unavailable`, `db.lock_timeout`, and related entries. Check the `error` category, such as `dns_failure` or `connection_refused`, and compare what the API reports with the direct health checks:

```powershell
uv run supportops logs search --event db.unavailable --since 1h
uv run supportops health
```

Continue with the [service availability runbook](service-availability.md) to distinguish application configuration problems from dependency outages.

## Find patterns across requests

A summary is useful when the customer cannot provide a request ID or reports an intermittent issue:

```powershell
uv run supportops logs summary --since 15m
uv run supportops logs search --status 5xx --since 1h
uv run supportops logs search --status 4xx --path /v1 --since 1h
uv run supportops logs search --level warning --since 30m
```

`logs summary` groups levels, event names, HTTP statuses, recurring warning/error signatures, and the slowest requests. Pattern grouping removes changing IDs and numbers so similar errors can be counted together. Check the example request IDs before treating grouped entries as identical incidents.

`logs search` supports filters for request ID, level, event, status, path, text, and time range. Filters can be combined. For example, `--status 4xx` matches client-error responses, and `--event auth.*` matches authentication-related event names. `--level warning` includes warning and more severe levels.

**Use `--path` with `logs search`, not `logs summary`.** A path filter is not part of the summary command's interface.

Relative times include `30s`, `15m`, `2h`, and `1d`; ISO timestamps are also supported. `--since` includes the boundary and `--until` excludes it. Events without usable timestamps cannot be included in a time-filtered result.

Be aware that routine `GET /health` traffic can dominate an unfiltered summary. Search for `/v1` paths or specific statuses when you need to focus on customer API traffic.

## Read logs from Docker, files, or stdin

### Docker containers

```powershell
uv run supportops logs summary docker:supportops-billing-api-1 --since 1h
uv run supportops logs trace ticket-5120 docker:supportops-billing-api-1
```

SupportOps invokes `docker logs` directly rather than passing the output through PowerShell. Docker must be available and the container must exist. A stopped container's logs can still be read; a recreated container has its own log history. Docker retrieval is limited to the most recent 100,000 lines.

### Local log files

Save a copy when you need to preserve evidence or examine logs without Docker:

```powershell
# Run from the repository root.
docker compose logs --no-log-prefix billing-api > "$env:TEMP\supportops-billing-api.log"
uv run supportops logs summary "$env:TEMP\supportops-billing-api.log"
```

Windows PowerShell 5.1 normally uses UTF-16 for redirected output. SupportOps detects UTF-8, UTF-8 with BOM, and UTF-16. If another tool requires UTF-8, export with the encoding it supports; PowerShell 5.1's `Out-File -Encoding utf8` writes UTF-8 with a BOM.

Review log files before attaching them to a ticket. The original file is evidence and may contain information that the SupportOps renderer would otherwise mask.

### Standard input

Use `-` to read a pipeline:

```powershell
docker compose logs --no-log-prefix billing-api | uv run supportops logs summary -
```

PowerShell 5.1 can re-encode text passed between programs, occasionally replacing non-ASCII characters. For logs where exact bytes matter, prefer the direct `docker:` source or a file. SupportOps also recognizes the standard `billing-api-1 |` prefix if you omit `--no-log-prefix`.

## Inspect logs without SupportOps

For a quick check, standard command-line tools can work with the JSON log entries. Unlike SupportOps' formatted output, these commands do not automatically redact secrets.

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

`ConvertFrom-Json` reports errors for lines that aren't valid JSON. Logs from services other than the billing API may contain plain-text messages or multiline tracebacks.

**Linux/macOS:**

```bash
docker logs supportops-billing-api-1 | grep 'ticket-5120'

docker logs supportops-billing-api-1 \
  | jq -c 'select(.event_name == "auth.rejected") | {timestamp, request_id, reason, key_prefix}'

docker logs supportops-billing-api-1 \
  | jq -r 'select(.event_name == "http.request") | .status' \
  | sort | uniq -c
```

## Interpret the evidence carefully

- **Missing logs are not proof that nothing happened.** Confirm the source, time window, and request ID before drawing that conclusion.
- **A status code and a log level are different.** The billing API records HTTP access events at `INFO`, even when the response is `401` or `500`; the related failure may appear in a separate `WARNING` or `ERROR` event.
- **Timestamps and durations measure different things.** Trace offsets indicate when messages were logged. `duration_ms` is measured in the API, while the time shown by `api request` also includes client and network overhead.
- **Log summaries depend on their input.** Check the source, coverage period, skipped-line count, and missing timestamps before interpreting totals or error patterns.
- **Not every input is structured JSON.** Non-JSON lines and very long entries are skipped. SupportOps understands the billing API format and common ECS-style fields; unfamiliar fields remain available as extra details.
- **Redaction has limits.** SupportOps masks known credential patterns and sensitive fields in its output, but unusual formats may evade detection. Review any excerpt or JSON export before sharing it.

The logs provide evidence of what was recorded. Combine them with the API response, health checks, and—when available—read-only database checks before deciding what happened or how to resolve it.
