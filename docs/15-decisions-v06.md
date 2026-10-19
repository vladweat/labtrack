# LabTrack v0.6 — Final Decision Register

> **Исторический базовый документ v0.6.** v0.6.1 supersedes contradictory requirements; current decisions and API are in [Decision Register v0.6.1](20-decisions-v061.md), [Custody Exceptions](21-custody-exceptions-v061.md), [Offline Integrity](22-offline-integrity-v061.md), [DB Delta](23-data-model-v061.md), [API](24-api-and-operations-v061.md). Keep v0.6 rules only where unchanged.

**Status:** accepted questionnaire answers + explicit follow-up approval, 2026-11-01.  
**Authority:** decisions below supersede conflicting v0.2–v0.5 assumptions; derived technical policies are labelled as such. At the time this v0.6 decision document was written, the project was documentation-only and had no backend implementation.

## 1. Previously approved baseline

- Pilot: **one region, one central laboratory**, multiple originating medical organizations, synthetic data only; each Shipment goes to that laboratory.
- Individual physical Sample inside a uniquely labelled TransportContainer. One Sample belongs to one ContainerItem and cannot be reshipped after Shipment cancellation in MVP.
- Pickup remains **whole-Shipment** bilateral Sender → assigned Courier confirmation. Delivery remains bilateral Courier → destination Lab, but **each container has its own delivery handoff**, allowing mixed outcomes in one Shipment.
- Two exception kinds only: `MISSING` / `REJECTED` at SAMPLE or CONTAINER level; reason code required for both kinds, comment optional. Neither successful container handoff nor a container issue automatically determines the outcomes of its tubes.
- Sender assigns/replaces Courier only in DRAFT. Bootstrap CLI creates organizations and users; Org Admin manages scoped memberships.
- Lab Operator may correct tube receipt in an open receiving session (with revision and audit); LAB_MANAGER can reopen a finished receiving session with mandatory reason and no time limit.
- Short-lived JWT + Argon2id, PostgreSQL sealed-manifest triggers, manually entered expected_delivery_at, Celery/Redis best-effort, Courier offline queue, opaque synthetic 1:N Sample external references, CI/tests/observability for production-like MVP.

## 2. Newly accepted answers (14/14)

| ID | Priority | Approved decision |
|---|---|---|
| Q01 | P0 | A refused container stays in the Courier's responsibility until a manual resolution takes effect. |
| Q02 | P0 | `CLOSED_UNDELIVERED` is final. Subsequent physical disposition outside LabTrack; do not imply an in-system return. |
| Q03 | P0 | **Two-stage** issue resolution: region-scoped REGIONAL_OPERATIONS proposes action, destination LAB_MANAGER approves it. |
| Q04 | P0 | **Partial delivery is allowed:** remaining containers may be handed off and their tubes received while other containers are missing/refused. |
| Q05 | P0 | Fixed reason codes are seeded by migration/bootstrap; no admin UI for editing codes in MVP. |
| Q06 | P0 | LAB_MANAGER may reopen completed receiving with no automatic time limit, reason + audit mandatory. |
| Q07 | P0 | One LAB_MANAGER approval suffices for reopening; no second manager required. |
| Q08 | P0 | Offline commands older than **24 hours** require manual review; do not auto-confirm. |
| Q09 | P0 | If server state changed while offline, reject the command and require manual conflict handling; never silently rebase. |
| Q10 | P0 | Retain both claimed device action timestamp and authoritative server acceptance time; show both in UI. |
| Q11 | P1 | `Sample 1:N SampleExternalReference`, typed opaque references; no real LIS integration. |
| Q12 | P1 | Notifications only inside the application: Dashboard and event stream; no Email/Telegram in MVP. |
| Q13 | P1 | Alert participants of the stuck handoff stage; escalate to regional operations if the allowed wait is exceeded. |
| Q14 | P1 | Expanded analytics: facility breakdown, time-series trends, average delivery time, incident frequencies and status counts. |

