# Internal investigation draft: request `demo-401-revoked`

> **Internal investigation draft - not for direct customer distribution.** SupportOps generated this from logs and read-only checks for human review. It may contain sensitive operational details, internal identifiers and information about other accounts. Verify every statement against the cited evidence, and remove internal details before sharing anything with a customer. Nothing here has been sent to anyone.

## Summary

| Field | Details |
| --- | --- |
| Request ID | `demo-401-revoked` |
| Result | FINDINGS: The API key was revoked (confirmed). |
| Request | `GET /v1/account -> 401` (9 ms) |
| Request time (UTC) | `2026-10-08T09:02:00.005Z` |
| Account | `acct_juniper` |
| API key prefix | `bk_juniper00` |
| Report generated (UTC) | `2026-10-08T12:30:00.000Z` |

## Findings

### 1. The API key was revoked (confidence: confirmed)

The API logged auth.rejected with reason revoked_key for key prefix bk_juniper00 (account acct_juniper).

Evidence: [E1], [E2], [E3]

**Interpretation (not verified)**

- The key was probably rotated, and this integration still uses the old one.

**Evidence limits**

- Database results show the state when the investigation ran, not at the time of the request.

## Timeline

| Offset | Time (UTC) | Log entry |
| --- | --- | --- |
| +0 ms | `2026-10-08T09:02:00.001Z` | WARNING auth.rejected: API key rejected [account_id=acct_juniper reason=revoked_key key_prefix=bk_juniper00] |
| +4 ms | `2026-10-08T09:02:00.005Z` | INFO http.request: GET /v1/account -> 401 (9 ms) |

## Evidence

- **E1** (log entry at 2026-10-08T09:02:00.001Z, `billing-api.jsonl:4`): WARNING auth.rejected: API key rejected [account_id=acct_juniper reason=revoked_key key_prefix=bk_juniper00]
- **E2** (log entry at 2026-10-08T09:02:00.005Z, `billing-api.jsonl:5`): INFO http.request: GET /v1/account -> 401 (9 ms)
- **E3** (database check at 2026-10-08T12:30:00.000Z, `billing.api_key_status`): Key bk_juniper00 is revoked; account acct_juniper is active; created 2026-01-05, expires never, revoked 2026-07-10.

## Impact

Requests with the same error signature in the logs that were read, counted by distinct request ID rather than by log entry.

| Error signature | Requests | Accounts | Log entries | Scope |
| --- | --- | --- | --- | --- |
| `auth.rejected (revoked_key): API key rejected` | At least 1 (demo-401-revoked) | At least 1 (acct_juniper) | At least 1 | Undetermined |
| `http.request (401): GET /v1/account` | At least 1 (demo-401-revoked) | At least 1 (acct_juniper) | At least 1 | Undetermined |

**Window:** from 2026-10-08T08:47:00.001Z to 2026-10-08T09:17:00.001Z.

**Undetermined:** No other request shows this error in the logs that were read, but coverage is incomplete, so it can't be called isolated.

**Coverage:** partial for every signature above, so the counts are lower bounds:

- The logs that were read begin 12 min after the start of the 30-minute impact window.
- The logs that were read end 7 min before the end of the 30-minute impact window; later occurrences aren't counted.

## Recommended next steps

1. Ask the customer to replace the key in their integration with a current one.
2. Re-check the key's record: supportops db run billing.api_key_status --param prefix=bk_juniper00

## Escalation

No escalation is indicated by the findings.

## Open questions

- Are other requests affected? The logs that were read don't fully cover the 15 minutes before and after this request (see Impact).

## Sources and limitations

- **Application logs:** billing-api.jsonl; 22 lines read, 22 log entries parsed, 0 skipped.
- **Database:** checked as supportops_ro@127.0.0.1:5433/billing at 2026-10-08T12:30:00.000Z.
- **API health:** not checked.
- **Current state:** Database checks ran at 2026-10-08T12:30:00.000Z. They show the database at that time, not when the request was made.
- **Confidence:** *confirmed* means the request's own server-side log entry names the cause, the logged HTTP status agrees and nothing contradicts it; *likely* means the cause is stated directly but the evidence is incomplete or only linked by time; *possible* means only an indirect pattern was seen. A contradiction lowers a finding by one level.
- **Lab context:** the service reported environment 'lab'. Problems may have been simulated deliberately; this draft doesn't claim a real defect.

*Review this draft before sharing any part of it or using it for a customer-facing decision.*
