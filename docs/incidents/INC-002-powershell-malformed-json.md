# INC-002: Customer creation fails with 400 from a PowerShell script

> **Simulated incident.** Reproduced with `supportops-lab start INC-002` in the disposable SupportOps scenario lab. The simulator sends malformed JSON matching the bytes observed in a Windows PowerShell 5.1 `curl.exe` reproduction, followed by a valid request. The Windows behavior was also tested manually against the disposable lab. All customer details are fictional; this is not a production incident.

## Incident details

| Field | Value |
| --- | --- |
| Incident ID | INC-002 |
| Reproduced (UTC) | 2026-10-09 |
| Severity | Low — request formatting requires a client-side fix ([severity guide](../runbooks/triage-and-escalation.md#severity-guide)) |
| Status | Diagnosed; customer workaround verified (simulation) |
| Service | billing-api |
| Request IDs | `inc002-cust-01` (failing), `inc002-cust-02` (fixed), `inc002-ps51-01` and `inc002-ps51-02` (manual Windows run) |
| Affected accounts | Not identified from the failing request (invalid JSON is rejected before authentication) |
| Finding | `malformed_json` — confirmed |

## Customer report

> "Creating a customer returns HTTP 400 from our PowerShell script, but the JSON looks valid."

The reported script constructs JSON inline and passes it to `curl.exe` with `-d`. The investigation focuses on what the API actually received, rather than assuming the original string reached the server unchanged.

## Expected vs. observed behavior

| | Behavior |
| --- | --- |
| Expected | `POST /v1/customers` with `{"name":"Acme","email":"billing@acme.example"}` returns `201 Created`. |
| Observed | `inc002-cust-01` returned `400 MALFORMED_REQUEST` at `2026-10-09T05:59:36.446Z`; the equivalent correctly encoded request returned `201`. |

## Reproduction

### Scenario lab (any platform)

```powershell
uv run supportops-lab start INC-002
```

```text
INC-002: Customer creation fails with 400 from a PowerShell script
...
Customer requests sent to the scenario lab at http://127.0.0.1:61880:
  inc002-cust-01  POST /v1/customers -> 400 (expected 400)
  inc002-cust-02  POST /v1/customers -> 201 (expected 201)
```

`inc002-cust-01` sends the exact 38-byte body `{name:Acme,email:billing@acme.example}`, which lacks the quotation marks required by JSON. `inc002-cust-02` sends valid JSON for the same customer. The E2E suite uses these deterministic bytes, so the test works on Linux without requiring PowerShell.

### Windows PowerShell 5.1 (manual)

The following commands were run with Windows PowerShell 5.1.26100 and `curl.exe` against the disposable lab. The key is an intentionally public, fictional Juniper test credential.

```powershell
$api = (uv run supportops-lab status --json | ConvertFrom-Json).api_url
$key = "bk_juniper01_lab_only_not_a_real_key"
curl.exe -s -i -X POST "$api/v1/customers" -H "Authorization: Bearer $key" -H "Content-Type: application/json" -H "X-Request-Id: inc002-ps51-01" -d '{"name":"Acme","email":"billing@acme.example"}'
```

```text
HTTP/1.1 400 Bad Request
content-type: application/problem+json
x-request-id: inc002-ps51-01
{"type":"about:blank","title":"Bad Request","status":400,"detail":"The request body is not valid JSON.","code":"MALFORMED_REQUEST","request_id":"inc002-ps51-01"}
```

For `inc002-ps51-01`, the server logged `content_length=38` and `Expecting property name enclosed in double quotes` at position 1. That matches the simulator's malformed bytes and demonstrates the quoting problem on this tested PowerShell 5.1 setup.

Writing the JSON to a file and sending the file works:

```powershell
[System.IO.File]::WriteAllText("$PWD\customer.json", '{"name":"Acme","email":"billing@acme.example"}')
curl.exe -s -i -X POST "$api/v1/customers" -H "Authorization: Bearer $key" -H "Content-Type: application/json" -H "X-Request-Id: inc002-ps51-02" --data-binary "@customer.json"
```

```text
HTTP/1.1 201 Created
content-type: application/json
location: /v1/customers/cus_0f5b9dd9192fdc66
x-request-id: inc002-ps51-02
{"id":"cus_0f5b9dd9192fdc66","name":"Acme","email":"billing@acme.example","created_at":"2026-10-09T05:59:37.587833Z"}
```

`WriteAllText` creates a UTF-8 file without a byte-order mark, avoiding PowerShell 5.1's default UTF-16 redirection behavior.

## Investigation

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc002-cust-01
```

```text
Investigation of request inc002-cust-01
Logs: docker:supportops-scenario-billing-api-1 (21 lines, 21 entries)
Request: POST /v1/customers -> 400 in 14 ms at 2026-10-09T05:59:36.446Z
Database: not needed for this request
API health: not checked
FINDINGS  The request body wasn't valid JSON (confirmed).
1. The request body wasn't valid JSON  [CONFIRMED]
   The API couldn't parse the request body as JSON: Expecting property name enclosed in double quotes (at position 1).
   Evidence: E1, E2
   Interpretation (not verified):
     - Property names without double quotes usually mean the client removed the quotes before sending. Windows PowerShell 5.1, for example, strips embedded double quotes when it passes a JSON string to a native program such as curl.exe.
   Next steps:
     - Ask the customer for the exact command or code that sends the request (without credentials).
     - Suggest sending the body from a UTF-8 file instead of an inline string, for example curl.exe --data-binary @body.json.
Evidence
  E1  log entry at 2026-10-09T05:59:36.445Z, docker:supportops-scenario-billing-api-1:10
      WARNING request.invalid_json: Request body is not valid JSON - Expecting property name enclosed in double quotes [error_position=1 content_type=application/json content_length=38]
  E2  log entry at 2026-10-09T05:59:36.446Z, docker:supportops-scenario-billing-api-1:11
      INFO http.request: POST /v1/customers -> 400 (14 ms)
```

No database check was needed. JSON parsing failed before authentication, so the failing request has no identified account.

## Evidence

| ID | Source and time (UTC) | Observation |
| --- | --- | --- |
| E1 | Log entry, `docker:supportops-scenario-billing-api-1:10`, 2026-10-09T05:59:36.445Z | `request.invalid_json`: `Expecting property name enclosed in double quotes`, position 1, `content_length=38`. |
| E2 | Log entry, `docker:supportops-scenario-billing-api-1:11`, 2026-10-09T05:59:36.446Z | Access log: `POST /v1/customers -> 400`. |
| Manual test | Windows PowerShell 5.1, `2026-10-09T05:59:37.556Z` | Inline `-d` request `inc002-ps51-01` returned `400` with the same decoder error; file-based `inc002-ps51-02` returned `201`. |

## Root cause and confidence

**Confirmed:** The API received a 38-byte request body without quoted property names, rejected it during JSON parsing, and returned `400` (E1–E2). Sending properly encoded JSON for the same customer returned `201`, both in the simulator and during the manual Windows reproduction.

**Likely client-side explanation:** The script passes inline JSON to `curl.exe`. On the tested Windows PowerShell 5.1 configuration, the native-command argument handling caused the embedded double quotes to be lost. The manual reproduction confirms that behavior, but the original customer's precise command and PowerShell version would still need verification. Later PowerShell versions were not tested in this incident.

**Confidence: confirmed** for `malformed_json`. The server's parsing error is direct evidence. Attribution to the customer's particular scripting environment remains an interpretation until that environment is checked.

## Impact

Within the examined window (`2026-10-09T05:44:36Z` to `06:14:36Z`), two reproduced requests showed the `request.invalid_json` signature: `inc002-cust-01` and the manual Windows request `inc002-ps51-01`. The pattern is **recurring in the test environment**. Log coverage was **partial**, so these counts are lower bounds for the captured sources.

Because the request failed before authentication, no customer account was identified from those failures. The two test requests demonstrate reproducibility; they do not establish a broader outage or the number of affected real integrations.

## Resolution or workaround

The verified workaround is to save the request body as UTF-8 JSON and send the file with `curl.exe --data-binary "@customer.json"`. The manual reproduction returned `201 Created` with that approach.

Using PowerShell's `Invoke-RestMethod` with `ConvertTo-Json` is another reasonable option, but it was **not tested** here. PowerShell 7.3+ behavior was not verified in this exercise either.

## Escalation

**Engineering escalation is not indicated:** the API correctly rejected malformed JSON. A Windows-specific example in the API documentation would make this integration problem easier to avoid.

## Customer update

> Hello,
>
> Thanks for the request details. We traced the `400` response to invalid JSON reaching the API: the property names and values arrived without the required quotation marks.
>
> We reproduced that behavior with `curl.exe` in Windows PowerShell 5.1. Sending the same JSON from a UTF-8 file with `--data-binary "@customer.json"` worked in our test environment.
>
> Please try that approach and let us know whether the request succeeds. If it still fails, share the command with any credentials removed, and we can review the quoting together.
>
> Best regards,
> Support team

*Draft for a simulated customer; no message was sent.*

## Prevention and follow-up

| Action | Owner | Status |
| --- | --- | --- |
| Add a Windows PowerShell example that sends JSON from a file to the API documentation | Documentation | Proposed |
| Mention PowerShell quoting in the `MALFORMED_REQUEST` troubleshooting guide | Support | Proposed |
| Use `supportops api request --data-file` when reproducing customer requests on Windows | Support | Ongoing |
