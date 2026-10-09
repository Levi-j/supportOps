# Triage and escalation

Use this runbook when a customer reports a failed request, an unexpected response, or a possible service incident. The goal is to establish what happened, understand the impact, and give the right team enough evidence to act. Start with the original request ID rather than immediately repeating the operation.

The examples use the local SupportOps billing lab. The investigation commands below read logs, run predefined read-only SQL checks, or call unauthenticated health endpoints. They do not replay customer requests or modify application data.

## First 15 minutes

1. **Establish the request.** Collect the request ID, approximate failure time in UTC, endpoint, HTTP status, and a brief description of the customer's experience. Billing API responses include `X-Request-Id`; error bodies also include `request_id`. Do not request API keys, passwords, or raw Authorization headers.

2. **Investigate the request.** Run:
   ```powershell
   uv run supportops investigate REQUEST_ID
   ```
   Review the timeline, findings, cited evidence, and open questions. The command extracts relevant identifiers and, where appropriate, runs targeted read-only database checks. Its findings are leads for triage, not substitutes for reviewing the evidence.

3. **Check current availability when relevant.** For `503` responses, database failures, or reports of an ongoing outage, run:
   ```powershell
   uv run supportops health
   ```
   Treat the health result as a snapshot of the service **now**. It may not describe conditions when the customer's request failed.

4. **Assess reach and urgency.** Read the investigation's impact and coverage sections. For the billing API, it compares matching error signatures across a 30-minute window centered on the request and counts distinct request IDs and accounts, not individual log lines. For OrderFlow, access logs lack user identifiers, so SupportOps can estimate affected requests but not distinct users. If the source is incomplete, the counts are minimums, not a final impact estimate. Broaden the logs or consult another source before describing the issue as isolated.

5. **Assign severity and ownership.** Use the guidelines below together with business impact, whether the problem is ongoing, and any immediate risk of data loss or duplicate charges. Escalate early when money, security, or shared infrastructure may be affected.

6. **Update the customer.** Confirm what is known, what happens next, and when the next update can be expected. Do not wait for a confirmed root cause before acknowledging an active issue.

If a request ID cannot be found, confirm the ID's spelling and case, the source (`SUPPORTOPS_LOG_SOURCE` or an explicit `SOURCE`), and the selected time window. An `INCONCLUSIVE` result means more evidence is needed; it is not proof that the customer did not experience a failure.

## Interpreting findings

SupportOps separates evidence from diagnosis. Each finding references numbered items (`E1`, `E2`, etc.) taken from log events, predefined database checks, or health responses.

| Confidence | How to read it |
| --- | --- |
| `confirmed` | Direct evidence supports the specific finding. For a historical request this may be a matching server-side event and HTTP status; for an order-data inconsistency it may establish only the **current database state**, not what caused it. |
| `likely` | The evidence points to a cause but is incomplete, or the link depends on a current-state check rather than the state at the time of the request. |
| `possible` | The finding is supported only indirectly, such as a `401` without a corresponding `auth.rejected` event. |

Read any contradictions, interpretations, and caveats before acting. A current database result may differ from the state recorded in an older log. An inference about client-side quoting, for example, should not be presented to the customer as an observed fact. Conflicting evidence lowers confidence; when no rule can establish a useful finding, the investigation returns `INCONCLUSIVE` and identifies what remains unknown.

Invoice-level findings come from the investigated invoice's own lookup record. Database-wide consistency checks provide context, not proof that another invoice caused a request to fail. The same principle applies to OrderFlow: a confirmed inconsistency in an order's **current** data does not establish which earlier request caused it. Where results are capped or logs are incomplete, treat counts as lower bounds. See the [OrderFlow integration guide](../integrations/orderflow.md) for target-specific caveats.

## Severity guide

These are practical triage examples for the SupportOps lab, **not production SLAs or fixed escalation timelines**. Adjust severity for actual customer impact, duration, and risk.

| Severity | Typical indicators | Initial response |
| --- | --- | --- |
| **High** | Suspected duplicate charges or inconsistent payment state; a shared service or database outage; failures across multiple accounts; exposed credentials or a security concern. | Escalate promptly to engineering, the relevant on-call team, or security. Preserve evidence. For uncertain payment state, advise against retrying until it has been checked. |
| **Medium** | Recurring `500` errors affecting a limited set of requests; database timeouts with no confirmed ongoing blocker; a persistent account-specific problem without immediate financial or security risk. | Collect the request trace and current checks, involve the owning engineering team, and monitor for broader impact. |
| **Low** | A clearly attributable request or configuration error, such as malformed JSON, failed validation, an expired or revoked key, or an invoice that cannot be paid in its current state. | Guide the customer through the supported correction and verify the next result where safe. Escalate if the explanation does not fit or the pattern spreads. |

Severity is driven by impact as well as error type. A sudden increase in `401` responses across many accounts may indicate a platform problem even though a single missing or invalid key is normally a support-level issue.

## Routing and ownership

