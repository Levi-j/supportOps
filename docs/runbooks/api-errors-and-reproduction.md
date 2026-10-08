# API errors and request reproduction

Use this runbook when a customer reports a failed API call, an unexpected response, or slow requests. The goal is to reproduce the problem safely, collect enough evidence to understand what happened, and decide whether the fix belongs with the client, the account configuration, or the service.

A useful starting sequence is:

1. Reproduce the request using the same method, path, and relevant headers.
2. Record the HTTP status, response body, timing, and request ID.
3. Trace the request ID through the server logs.
4. Compare the evidence with the customer's report before suggesting a fix or escalation.

If several endpoints are failing, start with the [service availability runbook](service-availability.md). A shared dependency failure may explain the symptoms.

## Before you begin

SupportOps reads its API address and credentials from `.env` or environment variables. For the local billing lab, the expected values are:

| Setting | Local lab value |
| --- | --- |
| `SUPPORTOPS_API_URL` | `http://127.0.0.1:8001` |
| `SUPPORTOPS_API_KEY` | `bk_juniper01_lab_only_not_a_real_key` |

Check the active configuration:

```powershell
uv run supportops config show
```

The command masks credentials. API keys in diagnostic output appear as a prefix followed by `***`.

To test a different key without passing it on the command line, use `--key-env`. This example uses a fictional revoked lab key:

```powershell
$env:TEST_KEY = "bk_juniper00_lab_only_not_a_real_key"
uv run supportops api request GET /v1/account --key-env TEST_KEY
Remove-Item Env:TEST_KEY
```

Use your organization's approved procedure for handling real customer credentials. Don't put them in command-line arguments, tickets, or shared logs. SupportOps also rejects credential-bearing custom headers, including `Authorization` and `Cookie`, through `--header`.

## 1. Reproduce the request

Start with a read-only request and assign a request ID that will be easy to find later:

```powershell
uv run supportops api request GET /v1/account --request-id ticket-4711
```

