# Authentication troubleshooting

Use this runbook when an API integration returns `401 Unauthorized` or `403 Forbidden`, or when you need to verify the credentials configured for SupportOps. The examples use the fictional billing API in the local lab.

## How authentication works

Every `/v1` endpoint requires an API key in the Authorization header:

```text
Authorization: Bearer bk_juniper01_lab_only_not_a_real_key
```

Billing API keys begin with `bk_`, followed by 17–125 letters, digits, or underscores. The first 12 characters form the **key prefix** (for example, `bk_juniper01`). PostgreSQL stores this prefix and a SHA-256 hash of the complete key, not the key itself. On each request, the API uses the prefix to locate a record, then compares the hash of the submitted key with the stored hash.

| Condition | HTTP response | `auth.rejected` reason |
| --- | --- | --- |
| Authorization header is missing | `401` | `missing_header` |
| Header or key format is invalid | `401` | `malformed_header` |
| Key is not recognized | `401` | `unknown_key` |
| Key has been revoked | `401` | `revoked_key` |
| Key has expired | `401` | `expired_key` |
| Key is valid, but its account is suspended | `403 ACCOUNT_SUSPENDED` | `account_suspended` |

All key-related `401` responses use the same public error. The API records the more specific reason in its structured logs rather than disclosing it to an unauthenticated caller.

A `401` means the credentials were not accepted; it does **not** establish whether the key is unknown, malformed, revoked, or expired. A `403` means the API identified the account but denied access. In the lab, a suspended account must be addressed through the account team, not by repeatedly changing keys.

### Keys included in the lab

| Prefix | Account | State |
| --- | --- | --- |
| `bk_juniper01` | Juniper Dental Group | Active |
| `bk_juniper00` | Juniper Dental Group | Revoked |
| `bk_kestrel01` | Kestrel Logistics | Active |
| `bk_alderfin1` | Alder & Finch Studio | Key active; account suspended |

The full fictional keys end with `_lab_only_not_a_real_key`. They are development fixtures, not production credentials. The revoked Juniper key is seeded with a revocation date relative to when the lab database was created.

## Check a configured key

```powershell
uv run supportops auth check
```

The command performs three checks in order:

1. **Credential format.** It detects missing keys, accidental quotes, leading or trailing whitespace, embedded line breaks, incorrect prefixes, and invalid length or characters. Keys containing whitespace are not sent.
2. **Live authentication.** It makes a single read-only `GET /v1/account` request with a generated ID beginning `supportops-auth-`. The response establishes whether this particular key was accepted by the API.
3. **Database metadata.** When `SUPPORTOPS_DB_URL` is configured, it runs the predefined, read-only `billing.api_key_status` lookup for the key prefix.

The output masks the complete credential, showing a value such as `bk_juniper01***`. It keeps observations separate from possible explanations.

A shortened successful result might look like this:

```text
API key check for http://127.0.0.1:8001/
Key: bk_juniper01*** from SUPPORTOPS_API_KEY
Format: looks like a valid key
Live request: GET /v1/account -> 200 OK
Request ID: supportops-auth-bfdbf1690a79
Result: AUTHENTICATED

Evidence
  - The API accepted the key for Juniper Dental Group (acct_juniper).
  - A matching key prefix exists in the database and its stored state is active.
```

Request IDs, durations, and dates vary between runs.

| Exit code | Meaning |
| --- | --- |
| `0` | The API authenticated the supplied key. |
| `1` | Authentication was rejected or another diagnostic problem was found. |
| `2` | No key is configured or the configuration is invalid. |
| `3` | The API could not be reached. |

### Check a different key

Use `--key-env` to select an environment variable rather than passing a credential as a CLI argument. This example uses the lab's deliberately revoked key:

```powershell
$env:CUSTOMER_KEY = "bk_juniper00_lab_only_not_a_real_key"
uv run supportops auth check --key-env CUSTOMER_KEY
Remove-Item Env:CUSTOMER_KEY
```

For Linux or macOS, use `export CUSTOMER_KEY=...` and `unset CUSTOMER_KEY` instead. The literal value above is a public lab fixture; **do not paste a real customer's secret into a terminal command or shell history**. For real credentials, use your organization's approved secret-handling process.

The lab key should produce a `401` and a nonzero exit code. The database lookup reports a revoked record with the same prefix. That is strong supporting evidence, but it still does not prove that the complete value submitted was the stored key.

