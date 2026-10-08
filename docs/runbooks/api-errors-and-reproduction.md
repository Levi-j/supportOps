# API errors and request reproduction

Use this runbook when a customer reports a failed API call, an unexpected response, or slow requests. The aim is to reproduce the behavior without introducing new problems, capture the evidence, and decide what needs to happen next.

A typical investigation follows this sequence:

1. Reproduce the request using the same method, path, and relevant headers.
2. Check the HTTP status, response body, and request ID.
3. Find the corresponding server events in the logs.
4. Determine whether the issue is in the request, the account or configuration, or the service itself.

If most or all endpoints are failing, start with the [service availability runbook](service-availability.md). A shared dependency failure may explain several symptoms at once.

## Before you begin

SupportOps reads its connection details and default API key from `.env` or environment variables. For the local billing lab, the expected settings are:

| Setting | Lab value |
|---|---|
| `SUPPORTOPS_API_URL` | `http://127.0.0.1:8001` |
| `SUPPORTOPS_API_KEY` | `bk_juniper01_lab_only_not_a_real_key` |

Check the effective settings with `uv run supportops config show`. The command masks credentials.

To test a different key, use `--key-env` to read it from an environment variable. The following example uses a deliberately revoked **lab key**:

```powershell
$env:TEST_KEY = "bk_juniper00_lab_only_not_a_real_key"
uv run supportops api request GET /v1/account --key-env TEST_KEY
Remove-Item Env:TEST_KEY
```

For real customer credentials, follow your organization's secure-handling process. Avoid putting secrets directly in CLI arguments or copying them into tickets and shell history. SupportOps also refuses credential-bearing headers such as `Authorization` and `Cookie` through `--header`.

In output, API keys are shown only as their 12-character prefix followed by `***`.

## 1. Reproduce the request

Start with a read-only request and provide a recognizable correlation ID:

```powershell
uv run supportops api request GET /v1/account --request-id ticket-4711
```

A successful request looks like this (timestamps and timings will vary):

```text
GET http://127.0.0.1:8001/v1/account
200 OK in 17 ms
Request ID: ticket-4711 (echoed by the server)
Credentials: API key bk_juniper01*** from SUPPORTOPS_API_KEY
Headers:
  content-type: application/json
  x-request-id: ticket-4711
Body:
  {
    "id": "acct_juniper",
    "name": "Juniper Dental Group",
    "api_key": {
      "prefix": "bk_juniper01",
      "label": "Practice software",
      "created_at": "2026-07-09T16:11:35.570377Z"
    }
  }
```

Focus on the following evidence:

- **Status and duration:** What the server returned and how long the request took.
- **Request ID:** SupportOps sends `X-Request-Id` and reports whether the server echoed it, changed it, or omitted it. If you do not provide an ID, SupportOps generates one.
- **Credentials:** Which key source was used, with the value masked.
- **Headers:** Troubleshooting details such as `content-type`, `location`, `retry-after`, `www-authenticate`, and `allow`.
- **Body:** JSON is formatted for readability. Output is limited to 4,000 characters by default; use `--full` when you need more.

Use `--json` for structured output that can be processed by another tool. Review and redact any customer-specific data before sharing it.

The command exits with `0` for HTTP responses below 400, `1` for error responses or request failures, and `2` for invalid usage or configuration, including refused write requests.

## 2. Interpret the response

The billing API uses [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) for errors. SupportOps identifies the application error code, shows validation problems by field, and provides a troubleshooting hint.

| HTTP status | Application code | First things to check |
|---|---|---|
| `3xx` | — | The `location` header, requested path, and base URL. SupportOps reports redirects without following them. |
| `400` | `MALFORMED_REQUEST` | JSON syntax and `Content-Type`. The server could not parse the body. |
| `401` | `UNAUTHENTICATED` | Whether the key was supplied and is current. Use the request ID to find the precise rejection reason in server logs. |
| `403` | `ACCOUNT_SUSPENDED` | Account status. Replacing a valid key will not resolve a suspended account. |
| `404` | `RESOURCE_NOT_FOUND` | Path, resource ID, and account context. Another account's resources deliberately appear not to exist. |
| `409` | `INVOICE_NOT_PAYABLE` | Current invoice status and whether a payment has already succeeded. |
| `422` | `VALIDATION_FAILED` | Missing, invalid, or unsupported fields shown in the response. |
| `500` | `INTERNAL_ERROR` | Server logs and any exception associated with the request ID; escalate when necessary. |
| `503` | `SERVICE_UNAVAILABLE` / `DATABASE_BUSY` | Dependency health, database connectivity or contention, and any `Retry-After` header. |