A successful response should resemble the following. Timings and returned data may vary:

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
    "label": "Practice software"
  }
}
```

Look for four pieces of evidence:

- **Status and duration:** What did the API return, and how long did the request take?
- **Request ID:** Did the server echo the `X-Request-Id` sent by SupportOps? If you don't supply one, SupportOps generates it.
- **Credentials:** Which key source was used? The key itself remains masked.
- **Response details:** Inspect relevant headers such as `content-type`, `location`, `retry-after`, `www-authenticate`, and `allow`, along with the response body.

JSON responses are formatted for readability. The CLI displays up to 4,000 characters by default; use `--full` when more detail is needed. Add `--json` when another tool needs structured diagnostic output. Review the result for customer-specific information before sharing it.

The request command returns exit code `0` for HTTP responses below 400, `1` for error responses or request failures, and `2` for invalid usage or configuration, including a rejected write attempt.

## 2. Interpret the response

The billing API uses [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) for errors. SupportOps displays the application's error code and a troubleshooting hint; for validation errors, it also identifies the relevant fields.

| HTTP status | Application code | What to check first |
| --- | --- | --- |
| `3xx` | — | The `Location` header, request path, and base URL. SupportOps does not follow redirects automatically. |
| `400` | `MALFORMED_REQUEST` | JSON syntax and `Content-Type`; the server could not parse the body. |
| `401` | `UNAUTHENTICATED` | Whether a current key was supplied. Use the request ID to find the rejection reason in server logs. |
| `403` | `ACCOUNT_SUSPENDED` | Account status; replacing a valid key will not reactivate a suspended account. |
| `404` | `RESOURCE_NOT_FOUND` | Path, resource ID, and account context. Resources owned by other tenants are deliberately hidden. |
| `409` | `INVOICE_NOT_PAYABLE` | Invoice status and whether a payment already succeeded. |
| `422` | `VALIDATION_FAILED` | Missing or invalid fields identified in the response. |
| `500` | `INTERNAL_ERROR` | Server events and exceptions associated with the request ID. Escalate if necessary. |
| `503` | `SERVICE_UNAVAILABLE` / `DATABASE_BUSY` | Dependency health, database access or contention, and any `Retry-After` header. |

The distinction between `400` and `422` matters: `400` means the body could not be parsed as JSON; `422` means it was valid JSON but failed the endpoint's validation rules.

For a `503`, check service health as well:

```powershell
uv run supportops health
```

## 3. Trace the request ID through server logs

The public `401 UNAUTHENTICATED` response is intentionally the same whether an API key is missing, unknown, revoked, or expired. The server logs the internal reason without recording the full key.

Reproduce a `401` with a fictional invalid key:

```powershell
$env:INVALID_LAB_KEY = "bk_placeholder0000000000000000000000"
uv run supportops api request GET /v1/account --key-env INVALID_LAB_KEY --request-id docs-401-1
Remove-Item Env:INVALID_LAB_KEY
```

The API should return `401 Unauthorized` with request ID `docs-401-1`.

**Use SupportOps to trace that request:**

```powershell
uv run supportops logs trace docs-401-1
```

The trace groups events with that request ID in time order. Look for an `auth.rejected` event with `reason=unknown_key`, followed by the `http.request` access event with status `401`. The exact timing varies.

You can also inspect the original Docker logs directly.

**Windows PowerShell 5.1:**

```powershell
docker compose logs billing-api --no-log-prefix | Select-String "docs-401-1"
```

**Linux/macOS:**

```bash
docker compose logs billing-api --no-log-prefix | grep "docs-401-1"
```

An authentication event will look similar to this:

```json
{"timestamp":"2026-10-08T19:11:41.856Z","level":"WARNING","service":"billing-api","logger":"billing_api.auth","message":"API key rejected","request_id":"docs-401-1","event_name":"auth.rejected","reason":"unknown_key","key_prefix":"bk_placehold"}
```

The `reason` field is the important evidence. Possible values include `missing_header`, `malformed_header`, `unknown_key`, `revoked_key`, and `expired_key`.

For example, `revoked_key` may mean an integration still uses a credential replaced during key rotation. `unknown_key` may indicate a copied value or a key from another environment. Confirm the customer's circumstances before recommending a change.

The `--no-log-prefix` option keeps Docker from adding a container label ahead of each JSON entry. For more on filtering and reading request timelines, see [Logs and request IDs](logs-and-request-ids.md).

## 4. When the API doesn't respond

Not every failure has an HTTP status. DNS resolution, a refused TCP connection, or a timeout can prevent the request from reaching the API.

A request to an unused local port may produce output like this:

```text
GET http://127.0.0.1:8099/v1/account
Failed after 2023 ms: connection_refused
Nothing accepted the connection on 127.0.0.1:8099.
Request ID: supportops-8578226064ac (no response)
Hint: Check whether the service is running and whether the configured port is correct.
```

There may be no corresponding server event because the request never reached the service. On the Windows setup used for this lab, refused connections may take roughly two seconds to report.

Check the configured address and container status:

```powershell
uv run supportops config show
docker compose ps
```

For DNS problems, TLS errors, and connection or read timeouts, use the [service availability runbook](service-availability.md#when-the-api-cant-be-reached). If the transport failure can't be classified reliably, leave it as unknown until you have more evidence.

## 5. Reproduce requests with JSON bodies

For requests with a body, prefer a JSON file. It is easier to inspect, share safely, and use consistently across shells.

```powershell
uv run supportops api request POST /v1/customers --data-file docs\examples\new-customer.json --yes
```

**This command creates a customer in the local lab.** Don't run it just to test connectivity.

SupportOps sets `Content-Type: application/json` unless you override it. It validates JSON before sending the request. Invalid JSON produces exit code `2`, and nothing is sent.

### PowerShell 5.1 quoting

Windows PowerShell 5.1 can remove embedded double quotes when passing inline JSON to native programs. For example:

```powershell
uv run supportops api request POST /v1/customers --data '{"name":"Acme","email":"billing@acme.example"}' --yes
```

On this project's PowerShell 5.1 setup, the CLI may receive malformed JSON. It should reject the body before sending a request and suggest `--data-file`.

This is worth checking when a request works in Postman but fails in a customer's script: the two clients may not actually be sending the same body.

### Send malformed JSON intentionally

When reproducing a customer's malformed payload, `--raw` skips local JSON validation:

```powershell
uv run supportops api request POST /v1/customers --data-file docs\examples\broken-customer.json --raw --yes
```

This deliberately sends invalid JSON to the **local lab**. It should return `400 MALFORMED_REQUEST` without creating a customer.

The repository includes these examples:

| File | Expected result |
| --- | --- |
| `docs/examples/new-customer.json` | `201 Created`; **creates a customer** |
| `docs/examples/broken-customer.json` | `400 MALFORMED_REQUEST` with `--raw` |
| `docs/examples/customer-missing-email.json` | `422 VALIDATION_FAILED` |
| `docs/examples/invoice-with-total.json` | `422 VALIDATION_FAILED` when posted to `/v1/invoices` |

## 6. Understand the write safeguards

`GET`, `HEAD`, and `OPTIONS` do not require an additional confirmation. For `POST`, `PUT`, `PATCH`, or `DELETE`, SupportOps requires all three conditions:

1. You explicitly pass `--yes`.
2. The target is a loopback address (`localhost`, `127.0.0.1`, or `::1`).
3. The service's `/health` response confirms `"environment": "lab"`.

The first two checks occur before any network traffic. Without `--yes`, the command refuses the write:

```text
Error: POST can change data on the server, so it needs --yes. Nothing was sent.
Hint: Add --yes only if you intend to change data in the local lab.
```

These guards reduce the risk of accidental writes, but they are not a substitute for checking which lab you're connected to.

SupportOps never automatically retries a write. If a connection drops or times out after submission, the operation may already have succeeded. Check the resulting resource or logs before retrying, especially for payments.

If an exercise requires resetting the lab, read the [lab reset procedure](../../README.md#resetting-the-lab) first. **A reset deletes changes in the SupportOps database volume.**

## 7. Measure response times

When a customer reports intermittent slowness, collect several measurements rather than relying on a single request:

```powershell
uv run supportops api latency /v1/invoices --count 20
```

Example output:

```text
GET http://127.0.0.1:8001/v1/invoices: 20 sequential requests
Responses: 200 x20
Successful: 20 of 20  p50 11.8 ms  p95 13.5 ms  max 18.2 ms  (threshold 1000 ms)
Slowest: supportops-latency-3ed29a-001 18.2 ms, supportops-latency-3ed29a-020 13.5 ms
All requests succeeded within the threshold.
```

The command sends sequential `GET` requests, up to 100 per run. Each request gets its own ID, so you can follow slow requests through the logs.

- **p50:** Median response time.
- **p95:** Nearest-rank 95th percentile of successful requests. With fewer than 20 successful requests, this is the slowest result.
- **max:** Longest successful request duration.

Non-2xx responses and transport failures count as problems. The command exits with `1` when any request fails or when p95 exceeds `SUPPORTOPS_SLOW_REQUEST_MS` (default: 1,000 ms) or the supplied `--threshold-ms`.

### A slow first request

Establishing a new HTTP connection takes time; subsequent requests may reuse it. If the first request is much slower than the others, investigate connection setup before assuming the API handler is slow.

For example, `http://localhost:8001` may try IPv6 first on Windows, while this lab binds to IPv4. Using `http://127.0.0.1:8001` avoids that ambiguity.

