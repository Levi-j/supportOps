# API errors and request reproduction

Use this runbook when a customer reports an unexpected API response, an intermittent error, or slow requests. The goal is to reproduce the observation safely, capture a request ID, and connect the response to server-side evidence before recommending a fix.

If many endpoints are failing at once, start with [Service Availability](service-availability.md). A shared dependency issue may explain what appears to be several separate problems.

## Before beginning

Confirm the API and credential source before sending a request:

```powershell
uv run supportops config show
```

For the local billing lab, `SUPPORTOPS_API_URL` normally points to `http://127.0.0.1:8001`. Credentials are masked in configuration output. Use `--key-env NAME` to read a different key from an environment variable; never pass a real credential as a command-line argument.

The examples below assume the **fictional local billing lab**. Treat any `POST` example as a data-changing operation unless explicitly described otherwise.

## 1. Reproduce the request

Start with a read-only request and an ID you can trace:

```powershell
uv run supportops api request GET /v1/account --request-id ticket-4711
```

A successful response includes the status, duration, request ID, masked credential source, headers, and body. For example:

```text
GET http://127.0.0.1:8001/v1/account
200 OK in 17 ms
Request ID: ticket-4711 (echoed by the server)
Credentials: API key bk_juniper01*** from SUPPORTOPS_API_KEY
```

Record the status and timing, whether the server echoed the request ID, and any useful headers such as `content-type`, `location`, `retry-after`, `www-authenticate`, or `allow`. Compare the response body with the customer's report rather than assuming the status tells the whole story.

Use `--full` if the default 4,000-character body preview is insufficient, or `--json` when you need structured output. Review either for customer information before sharing it.

A typical API error response returns exit code `1`; successful responses return `0`. Invalid usage, configuration, or a refused write returns `2`.

## 2. Interpret the response

The billing API uses [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457). Its `code` field helps distinguish failures that have the same HTTP status.

| Response | What to check first |
| --- | --- |
| `3xx` | `Location`, URL, and whether a redirect is expected; SupportOps does not follow redirects automatically |
| `400 MALFORMED_REQUEST` | Invalid JSON syntax or `Content-Type` |
| `401 UNAUTHENTICATED` | Missing, unknown, expired, or revoked API key; inspect the request trace |
| `403 ACCOUNT_SUSPENDED` | Account state; a new key won't reactivate the account |
| `404 RESOURCE_NOT_FOUND` | Path, ID, and tenant; another tenant's resource is intentionally hidden |
| `409 INVOICE_NOT_PAYABLE` | Invoice state and whether a previous payment succeeded |
| `422 VALIDATION_FAILED` | Missing or invalid fields in otherwise valid JSON |
| `500 INTERNAL_ERROR` | Server exception and earlier events for the request ID |
| `503 SERVICE_UNAVAILABLE` / `DATABASE_BUSY` | Health, database connectivity or lock contention, and `Retry-After` |

`400` means the JSON could not be parsed; `422` means it was valid JSON but failed endpoint validation. For a `503`, also run:

```powershell
uv run supportops health
```

An HTTP response is an observation, not necessarily a root-cause explanation.

## 3. Trace the request ID through server logs

The billing API deliberately returns a generic `401` for multiple credential failures. To see the server-side reason, reproduce one with a **fictional lab key**:

```powershell
$env:INVALID_LAB_KEY = "bk_placeholder0000000000000000000000"
uv run supportops api request GET /v1/account --key-env INVALID_LAB_KEY --request-id docs-401-1
Remove-Item Env:INVALID_LAB_KEY

uv run supportops logs trace docs-401-1
```

Look for `auth.rejected` with `reason=unknown_key`, followed by an `http.request` event for `GET /v1/account -> 401`. The trace preserves chronological order and does not reveal the full key.

To inspect the original Docker logs without changing the service:

```powershell
docker compose --project-name supportops logs --no-log-prefix billing-api | Select-String "docs-401-1"
```

In Linux/macOS, replace `Select-String` with `grep`. Other authentication reasons include `missing_header`, `malformed_header`, `revoked_key`, and `expired_key`. See [Authentication troubleshooting](authentication.md) before concluding that a credential should be replaced.