The distinction between `400` and `422` is particularly useful: `400` means the body was not valid JSON; `422` means the JSON parsed successfully but failed the endpoint's validation rules.

For a `503`, also run:

```powershell
uv run supportops health
```

## 3. Use the request ID to check server logs

The API intentionally returns the same public `401 UNAUTHENTICATED` response for missing, unknown, revoked, and expired keys. The server logs the specific reason without exposing the full credential.

Here is a reproducible lab example using an invalid test key:

```powershell
$env:INVALID_LAB_KEY = "bk_placeholder0000000000000000000000"
uv run supportops api request GET /v1/account --key-env INVALID_LAB_KEY --request-id docs-401-1
Remove-Item Env:INVALID_LAB_KEY
```

The command should return `401 Unauthorized` and a `UNAUTHENTICATED` problem response with request ID `docs-401-1`. The public response does not reveal whether the key exists.

Search the billing API logs for that ID:

**Windows PowerShell**

```powershell
docker compose logs billing-api --no-log-prefix | Select-String "docs-401-1"
```

**Linux/macOS**

```bash
docker compose logs billing-api --no-log-prefix | grep "docs-401-1"
```

A matching event looks like:

```json
{"timestamp":"2026-10-08T19:11:41.856Z","level":"WARNING","service":"billing-api","logger":"billing_api.auth","message":"API key rejected","request_id":"docs-401-1","event_name":"auth.rejected","reason":"unknown_key","key_prefix":"bk_placehold"}
```

The `reason` field is the key evidence. Common values are `missing_header`, `malformed_header`, `unknown_key`, `revoked_key`, and `expired_key`.

For example, `revoked_key` may indicate that an integration is still using a key replaced during rotation. `unknown_key` may point to a copied value or a key intended for another environment. Confirm the circumstances before recommending a fix.

`--no-log-prefix` keeps Docker from adding the container name before each JSON log line. The dedicated `supportops logs` commands are planned for M5; until then, use Docker's logs directly.

## 4. When the API does not respond

Not every failure has an HTTP status. A DNS error, refused connection, or timeout can occur before the request reaches the service.

For example, a request to an unused local port may produce:

```text
GET http://127.0.0.1:8099/v1/account
Failed after 2023 ms: connection_refused
Nothing accepted the connection on 127.0.0.1:8099.
Request ID: supportops-8578226064ac (no response)
Hint: Check whether the service is running and whether the configured port is correct.
```

In this case, the request ID will not appear in the API logs because the request never arrived. On the Windows setup used for this lab, a refused connection may take roughly two seconds to report.

Check the configured address and running containers:

```powershell
uv run supportops config show
docker compose ps
```

