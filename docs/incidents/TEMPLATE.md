# INC-XXX: [Concise incident title]

> **Simulated incident.** This report documents a scenario reproduced in the SupportOps lab. State exactly how it was introduced (for example, a lab-only fault, a configuration override, or a transaction holding a lock). Do not present it as a production incident or an unplanned software defect.

## Incident details

| Field | Value |
| --- | --- |
| Incident ID | INC-XXX |
| Date (UTC) | YYYY-MM-DD |
| Severity | High / Medium / Low — see the [severity guide](../runbooks/triage-and-escalation.md#severity-guide) |
| Status | Investigating / Escalated / Resolved |
| Service | billing-api |
| Request IDs | [Relevant request IDs] |
| Affected accounts | [Account IDs, or unknown if the evidence is incomplete] |

## Customer report

> [Summarize the customer's report in their own terms. Include the observed error and approximate time, but omit credentials and personal information.]

## Expected and observed behavior

| | Behavior |
| --- | --- |
| Expected | [What the request should have done] |
| Observed | [What occurred, including HTTP status, request ID, and UTC time] |

## Reproduction

Describe how the scenario was reproduced in the lab. Include the initial conditions and the commands actually used; provide a Linux equivalent if it differs materially from PowerShell.

1. [Set up the isolated scenario or identify the relevant lab state.]
2. [Send the request or trigger the failure.]
3. [Record the result and request ID.]

[Note whether reproduction changes data or requires resetting a disposable environment. Do not imply that a state-changing test is safe against the user's existing lab.]

## Investigation

List the checks in the order they were performed. Start with `supportops investigate REQUEST_ID`, then add any focused log, API, or database checks needed to verify the result. Include concise excerpts from actual output rather than full terminal dumps.

```powershell
uv run supportops investigate REQUEST_ID
```

```text
[Relevant output excerpt]
```

## Evidence

| ID | Source and time (UTC) | Observation |
| --- | --- | --- |
| E1 | [Log event, file:line, timestamp] | [What the log directly records] |
| E2 | [Database check, execution time] | [Observed database state at the time of the check] |
| E3 | [API/health check, execution time] | [Observed response or availability] |

Reference evidence IDs in the findings below. Distinguish historical log events from database and health checks performed later. Do not include secrets, full credentials, customer data, or unrelated account details.

## Root cause and confidence

**Established facts:** [What the evidence directly demonstrates, citing E1, E2, etc.]

**Interpretation:** [The likely explanation, why it fits the evidence, and what would still need to be checked. If the cause is not established, say so explicitly.]

**Confidence:** Confirmed / Likely / Possible — [Brief justification, including any contradictions or gaps.]

## Impact

[Describe the affected operation, distinct requests and accounts, and the period examined (UTC). State whether log coverage is complete or partial. Treat counts from incomplete logs as lower bounds, and do not describe an incident as isolated unless the available coverage supports that conclusion.]

## Resolution or workaround

[Document the action taken and its result. If unresolved, record the safest available workaround and the outstanding owner or decision. For payment discrepancies, avoid recommending retries until the payment state has been verified.]

## Escalation

[Receiving team or owner, time of handoff (UTC), reason for escalation, evidence shared, requested action, and any remaining risks. If no escalation was needed, explain why.]

## Customer update

> [A concise, customer-safe response: acknowledge the issue, state verified facts, explain the next action, and give a specific next-update time when follow-up is pending. Avoid internal infrastructure details, other accounts' data, and unverified root-cause claims.]

## Prevention and follow-up

| Action | Owner | Status |
| --- | --- | --- |
| [Code, configuration, monitoring, or documentation improvement] | [Team or role] | Open / In progress / Done |

[Record any remaining verification needed to close the incident.]