A typical explanation is:

```text
Result: REJECTED (401)

Evidence
  - The API returned 401 Unauthorized.
  - The database contains a revoked key with prefix bk_juniper00.

Interpretation
  - Revocation could explain the rejection if the supplied key is the
    same key as the stored record. Confirm the server's rejection reason.

Next steps
  - Trace the request ID in the application logs.
  - If revocation is confirmed, ask the account owner or an administrator
    to issue a current key.
```

### Confirm the reason in the logs

Copy the **request ID printed by your own `auth check` result** and trace it:

```powershell
uv run supportops logs trace supportops-auth-a6c389236b92
```

The ID above is illustrative. A matching trace for a revoked lab key contains events similar to:

```text
+0 ms  WARNING  auth.rejected  API key rejected  [authentication]
       reason=revoked_key key_prefix=bk_juniper00
+2 ms  INFO     http.request   GET /v1/account -> 401
```

The `auth.rejected` event records what the API determined about the submitted credential. It is more specific evidence than a generic HTTP `401` or a prefix-only database lookup. See [Logs and request IDs](logs-and-request-ids.md) for other tracing examples.

## Interpret the database evidence

A lookup by prefix can establish which account and key record the prefix belongs to, along with creation, expiration, and revocation information. It **cannot establish that the full key presented by the client matches that record**. A typo after character 12 leaves the prefix unchanged but produces a different hash.

| API result | Database metadata | Interpretation |
| --- | --- | --- |
| `200` | Active | The API accepted the key; the two observations are consistent. |
| `200` | Revoked or expired | The API and the diagnostic database may point to different environments. Verify configuration. |
| `401` | No matching prefix | Check for typos, an outdated key, or an environment mismatch. |
| `401` | Revoked or expired | The stored state may explain the failure; confirm the logged reason. |
| `401` | Active | The full key may differ from the stored key, or the services may use different databases. Check the logs. |
| `403 ACCOUNT_SUSPENDED` | Suspended account | Authentication identified the account, but account access is disabled. |
| Any | Database unavailable | The lookup provides no corroborating evidence; use the API response and logs. |

To inspect the stored record directly, without supplying the complete key:

```powershell
uv run supportops db run billing.api_key_status --param prefix=bk_juniper00
```

The lookup reports the stored key's lifecycle state, not whether an arbitrary credential with that prefix would authenticate.

## Common configuration problems

| Symptom | What to investigate |
| --- | --- |
| Key copied with literal surrounding quotes | `auth check` flags the quotes; the API may log `malformed_header`. |
| Trailing space or newline | The format check identifies the extra whitespace and does not send the key. |
| Key from another environment | A `401` and missing or inconsistent database metadata suggest checking the target URL and key source. |
| Placeholder remains in `.env` | Check which configuration source is active with `supportops config show`; do not print the complete key. |
| Integration still uses a rotated-out key | Look for `revoked_key` in the request trace. |
| `SUPPORTOPS_API_URL` points to the wrong service | An unexpected response, including a `404` from `/v1/account`, may indicate an incorrect target. |

Environment-file parsers may remove ordinary quoting around values. Literal quote characters often come from copied shell variables or integrations, so check the actual configuration source rather than assuming the `.env` file is at fault.

## Handle credentials safely

- Refer to credentials by masked prefix and request ID in support tickets. Never request or paste the full secret into a ticket, chat, or diagnostic command.
- Use an approved secure method for providing secrets to an integration. Do not put production keys in shell history or source control.
- Review raw logs, `curl -v` output, and exported files before sharing them. SupportOps redacts recognized sensitive patterns in its rendered output, but masking is not infallible.
- Key issuance, rotation, and revocation belong to the account owner or an authorized administrator. Support engineers should not reactivate revoked credentials.

## Escalation

**Account team:** Confirmed `403 ACCOUNT_SUSPENDED` or an account-access dispute.

**Engineering:** Authentication behavior conflicts with reliable database and log evidence; a previously revoked key appears to be accepted; or `/v1/account` repeatedly returns `5xx`.

**Security:** A complete key has been exposed in a ticket, log, repository, or other unauthorized location. Follow the organization's revocation and incident process.

For a useful handoff, include the timestamp, API URL or environment, masked key prefix, request ID, HTTP status, relevant `auth check` findings, and the server-side `auth.rejected` reason. Avoid including the credential itself.