For DNS failures, TLS errors, and connection or read timeouts, follow the steps in the [service availability runbook](service-availability.md#when-the-api-cant-be-reached). Treat an unclassified transport failure as unknown until more evidence is available.

## 5. Reproduce requests with JSON bodies

For requests that have a body, prefer a file. This avoids shell-quoting problems and makes the reproduction easy to repeat:

```powershell
uv run supportops api request POST /v1/customers --data-file docs\examples\new-customer.json --yes
```

**This example creates a customer in the local lab.** Do not run it merely to check connectivity.

SupportOps uses `Content-Type: application/json` unless another content type is supplied. It validates JSON before sending the request. If the body is invalid, it exits with code `2` and sends nothing.

### PowerShell 5.1 quoting

Windows PowerShell 5.1 can strip embedded double quotes when passing inline JSON to native programs. For example:

```powershell
uv run supportops api request POST /v1/customers --data '{"name":"Acme","email":"billing@acme.example"}' --yes
```

On the Windows PowerShell 5.1 setup used for this project, the command can reach SupportOps without the necessary JSON quotes. The CLI should reject the invalid JSON before sending anything and suggest `--data-file`.

This explains a realistic support ticket: a request works from Postman but fails in a script because the script sends a different body.

### Send malformed JSON intentionally

When you need to reproduce the customer's exact malformed payload, `--raw` skips the CLI's JSON validation:

```powershell
uv run supportops api request POST /v1/customers --data-file docs\examples\broken-customer.json --raw --yes
```

This deliberately sends an invalid body to the **local lab** and should return `400 MALFORMED_REQUEST`, without creating a customer.

The repository includes these examples:

| File | Expected outcome |
|---|---|
| `docs/examples/new-customer.json` | `201 Created`; **creates a customer** |
| `docs/examples/broken-customer.json` | `400 MALFORMED_REQUEST` when used with `--raw` |
| `docs/examples/customer-missing-email.json` | `422 VALIDATION_FAILED` |
| `docs/examples/invoice-with-total.json` | `422 VALIDATION_FAILED` when sent to `/v1/invoices` |

## 6. Understand the write safeguards

`GET`, `HEAD`, and `OPTIONS` are permitted without an additional confirmation. A request using `POST`, `PUT`, `PATCH`, or `DELETE` is sent only when all three conditions hold:

1. You explicitly pass `--yes` for that request.
2. The configured target is a loopback address (`localhost`, `127.0.0.1`, or `::1`).
3. The service's `/health` response confirms `"environment": "lab"`.

The first two checks happen before any network traffic. Without `--yes`, the CLI refuses the request:

```text
Error: POST can change data on the server, so it needs --yes. Nothing was sent.
Hint: Add --yes only if you intend to change data in the local lab.
```

These safeguards prevent ordinary use of SupportOps from sending writes to remote services. They do **not** replace the need to verify the lab you're connected to before changing data.

SupportOps never automatically retries a write. If the connection drops or times out after sending the request, the operation might still have succeeded. Check the resulting resource or server logs before retrying—particularly for payments.

If you need to reset the lab after an exercise, read the [lab reset procedure](../../README.md#resetting-the-lab) first. **Resetting deletes all changes in the SupportOps database volume.**

## 7. Measure response times

When a customer reports intermittent slowness, collect several timings rather than relying on one request:

```powershell
uv run supportops api latency /v1/invoices --count 20
```

An example result:

```text
GET http://127.0.0.1:8001/v1/invoices: 20 sequential requests
Responses: 200 x20
Successful: 20 of 20  p50 11.8 ms  p95 13.5 ms  max 18.2 ms  (threshold 1000 ms)
Slowest: supportops-latency-3ed29a-001 18.2 ms, supportops-latency-3ed29a-020 13.5 ms, supportops-latency-3ed29a-005 13.1 ms
All requests succeeded within the threshold.
```

The command sends only `GET` requests, sequentially, with a maximum of 100 per run. Each request has a separate ID so you can correlate slower requests with server logs.

- **p50** is the median response time.
- **p95** is the nearest-rank 95th percentile of successful requests. For fewer than 20 successful requests, it equals the slowest one.
- **max** is the longest successful request duration.

Non-2xx responses and transport failures count as problems. The command exits with `1` if any request fails or if p95 exceeds `SUPPORTOPS_SLOW_REQUEST_MS` (default: 1,000 ms) or the supplied `--threshold-ms`.

### A slow first request

A new HTTP connection takes time to establish, while later requests may reuse it. If the first request is much slower than the rest, check connection setup before blaming the API handler.

For example, on the Windows lab, `http://localhost:8001` may try IPv6 before reaching the service over IPv4. This can make the first request much slower. Prefer `http://127.0.0.1:8001` in `SUPPORTOPS_API_URL` for this lab.

The timing is measured from the client and includes connection overhead; it is not a pure measurement of server processing time.

## 8. Reproduce a request with curl

If SupportOps is unavailable, curl can help reproduce the HTTP request. These examples use the **public, fictional lab key**. Do not copy real customer credentials into command history.

**Windows PowerShell**

```powershell
$key = "bk_juniper01_lab_only_not_a_real_key"
curl.exe -i -H "X-Request-Id: ticket-4711" -H "Authorization: Bearer $key" http://127.0.0.1:8001/v1/account
curl.exe -i -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" `
  --data-binary "@docs/examples/customer-missing-email.json" http://127.0.0.1:8001/v1/customers
```

**Linux/macOS**

```bash
key="bk_juniper01_lab_only_not_a_real_key"
curl -i -H "X-Request-Id: ticket-4711" -H "Authorization: Bearer $key" http://127.0.0.1:8001/v1/account
curl -i -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
  --data-binary "@docs/examples/customer-missing-email.json" http://127.0.0.1:8001/v1/customers
```

The POST example is intentionally invalid and should return `422 VALIDATION_FAILED`, without creating a record.

Unlike SupportOps, curl does not validate JSON before sending, enforce lab-only writes, or automatically protect secrets in verbose request output. Be careful when sharing command output, particularly if you use `-v`.