## 4. When the API doesn't respond

DNS errors, refused connections, and timeouts happen before an HTTP response is available. The request ID may have **no matching server log entry** if nothing reached the API.

Check the effective address and the lab container status:

```powershell
uv run supportops config show
docker compose --project-name supportops ps
```

Inspect the failure type: `dns_failure`, `connection_refused`, `connect_timeout`, `read_timeout`, `tls_error`, or `connection_closed`. Each points to a different next check. The [service availability runbook](service-availability.md#when-the-api-cannot-be-reached) covers these in detail.

## 5. Reproduce requests with JSON bodies

Prefer checked-in example files over inline JSON, especially in Windows PowerShell 5.1:

```powershell
uv run supportops api request POST /v1/customers --data-file docs\examples\new-customer.json --yes
```

**This creates a customer in the local lab.** Don't run it just to check connectivity. SupportOps validates JSON before sending it; invalid JSON is refused locally with exit code `2`.

### PowerShell 5.1 quoting

PowerShell 5.1 may change embedded double quotes when passing inline JSON to a native process. A command such as:

```powershell
uv run supportops api request POST /v1/customers --data '{"name":"Acme","email":"billing@acme.example"}' --yes
```

may arrive at the CLI differently than expected. Use `--data-file` to avoid shell-quoting ambiguity. This is useful when a customer's script fails while the same request works in Postman.

### Send malformed JSON intentionally

`--raw` skips local JSON validation so you can reproduce a malformed client request **in the verified local lab**:

```powershell
uv run supportops api request POST /v1/customers --data-file docs\examples\broken-customer.json --raw --yes
```

This should return `400 MALFORMED_REQUEST` without creating a customer.

| Example file | Expected response |
| --- | --- |
| `docs/examples/new-customer.json` | `201`; creates a local customer |
| `docs/examples/broken-customer.json` | `400` when used with `--raw` |
| `docs/examples/customer-missing-email.json` | `422` |
| `docs/examples/invoice-with-total.json` | `422` when posted to `/v1/invoices` |

## 6. Understand the write safeguards

For the **billing target**, SupportOps permits `POST`, `PUT`, `PATCH`, and `DELETE` only when all of these conditions hold:

1. You supplied `--yes`.
2. The target URL uses a loopback host (`127.0.0.1`, `localhost`, or `::1`).
3. Its `/health` response identifies the service as `environment: lab`.

Without confirmation or a permitted target, SupportOps refuses the write. It also refuses credential-bearing custom headers. The optional **OrderFlow target is read-only**, so write methods are rejected even with `--yes`.

A write that times out may already have succeeded; SupportOps deliberately does **not** retry it automatically. Check the record and server logs before repeating customer creation, payments, or other state-changing requests.

Only reset the billing lab when you explicitly intend to discard its stored changes. See the [README reset procedure](../../README.md#resetting-the-lab) for the consequences.

## 7. Measure response times

```powershell
uv run supportops api latency /v1/invoices --count 20
```

The command makes up to 100 **sequential GET** requests and reports status counts, median (`p50`), 95th percentile (`p95`), maximum response time, and the slowest request IDs.

```text
Responses: 200 x20
Successful: 20 of 20  p50 11.8 ms  p95 13.5 ms  max 18.2 ms
All requests succeeded within the threshold.
```

These are illustrative timings, not performance guarantees. Non-2xx responses and transport failures count as problems, and the command exits with `1` when any request fails or the successful-request `p95` exceeds the configured threshold (default: 1,000 ms). This is a lightweight diagnostic—not a load test.

### A slow first request

Connection setup can make the first request slower than subsequent requests. The lab binds to IPv4; on some Windows systems, `localhost` may try IPv6 first. Prefer `127.0.0.1` for repeatable local measurements. Client-side timings include connection and network overhead, not just application execution.

## 8. Reproduce a request with curl

If SupportOps isn't available, you can use curl to compare what a client actually sends. These examples use **public fictional lab credentials only**.

**Windows PowerShell 5.1:**

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

The POST deliberately omits a required field and should return `422` without creating a customer. Unlike SupportOps, curl does not validate the body beforehand, check lab-only write permissions, or redact credentials. Review any verbose output before sharing it.