**Follow-up user approval:** Sample receiving is permitted **only inside individually received containers whose Courier and Lab handoff is confirmed by different authorized users**. Other containers retain distinct custody and disposition.

## 3. Scope correction vs v0.5

| v0.5 design | v0.6 decision |
|---|---|
| All-or-nothing delivery; any container issue blocks whole Shipment | **Per-container** Delivery; the unaffected containers may be received independently |
| Regional operations unilaterally resolves a blocked Shipment | REGIONAL_OPERATIONS **proposes** a resolution; LAB_MANAGER **approves** before it has any effect |
| Only full-Shipment delivery permits `RECEIVING` | Received containers can be opened for tube-level receiving while other containers remain in transit or blocked |
| v0.5 `/container-issues/resolve` returns an immediately applied resolution | Replace with explicit proposal / approval endpoints, no side effect upon proposal |
| v0.5 single `COURIER_CONFIRM_DELIVERY` for all containers | Courier initiates delivery separately for each container; whole-Shipment Pickup remains unchanged |
| v0.5 lacks final disposition for mix of received + permanently undelivered containers | Explicit terminal policy below, subject to tests and product review |

## 4. Derived engineering policies (not separate answers)

These fill necessary implementation gaps without claiming a further user vote:

1. **Finalization with mixed outcomes:** if some containers are RECEIVED and all others are either RECEIVED or UNDELIVERED_CLOSED, finish as `COMPLETED_WITH_ISSUES` after recording an explicit result for **every tube of each RECEIVED container**. Tubes of UNDELIVERED_CLOSED containers retain `receipt_result=NULL`; do not fabricate tube-level MISSING. If **none** of the containers were received and all were closed, Shipment becomes final `CLOSED_UNDELIVERED`. If every container was received and every tube ACCEPTED, finish `COMPLETED`; if any tube MISSING/REJECTED, `COMPLETED_WITH_ISSUES`.
2. **Actor separation:** the approver of a resolution must not be the proposer (different `user_id`); the approving LAB_MANAGER belongs to the destination laboratory. Proposal alone does not change Container or Shipment delivery status.
3. **One resolution at a time:** each OPEN container issue may have one PENDING resolution proposal. Rejected proposals stay in audit/history and require a new proposal.
4. **24h offline threshold:** pending actions older than 24 hours must be visibly flagged for review. Once received by the server, any expired or conflicting command must not mutate custody state before an authorized manual decision. If the device never connects, the server cannot know the unseen command; it detects stalled server-side handoffs instead.
5. **No more than one open receiving session** per Shipment. First confirmed container delivery opens the session; its tube outcomes can be recorded immediately. Overall completion waits for disposition of every container.
6. **Lock boundary:** every container-specific mutation locks the parent Shipment before its children. The sealed membership/identity of containers and samples remains immutable.

These are the **recommended v0.6 implementation defaults**. They are explicit and reviewable, particularly the terminal mixed-delivery policy and the precise offline-review release workflow.

## 5. Still needing real-world operational/compliance review

- What legally constitutes physical custody and evidence for refused containers. The system records an operational claim, not a qualified digital signature.
- Who may authorize delayed offline commands after human investigation, and what additional evidence must be gathered. The MVP may reject and require a fresh online handoff rather than retroactively approve one.
- Final reason-code values; examples are illustrative seed entries until a catalog is signed off.
- Real data retention, device security, recovery targets, medical rules and timestamp semantics. The current product operates only on synthetic test data.

## 6. Normative v0.6 documents

- [Per-container workflows](16-domain-workflows-v06.md)
- [Data model and concurrency](17-data-model-v06.md)
- [REST API and offline changes](18-api-v06.md)
- [Implementation and acceptance](19-roadmap-v06.md)
- [OpenAPI v0.6 draft](../openapi/labtrack-v1.json)

Older design documents remain as evolution history and must not be used as v0.6 source-of-truth where they conflict with this register.
