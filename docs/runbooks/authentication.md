# Authentication troubleshooting

Use this runbook when an integration receives `401 Unauthorized` or `403 Forbidden`, or when you need to verify the credential SupportOps is using. The examples use the fictional [billing lab](../billing-lab.md); the [OrderFlow section](#bearer-tokens-orderflow) explains what changes for JWTs.

## How authentication works

Every billing `/v1` endpoint expects an API key in a bearer Authorization header:

```text
Authorization: Bearer bk_juniper01_lab_only_not_a_real_key
```

Billing keys begin with `bk_` and contain 20–128 characters in total. PostgreSQL stores a 12-character prefix and a SHA-256 hash, **not the complete key**. The API uses the prefix to find a candidate record and compares hashes in constant time.

| Condition | Response | Server-side `auth.rejected` reason |
| --- | --- | --- |
| Missing Authorization header | `401` | `missing_header` |
| Malformed header or key | `401` | `malformed_header` |
| Unknown key | `401` | `unknown_key` |
| Revoked key | `401` | `revoked_key` |
| Expired key | `401` | `expired_key` |
| Valid key, suspended account | `403 ACCOUNT_SUSPENDED` | `account_suspended` |

The public `401` response does not reveal why a key was rejected. The server logs the reason so it can be investigated without disclosing it to unauthenticated clients. A `403` means the account was identified but access was denied.

### Keys included in the lab

| Prefix | Fictional account | State |
| --- | --- | --- |
| `bk_juniper01` | Juniper Dental Group | Active |
| `bk_juniper00` | Juniper Dental Group | Revoked |
| `bk_kestrel01` | Kestrel Logistics | Active |
| `bk_alderfin1` | Alder & Finch Studio | Account suspended |

These are public development fixtures, not real credentials. The full sample values are documented in the [billing lab reference](../billing-lab.md#lab-api-keys).

## Check a configured key

```powershell
uv run supportops auth check
```

SupportOps checks the key's format, makes one read-only `GET /v1/account` request, and—when `SUPPORTOPS_DB_URL` is configured—looks up the stored key prefix with `billing.api_key_status`.

The output masks the key (`bk_juniper01***`) and separates observations from possible explanations. For example:

```text
API key check for http://127.0.0.1:8001/
Key: bk_juniper01*** from SUPPORTOPS_API_KEY
Format: looks like a valid key
Live request: GET /v1/account -> 200 OK
Result: AUTHENTICATED

Evidence
  - The API accepted the key for Juniper Dental Group (acct_juniper).
  - A matching key prefix exists in the database and its stored state is active.
```

The request ID is generated for each run and appears in the complete output; use it to inspect the corresponding server logs.

| Exit code | Meaning |
| --- | --- |
| `0` | Authenticated |
| `1` | Rejected or another diagnostic problem |
| `2` | Missing credential or invalid configuration |
| `3` | API unreachable |

### Check a different key

Use `--key-env` to read a credential from an environment variable instead of passing it directly on the command line. This example uses the deliberately revoked *fictional* key:

```powershell
$env:CUSTOMER_KEY = "bk_juniper00_lab_only_not_a_real_key"
uv run supportops auth check --key-env CUSTOMER_KEY
Remove-Item Env:CUSTOMER_KEY
```

Expect a `401` and a nonzero exit code. If the database is available, the matching prefix should also show a revoked record. That is supporting evidence, **not proof that the complete submitted key matches the stored key**.

For real credentials, use an approved secret-handling method. Never paste customer secrets into shell history, tickets, or shared logs.

### Confirm the reason in the logs

Copy the request ID from your own `auth check` output:

```powershell
uv run supportops logs trace YOUR_REQUEST_ID
```

A revoked-key trace should contain events resembling:

```text
+0 ms  WARNING  auth.rejected  API key rejected
       reason=revoked_key key_prefix=bk_juniper00
+2 ms  INFO     http.request   GET /v1/account -> 401
```

The `auth.rejected` reason is direct evidence of how the server handled that request, unlike a prefix-only database lookup. See [Logs and request IDs](logs-and-request-ids.md) for more tracing examples.

## Interpret the database evidence

A prefix lookup shows the stored record's account and lifecycle state. It cannot verify the rest of the submitted key; two different values can share the same first 12 characters.

| API result | Database result | What to investigate |
| --- | --- | --- |
| `200` | Active | Observations agree |
| `200` | Revoked or expired | API and SupportOps may be using different databases or environments |
| `401` | Prefix not found | Typo, outdated credential, or wrong environment |
| `401` | Revoked or expired | Stored state could explain the rejection; confirm the log reason |
| `401` | Active | Full key may differ despite sharing the prefix; inspect the logs |
| `403` | Suspended account | Account access needs an authorized account-team decision |
| Any | Database unavailable | No database corroboration; rely on API and log evidence |

To inspect a record without supplying a full key:

```powershell
uv run supportops db run billing.api_key_status --param prefix=bk_juniper00
```

## Common configuration problems

| Symptom | Next check |
| --- | --- |
| Quotes, spaces, or line breaks in the credential | `auth check` reports formatting problems; whitespace-bearing values aren't sent |
| Key from another environment | Compare the target URL and credential source with `supportops config show` |
| Integration still uses a rotated key | Find `revoked_key` in the request trace |
| `GET /v1/account` returns an unexpected `404` | Check that `SUPPORTOPS_API_URL` points to the intended API |
| API accepts a key but database metadata disagrees | Confirm that the API and diagnostic connection use the same environment |

Environment-file parsers may remove ordinary quotes around values. If the CLI reports literal quote characters, check where the value originated before editing `.env`.

## Bearer tokens (OrderFlow)

The optional [OrderFlow integration](../integrations/orderflow.md) uses short-lived JWTs instead of billing API keys. Its `auth check` has three important differences:

- **Unverified claims:** SupportOps decodes the JWT's subject, role, issuer, and timestamps locally but does not check its signature. Claims are labeled **unverified and untrusted**, never presented as established facts.
- **HTTP evidence:** `GET /api/v1/users/me` establishes whether OrderFlow accepts the token. A `401` with `WWW-Authenticate: Bearer error="invalid_token"` means a token was presented and refused. OrderFlow does not log the specific rejection reason.
- **No token database lookup:** OrderFlow does not store issued tokens, so there is no record to corroborate locally decoded claims.

An expired claim or unexpected issuer is a possible explanation, not a confirmed cause. Never ask a customer to send their token. If an authorized test account is available, inspect that token through the approved process described in the integration guide.

## Handle credentials safely

- Record the environment, request ID, and **masked** key prefix in a support ticket—not the credential itself.
- Keep production credentials out of command-line arguments, source control, and customer-facing reports.
- Review exported JSON, raw Docker logs, and `curl -v` output before sharing. Pattern-based redaction cannot guarantee that every sensitive value is removed.
- Leave key issuance, revocation, and account reactivation to authorized owners.

## Escalation

| Team | When to involve them |
| --- | --- |
| Account team | Confirmed suspended-account response or account-access dispute |
| Engineering | API authentication contradicts reliable logs or database state, or `/v1/account` repeatedly returns `5xx` |
| Security | A full credential has been exposed in an unauthorized location |

Include the timestamp, environment, masked prefix, request ID, HTTP response, relevant `auth check` findings, and any server-side `auth.rejected` reason. Never include the secret itself.