These are client-side measurements, so they include connection and network overhead, not just application processing time.

## 8. Reproduce a request with curl

If SupportOps isn't available, curl can reproduce the HTTP request directly. The examples below use a **fictional, public lab key**. Never substitute real customer credentials into commands that may be saved in shell history.

**Windows PowerShell:**

```powershell
$key = "bk_juniper01_lab_only_not_a_real_key"

curl.exe -i -H "X-Request-Id: ticket-4711" -H "Authorization: Bearer $key" http://127.0.0.1:8001/v1/account

curl.exe -i -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" `
  --data-binary "@docs/examples/customer-missing-email.json" http://127.0.0.1:8001/v1/customers
```

**Linux/macOS:**

```bash
key="bk_juniper01_lab_only_not_a_real_key"

curl -i -H "X-Request-Id: ticket-4711" -H "Authorization: Bearer $key" http://127.0.0.1:8001/v1/account

curl -i -X POST -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
  --data-binary "@docs/examples/customer-missing-email.json" http://127.0.0.1:8001/v1/customers
```

The POST deliberately omits a required field. It should return `422 VALIDATION_FAILED` without creating a customer.

Unlike SupportOps, curl does not validate JSON before sending, enforce lab-only write safeguards, or automatically redact secrets from verbose output. Be careful with any logs or command output you share, especially when using `-v`.