| Finding | First point of contact | Notes |
| --- | --- | --- |
| `key_revoked`, `key_expired`, `key_unknown`, `auth_header_problem` | Customer or account administrator, with support guidance | Confirm the request's logged reason; never ask for the full key. Escalate unexpected widespread failures. |
| `account_suspended` | Account management | Account restrictions require an authorized account decision. |
| `malformed_json`, `validation_failed`, `invoice_not_payable`, `resource_not_found` | Customer, with support guidance | Verify the request and permitted resource scope before recommending changes. |
| `payment_invoice_inconsistent` | Engineering / billing owner | Requires careful reconciliation; support must not edit payment or invoice records. |
| `unhandled_exception`, unexplained `5xx` | Application engineering | Provide the request trace, exception details, and impact estimate. |
| `lock_contention` | Engineering / DBA | Identify blocking sessions, but do not terminate them from the support workflow. |
| `api_cannot_reach_database` | Deployment / service on-call | The API's database path differs from the direct support-side check; inspect configuration and network reachability. |
| `database_outage` | Database / infrastructure on-call | Escalate the failed dependency and current availability evidence. |
| `order_inconsistent` | OrderFlow engineering / data owner | Escalate the current order or inventory inconsistency. Do not attribute it to the investigated request without separate evidence. |
| `insufficient_stock`, `login_failed`, `credentials_rejected` | Customer / account support initially | Check the observed response and the finding's confidence; an unexplained bearer-token rejection is only a possible diagnosis. |

For an OrderFlow investigation, explicitly select its separate configuration using `--env-file orderflow.env`. Without it, SupportOps normally uses the billing configuration, unless a shell environment variable overrides the target. OrderFlow access and database checks are read-only.

Ownership can change as new evidence emerges. If a proposed fix would require altering data, changing credentials, restarting infrastructure, or ending a database session, hand it to the authorized owner rather than performing it as a diagnostic step.

## Preparing an escalation

A useful handoff should let the next engineer understand the problem without reconstructing the investigation. Include:

- The customer-visible symptom, request ID, method, path, HTTP status, and UTC timestamp.
- The affected account and relevant resource IDs, limited to what the receiving team needs. Include only a masked key prefix, never a credential.
- Findings with confidence levels and the evidence IDs that support them; note contradictions and unanswered questions.
- Relevant log excerpts or stack traces, plus any read-only database and health results with their collection times.
- The estimated impact, time window, whether coverage was partial, and any reason the counts could be incomplete.
- Actions already taken, any immediate risk (particularly around payments), what the customer has been told, and the next-update commitment.

Generate a draft when useful:
```powershell
uv run supportops investigate REQUEST_ID --report reports/REQUEST_ID.md
```
The report is redacted and will not overwrite an existing file. The `reports/` directory is excluded from Git. **Review every draft before sharing it**: automated masking may not catch sensitive details that do not match a known pattern, and internal findings may not be appropriate for the customer.

### What to add for availability and lock incidents

**`api_cannot_reach_database` (deployment owner / on-call):**

- the health result with its time;
- liveness and readiness statuses, and whether PostgreSQL answered the support-side check;
- the API's `db.unavailable` error category;
- the `app.started` line showing the configured database host, port and user (never the password);
- when the failures began relative to the last deployment or maintenance.

Ask the owner to verify and correct the configuration, then confirm that readiness returns `200`. [INC-004](../incidents/INC-004-db-misconfigured.md) is a worked example.

**`lock_contention` (Engineering / DBA):**

- the affected request and invoice;
- the blocking session's pid, application name, role, state, transaction age and last query, from `pg.long_transactions`;
- any `pg.blocking_sessions` rows captured while a request was waiting;
- the `db.lock_timeout` log entry;
- whether a payment was recorded for the failed attempt.

Ask the session's owner to complete or safely end the transaction. Support must not cancel or terminate sessions. [INC-005](../incidents/INC-005-blocked-writes.md) is a worked example.

### Escalation handoff example
```text
Summary:       [Customer-visible failure and when it started]
Severity:      [High / Medium / Low, with rationale]
Request:       [Request ID, UTC time, METHOD /path, HTTP status]
Account:       [Authorized account ID and relevant resource IDs]
Findings:      [Finding and confidence; contradictions if any]
Evidence:      [E1 log event; E2 database check at UTC time; ...]
Impact:        [At least N distinct requests, M accounts; window and coverage]
Actions taken: [Checks completed, guidance already given]
Requested help:[Specific decision or action needed from the receiving team]
Customer:      [Last update and promised next update time]
```
## Communicating with customers

Be specific about the symptom and careful about the cause. Keep internal hostnames, stack traces, account ownership details, and other customers' information out of customer replies. When the investigation is still open, say what is being checked and give a realistic next-update time. Do not promise a fix or a refund before the responsible team has confirmed it.

For authentication issues, request a correlation ID or the non-secret key prefix rather than the full credential.

**Example: revoked key**

> Thanks for sending the request ID. The server logs indicate that the key used for this request was revoked. Please check which key your integration is configured to use and replace it with a current key through your normal account-management process. If the revocation was unexpected, let us know so we can involve your account administrator.

**Example: payment investigation in progress**

> Thanks for reporting the failed payment attempt. We're checking whether a payment was recorded despite the error response. Please don't retry this payment until we've confirmed its status, as another attempt could result in a duplicate charge. We've referred the issue to engineering and will update you by [time and time zone].

Only say that a payment was recorded, or that a problem has been resolved, once the supporting evidence has been reviewed.

## Related runbooks

- [Service availability](service-availability.md)
- [API errors and reproduction](api-errors-and-reproduction.md)
- [Logs and request IDs](logs-and-request-ids.md)
- [Database diagnostics](database-diagnostics.md)
- [Authentication](authentication.md)
