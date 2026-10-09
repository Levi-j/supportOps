# INC-001: Integration receives 401 after an API key rotation

> **Simulated incident.** Reproduced with `supportops-lab start INC-001` in the disposable SupportOps scenario lab. The setup revokes Kestrel Logistics' existing test key, records the revocation as two days earlier, and creates a replacement. The simulated integration continues to use the revoked key. All accounts and credentials are fictional; no production service was involved.

## Incident details

| Field | Value |
| --- | --- |
| Incident ID | INC-001 |
| Reproduced (UTC) | 2026-10-09 |
| Severity | Low — one integration requires a credential update ([severity guide](../runbooks/triage-and-escalation.md#severity-guide)) |
| Status | Diagnosed; customer action identified (simulation) |
| Service | `billing-api` |
| Request IDs | `inc001-cust-01`, `inc001-cust-02` |
| Account | `acct_kestrel` (Kestrel Logistics) |
| Finding | `key_revoked` — confirmed |

## Customer report

> "Our integration suddenly started returning 401 responses. We haven't changed our application."

Both reproduced requests received `401 Unauthorized`. The API deliberately returns the same `UNAUTHENTICATED` error for different credential failures, so the response alone cannot establish why the key was rejected.

## Expected vs. observed behavior

| | Behavior |
| --- | --- |
| Expected | `GET /v1/invoices` with an active, authorized key returns `200` and the account's invoices. |
| Observed | `GET /v1/invoices` and `GET /v1/account` returned `401 UNAUTHENTICATED`. The first request, `inc001-cust-01`, was logged at `2026-10-09T05:59:06.433Z`; `inc001-cust-02` followed. |

## Reproduction

Run the scenario from the repository root. It recreates **only the disposable scenario lab**, not the persistent `supportops` environment.

```powershell
uv run supportops-lab start INC-001
```

Selected output from the 2026-10-09 reproduction:

```text
INC-001: Integration receives 401 after an API key rotation
Customer report: "Our integration suddenly started returning 401 responses. We haven't changed our application."
...
Customer requests sent to the scenario lab at http://127.0.0.1:58573:
  inc001-cust-01  GET /v1/invoices -> 401 (expected 401)
  inc001-cust-02  GET /v1/account -> 401 (expected 401)

Investigate:
  uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc001-cust-01
```

Docker assigns a new host port to the disposable lab on each run. The generated environment file provides the correct API, database, and log targets.

## Investigation

### 1. Trace the failed request and collect evidence

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env investigate inc001-cust-01
```

The following is a trimmed excerpt; the impact assessment appears separately below.

```text
Investigation of request inc001-cust-01
Logs: docker:supportops-scenario-billing-api-1 (13 lines, 13 entries)
Request: GET /v1/invoices -> 401 in 22 ms at 2026-10-09T05:59:06.433Z, account acct_kestrel
Database: checked as supportops_ro@127.0.0.1:58571/billing at 2026-10-09T05:59:07.231Z
API health: not checked

FINDINGS  The API key was revoked (confirmed).

1. The API key was revoked  [CONFIRMED]
   The API logged auth.rejected with reason revoked_key for key prefix bk_kestrel01 (account acct_kestrel).
   Evidence: E1, E2, E3
   Interpretation (not verified):
     - The key was probably rotated, and this integration still uses the old one.

Evidence
  E1  log entry at 2026-10-09T05:59:06.432Z, docker:supportops-scenario-billing-api-1:10
      WARNING auth.rejected: API key rejected [account_id=acct_kestrel reason=revoked_key key_prefix=bk_kestrel01]
  E2  log entry at 2026-10-09T05:59:06.433Z, docker:supportops-scenario-billing-api-1:11
      INFO http.request: GET /v1/invoices -> 401 (22 ms)
  E3  database check at 2026-10-09T05:59:07.231Z, billing.api_key_status
      Key bk_kestrel01 is revoked; account acct_kestrel is active; created 2026-02-11, expires never, revoked 2026-10-07.
```

### 2. Check the key's database record

The catalog query uses the 12-character prefix; it does not retrieve or expose the complete API key.

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env db run billing.api_key_status --param prefix=bk_kestrel01
```

```text
INFO     billing.api_key_status  Found the API key with prefix bk_kestrel01.  (8 ms)
         key_prefix      bk_kestrel01
         key_id          key_kestrel_main
         label           Dispatch integration
         account_id      acct_kestrel
         account_name    Kestrel Logistics
         account_status  active
         created_at      2026-02-11 05:59:00 UTC
         expires_at      -
         revoked_at      2026-10-07 05:59:06 UTC
         key_status      revoked
```

### 3. Review the request timeline

```powershell
uv run supportops --env-file .lab\supportops-scenario\supportops.env logs trace inc001-cust-01
```

```text
2 log entries for request ID inc001-cust-01 in docker:supportops-scenario-billing-api-1
Access log: GET /v1/invoices -> 401 in 22 ms
       +0 ms  WARNING  auth.rejected  API key rejected   [authentication]
              account_id=acct_kestrel reason=revoked_key key_prefix=bk_kestrel01
       +1 ms  INFO     http.request   GET /v1/invoices -> 401 (22 ms)
```

## Evidence

| ID | Source (UTC) | Observation |
| --- | --- | --- |
| E1 | `docker:supportops-scenario-billing-api-1:10`, `2026-10-09T05:59:06.432Z` | `auth.rejected` identified `revoked_key` for prefix `bk_kestrel01` and account `acct_kestrel`. |
| E2 | `docker:supportops-scenario-billing-api-1:11`, `2026-10-09T05:59:06.433Z` | The same request, `inc001-cust-01`, returned `401`. |
| E3 | `billing.api_key_status`, checked at `2026-10-09T05:59:07.231Z` | The key was recorded as revoked on 2026-10-07; the account was active at investigation time. |

## Root cause and confidence

**Confirmed:** The API rejected the key because it was revoked (E1), and the request returned `401` (E2). The read-only database lookup corroborated the revoked status (E3). The second request was also rejected with the same reason.

**Not established:** The investigation does not show who revoked the key or why. The replacement key is known from the scenario setup, not independently established by the request's evidence. A missed integration update during rotation is a plausible explanation, not a proven historical fact.

**Confidence: confirmed** for the revocation finding. The matching server log and HTTP result are direct evidence. The database lookup reflects the state when the check ran, not a reconstruction of the earlier request.

## Impact

Within the investigated 30-minute window (`2026-10-09T05:44:06Z` to `06:14:06Z`), the `auth.rejected (revoked_key)` signature appeared for **two distinct requests** from **one identified account**, `acct_kestrel`. The pattern is recurring within the available logs.

**Coverage was partial:** the scenario logs begin approximately 14 minutes after the window starts and end approximately 14 minutes before it ends. The observed counts are therefore lower bounds. No additional affected account was identified in the logs examined; this does not establish service-wide scope.

## Resolution or workaround

The appropriate next step is for the account owner to update the integration with a current key through their normal credential-management process. Support can then confirm whether a new request succeeds. Support should neither reactivate a revoked key nor ask the customer to disclose its full value.

## Escalation

**Engineering escalation is not indicated** by the observed behavior. If the revocation was unexpected, support should refer the question to the account administrator or account-management team with the request IDs, safe key prefix, and recorded revocation date.

## Customer update

> Hello,
>
> Thanks for sharing the request IDs. We traced the 401 responses to an API key that has been revoked. The key prefix is `bk_kestrel01`, and our records show that it was revoked on 7 October. Your account itself is active.
>
> Please update the integration to use a current key from your account's credential-management process, then try the request again. If the revocation was unexpected, we can help your account administrator investigate when the key was changed.
>
> Best regards, \
> Support team

*Draft for a simulated customer; no message was sent.*

## Prevention and follow-up

| Action | Owner | Status |
| --- | --- | --- |
| Include every dependent integration in the key-rotation checklist. | Customer / integration owner | Recommended |
| Evaluate a planned transition window for key rotations where security policy permits. | Product / account management | Proposed |
| Use request IDs and safe key prefixes, never full credentials, in support handoffs. | Support | Ongoing |
