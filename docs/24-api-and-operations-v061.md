# LabTrack v0.6.1 — API Contracts, Inbox, SLA and Analytics

**Status: FINAL PRODUCT/API REQUIREMENTS for synthetic MVP, 2026-11-01.** Base `/api/v1`, OpenAPI 3.1 design contract. OPEN-01–04 are approved in [Decision Register](20-decisions-v061.md). This document defines normative requirements; current implementation status is tracked in the README and roadmap.

## 1. Approved API endpoints

| Method | Path | Purpose / authorization |
|---|---|---|
| POST | `/shipments/{shipment_id}/custody-incidents` | REGIONAL_OPERATIONS opens stalled/custody dispute incident |
| GET | `/shipments/{shipment_id}/custody-incidents` | Scoped incident list, audit trail |
| POST | `/shipments/{shipment_id}/custody-incidents/{incident_id}/attest` | Origin Sender attests Pickup facts; destination Lab staff attests Delivery facts in own account; witness policy approved |
| POST | `/shipments/{shipment_id}/custody-incidents/{incident_id}/resolve` | Region-scoped REGIONAL_OPERATIONS records independent administrative resolution; never authenticates as missing Courier |
| POST | `/shipments/{shipment_id}/containers/{container_id}/exceptional-receipts` | REGIONAL_OPERATIONS initiates destination-laboratory exceptional receipt review, **no immediate RECEIVED state** |
| POST | `/shipments/{shipment_id}/container-issues/{issue_id}/proposals/{proposal_id}/withdraw` | Original REGIONAL_OPERATIONS proposer withdraws their own PENDING proposal with mandatory reason |
| PATCH | `/shipments/{shipment_id}/receiving/items/{item_id}` | Destination LAB_OPERATOR records a result for an item in a verified RECEIVED container |
| POST | `/shipments/{shipment_id}/receiving/complete` | Destination LAB_OPERATOR finalizes receiving after all outcomes and container dispositions are complete |
| POST | `/shipments/{shipment_id}/receiving/reopen` | Destination LAB_MANAGER reopens completed receiving with a reason |
| GET | `/shipments/{shipment_id}/receiving/history` | Origin Sender or destination Lab reads receiving sessions and paginated result revisions |
| GET | `/courier/shipments/{shipment_id}/offline-manifest` | Currently assigned, active Courier obtains an immutable server-issued PHI-free snapshot |
| POST | `/courier/sync` | Currently authorized Courier submits 1–20 immutable per-command Pickup/Delivery proposals |
| GET | `/operations/stalled-shipments` | REGIONAL_OPERATIONS lists PostgreSQL-backed cases across authorized regions; scoped participants see only their own cases |
| GET | `/analytics/shipments/summary` | Active REGIONAL_ANALYST or REGIONAL_OPERATIONS reads scoped aggregates only |
| GET | `/notifications` | Recipient's Inbox, read/unread filter, limit/offset pagination |
| POST | `/notifications/{notification_id}/read` | Mark own notification read idempotently |
| GET | `/notifications/unread-count` | Number of unread Inbox items for authenticated recipient |

**No new third Sample/Container issue kind.** A CustodyIncident is an administrative review. A failed incident is not equivalent to SAMPLE REJECTED.

Example withdrawal:
```json
{"reason":"Восстановлена связь с курьером; обстоятельства уточняются"}
```

Example incident opening:
```json
{
  "phase":"DELIVERY",
  "kind":"COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE",
  "container_id":"44444444-4444-4444-8444-444444444444",
  "reason":"Контейнер физически предъявлен, подтверждение курьера недоступно"
}
```

Separate attestations (the server validates each against incident.phase):

Origin Sender (Pickup facts):
```json
{"statement":"Все контейнеры остались в учреждении","physical_release_occurred":false,"containers_at_origin":true}
```

Destination Lab witness (Delivery, personally present):
```json
{"container_code":"LT-C-EXAMPLE-02","statement":"Лично наблюдал физическое предъявление контейнера","physically_present":true}
```

For exceptional receipt, exactly two authenticated destination-laboratory employees must independently submit the Delivery form; one is an active LAB_MANAGER. The region-scoped administrative resolver is not one of these witnesses.

Regional administrative finalization:
```json
{"resolution":"EXCEPTIONAL_RECEIPT_VERIFIED","reason":"Два независимых свидетельства проверены; Pickup ранее подтверждён"}
```

Other permitted outcomes are NO_PHYSICAL_TRANSFER (Pickup only, does not withdraw automatically), ESCALATED_NO_CUSTODY_CHANGE and CONTAINER_ISSUE_REFERRED (no implicit custody change).

The server records actor identity + scoped organization/role + server timestamp itself. **Do not accept arbitrary witness user IDs or signatures for someone else in requests.**

Response of exceptional receipt initiation is an incident in OPEN/PENDING_EVIDENCE, **never** a successful Handoff. After two qualifying personally authenticated destination-laboratory witnesses (at least one LAB_MANAGER), confirmed whole-Shipment Pickup and separate regional administrative resolution, the container may transition to RECEIVED with `receipt_evidence.method=EXCEPTIONAL`. Missing Courier's Handoff remains unconfirmed/superseded.

### Final incident resolution permissions (OPEN-01–03 approved)

- `POST /custody-incidents` and `POST /containers/{container_id}/exceptional-receipts`: region-scoped **REGIONAL_OPERATIONS** starts review.
- `POST /custody-incidents/{incident_id}/attest`: for Pickup incident, active origin **SENDER_OPERATOR** attests origin facts. For Delivery case, active destination Lab users attest facts. An exceptional receipt needs **two distinct destination-lab staff**, at least one **LAB_MANAGER**; no second signature from the same user under another role. Actor is JWT caller, not a request-supplied user ID.
- `POST /custody-incidents/{incident_id}/resolve`: authenticated **REGIONAL_OPERATIONS** (not one of exceptional Lab witnesses) independently records administrative outcome. `EXCEPTIONAL_RECEIPT_VERIFIED` is allowed **only** if Shipment Pickup is CONFIRMED, both qualifying attestations exist and container is eligible; in the same transaction create receipt evidence, supersede pending Delivery (not CONFIRMED), and transition only that container to RECEIVED. No bulk tube acceptance.
- `NO_PHYSICAL_TRANSFER`: administrative conclusion for origin review with Sender attestation; **does not** mark Pickup confirmed or silently cancel. Sender must separately call the existing Pickup withdraw/cancel commands.
- `ESCALATED_NO_CUSTODY_CHANGE`: administrative review may finish without changing Shipment/Container custody. A disputed Pickup **never** becomes confirmed through the incident endpoint.
- Resolution route rejects incompatible incident kinds/phase/outcome or stale incident/evidence with 409. Existing two-stage ContainerIssue approval remains required for retry/undelivered closure.

### Final offline relevant-state dependency rule (OPEN-04 approved)

Each Courier offline command checks sealed manifest revision, **target** custody state version/attempt, whole-Shipment Pickup validity/phase, current assigned Courier and active authorization. Pickup checks its own handoff/custody phase version; Delivery checks its target Container and Delivery attempt version. Irrelevant mutations elsewhere in Shipment are not automatically a conflict; there is **no automatic rebase of any relevant change**. Target ABA (state changed and changed back) must still conflict via monotonic version.

### Draft deadline editing (required by Q12)

Introduce `PATCH /shipments/{shipment_id}` for scoped Sender in DRAFT with `{"expected_delivery_at":"2026-11-02T09:00:00Z"}` (or null while still DRAFT). The existing CreateShipmentRequest leaves this field optional, so a draft must be editable **before** it can be sealed. No changes to destination or Courier via this endpoint. `seal` rejects null or timestamp not strictly later than current authoritative server time; all validations transactional.

## 2. Existing API changes

- `CreateShipmentRequest.expected_delivery_at` nullable/optional in DRAFT; `POST /shipments/{id}/seal` rejects missing/past/non-future deadline using server clock with business error `DELIVERY_DEADLINE_REQUIRED` / `DELIVERY_DEADLINE_INVALID`. Validation on CreateShipment **and** seal ensures configured pilot destination Lab, no client change to destination.
- `ContainerIssueResolutionProposal` exposes withdrawal actor/time/reason. Add `WITHDRAWN` transition only from PENDING by original proposer; simultaneous decide/withdraw yields 409 for loser.
- `Handoff` can report `SUPERSEDED` for pending exceptional receipt; never mark absent Courier `CONFIRMED`.
- `TransportContainer` includes stable `receipt_evidence` metadata or safe reference and state_version; ordinary and exceptional evidence are distinguishable.
- `GET /courier/shipments/{id}/offline-manifest` returns `snapshot_id`, `issued_at`, sealed manifest_revision and relevant custody versions, server-scoped actor, no PHI.
- Courier `/sync` request includes snapshot_id + expected relevant target-state version and command payload. Per-command statuses include REVIEW_REQUIRED; no stale payload is executed after manual review, use new live action if safe.
- Client may keep independent offline queues on multiple devices; server serialization/idempotency controls conflicts (Q10).
- `POST /shipments/{id}/receiving/items/{item_id}` is **not** introduced; retain existing `PATCH` route. Eligibility now checks verified NORMAL **or** approved EXCEPTIONAL receipt evidence + OPEN session.
- Existing ContainerIssue MISSING/REJECTED and mixed-shipment completion rules remain unchanged. After RECEIVED, a container-level issue must not rewind custody: record REJECTED outcomes individually on affected samples.

### Stage F receiving lifecycle (implemented)

`PATCH /shipments/{shipment_id}/receiving/items/{item_id}` requires active destination `LAB_OPERATOR` membership, verified NORMAL or EXCEPTIONAL receipt evidence for the item's RECEIVED container, and an OPEN receiving session. A received container remains writable while sibling containers are unresolved. Each accepted change updates the current item projection and appends an immutable `ReceiptRevision` with actor, reason/comment, prior and new values, and server timestamp; mutation, audit and idempotency response are transactional.

`POST /shipments/{shipment_id}/receiving/complete` accepts an omitted body or `{}` (explicit JSON `null` is rejected) and requires an idempotency key. Completion requires every container terminal (`RECEIVED` or `UNDELIVERED_CLOSED`), at least one received container, results for every received item, no results for undelivered items, and one OPEN session. It closes that session and sets `COMPLETED` only when every container was received and every item is `ACCEPTED`; otherwise it sets `COMPLETED_WITH_ISSUES`.

`POST /shipments/{shipment_id}/receiving/reopen` requires an active destination `LAB_MANAGER`, a nonblank reason, and an idempotency key. It accepts only `COMPLETED` or `COMPLETED_WITH_ISSUES`, creates a new OPEN session with the next sequence, and preserves prior sessions, results and revisions. `GET /shipments/{shipment_id}/receiving/history` is visible to the origin Sender and destination Lab Operator/Manager; `limit` defaults to 20 (maximum 100), `offset` to 0, and applies to the revision history. Stage F introduced lifecycle migration `0007_stage_f_lifecycle` and integrity migration `0008_stage_f_integrity`; Stage G adds `0009_stage_g_offline`. PostgreSQL guards require an active SAMPLE reason code matching each MISSING/REJECTED result, forbid reason/comment values for ACCEPTED or unset results, and require the Shipment to be `COMPLETED` or `COMPLETED_WITH_ISSUES` before a sequence>1 reopen session can be inserted.

### Stage G Courier offline API and operations (implemented)

`GET /courier/shipments/{shipment_id}/offline-manifest` checks the active Courier assignment and authorization and persists an immutable snapshot bound to actor, assignment, Shipment and region. It contains sealed container codes/statuses and relevant versions, no patient/sample data; `issued_at` is database time. `POST /courier/sync` validates a batch of 1–20 typed commands, then commits each command independently and in request order. It rechecks current authorization before replay lookup and validates the 24-hour snapshot policy, sealed manifest revision, Pickup phase version and relevant target-container version. It does not use aggregate `Shipment.version` alone.

The sync outcomes are `APPLIED`, `ALREADY_APPLIED`, `REJECTED`, `RETRYABLE`, and `REVIEW_REQUIRED`. Authorized terminal outcomes are persisted with audit and replay identity; masked authorization denials are not recorded, and RETRYABLE is not persisted. Exact ID/body replay returns the original outcome; changed payload cannot reuse the ID. Full response and causal data remain for at least 90 days; explicit compaction retains an immutable tombstone. E2E-06's same-snapshot Pickup→Delivery sequence uses optional `depends_on_command_id`; Delivery can rely on Pickup only when it names an APPLIED command by the same actor for the same Shipment and snapshot and the live relevant versions equal that Pickup's recorded post-state. No generic rebase is allowed.

The React/TypeScript/Vite PWA stores per-user snapshots and immutable commands in IndexedDB; access tokens remain in memory, API traffic is network-only, and the service worker caches only shell assets. It reauthenticates after reload, warns and purges the scoped queue on logout/account change, and syncs in the foreground. A locally pending command is not a server custody event. Production hosting is outside Stage G.

Detailed replay compaction is available as `python -m app.cli compact-offline-replays` from `backend/`. No scheduler is installed. Handoff recommendation: Platform Operations should schedule daily execution after deployment; this cadence is proposed and must be configured by operations. Stage G verification and current branch status are in the [roadmap](19-roadmap-v06.md).

### Stage I role-workspace API additions (merged and pushed at `99caae3`)

- `GET /organizations/{organization_id}/couriers` requires an active `SENDER_OPERATOR` grant to that organization and returns only active Couriers in its active region, with stable ordering and pagination. Email addresses are not returned.
- `GET /shipments` is paginated and role-scoped: Sender Operators and Lab Managers see their organization's shipments, Lab Operators see shipments for their destination organization, and Couriers see their assigned shipments in an active Courier region.
- HTTP idempotency now includes route resource context as well as the validated request body; an identical key/body targeting another resource cannot replay the first resource's response. Migration `0011_http_request_scope` adds/backfills this context.

These additions support the local Sender and Lab workspaces and were merged and pushed to `main` at `99caae3`. Stage I was not deployed to the public demo. The dedicated real-browser acceptance and role-scope checks are described in the [test plan](25-test-plan-v061.md).

## 3. Inbox semantics (Q15)

- `GET /notifications?unread_only=true&limit=20&offset=0` returns own notifications (safe event metadata, not patient data). Optional query `unread_only` default false.
- `POST /notifications/{notification_id}/read` requires active recipient identity and ownership; setting `read_at` twice is idempotent and returns existing representation.
- `GET /notifications/unread-count` returns `{"count":N}`.
- `notifications` table deduplicates (recipient_user_id,event_id,type). Celery creates/delivers notifications best effort. Redis or Celery worker failure may delay or miss Inbox entries; stalled cases are queried from authoritative PostgreSQL and remain visible through those failures. PostgreSQL has no dashboard fallback; an outage may make dashboard APIs unavailable.
- Notifications to stage participants obey org/region scope; regional escalation excludes row-level Sample data for REGIONAL_ANALYST.

## Request correlation

Every API response includes a server-generated UUID in `X-Request-ID`. The server ignores caller-supplied values for that header and never echoes or logs them. Use the returned UUID to correlate the request with safe structured server logs.

## 4. Stalled operation monitoring (Q19)

| Workflow | Default demo threshold | Response |
|---|---|---|
| Shipment PICKUP_PENDING | 4 hours | Alert relevant Sender/Courier and escalate to REGIONAL_OPERATIONS |
| Container DELIVERY_PENDING | 4 hours | Alert assigned Courier, destination Lab and regional operations |
| ResolutionProposal PENDING | 24 hours | Alert proposer/manager and regional operations; may withdraw or decide |
| Shipment expected_delivery_at exceeded | Calendar timestamp | Overdue metric/alert if not terminal |

Thresholds are config constants, not compliance SLA. Poll/query from PostgreSQL; never auto-confirm, auto-close, or alter custody just due to timer.

`GET /operations/stalled-shipments` is available to active REGIONAL_OPERATIONS across all their authorized regions. Participants also see only their own scoped cases: SENDER_OPERATOR sees owned Shipment Pickup/overdue cases; the assigned COURIER sees their own Pickup/Delivery/overdue cases; destination LAB_OPERATOR sees Delivery/overdue cases; destination LAB_MANAGER additionally sees destination pending-proposal cases. REGIONAL_ANALYST receives no case rows. The optional `region_id` filter only narrows cases; when omitted, results may span authorized regions and each item includes its `region_id`.

Return one item per qualifying case: `PICKUP_PENDING`, `DELIVERY_PENDING`, `PROPOSAL_PENDING`, or `SHIPMENT_OVERDUE`. A Shipment may therefore occur in multiple items. `total` is the case count before pagination. Order oldest `last_event_at` first, then by case type, Shipment ID and nullable container/proposal IDs. `last_event_at` is the threshold-driving Handoff/proposal timestamp or Shipment deadline, and `age_seconds` is measured from that timestamp. Delivery cases include their `container_id`; proposal cases include both `proposal_id` and the associated `container_id`. Items contain no row-level Sample data. A notification or worker record is not required for a case to appear.

## 5. Analytics contract (Q16–Q18)

- `GET /analytics/shipments/summary?granularity=day|week&from=...&to=...`. `from` inclusive, `to` exclusive. **UTC calendar boundaries**; weeks start Monday UTC. User locale changes display only.
- Count each Shipment once according to latest current state; current Sample results use latest accepted receipt revision. Historical event/revision timeline is a **separate** event count; do not count reopening as a new Shipment.
- `average_full_shipment_delivery_seconds`: among Shipments with **every container RECEIVED**, confirmed Pickup server timestamp → latest confirmed Container Receipt server timestamp. Partially or wholly undelivered Shipments excluded from this average.
- `average_received_container_delivery_seconds`: among **RECEIVED containers in partially delivered Shipments**, confirmed Shipment Pickup → each received container's server receipt timestamp. Never treat a missing container as a successful zero duration.
- For the full-delivery mean, numerator is the sum of eligible Pickup-to-last-receipt durations and denominator is the number of Shipments with every container RECEIVED and confirmed Pickup/receipt timestamps. For the partial-delivery mean, numerator is the sum of eligible Pickup-to-container-receipt durations and denominator is the number of RECEIVED containers in partially delivered Shipments. A mean with no eligible observations is null.
- For this Stage H slice, `Shipment.created_at` selects the current-state cohort for the optional `from`/`to` range. Count `total` and `by_status` by distinct Shipment. Count Sample MISSING/REJECTED from current receipt rows after the latest accepted receipt revision; count container MISSING/REJECTED by each container's latest issue outcome.
- `partially_completed_with_issues` counts current `COMPLETED_WITH_ISSUES` Shipments with more than zero but fewer than all containers `RECEIVED`. In each period bucket, `partially_delivered_shipments` counts Shipments with more than zero but fewer than all containers `RECEIVED`, including completed mixed (`COMPLETED_WITH_ISSUES`) Shipments. `completed_shipments` counts Shipments currently `COMPLETED` or `COMPLETED_WITH_ISSUES`; `sample_issues` counts current MISSING plus REJECTED receipt rows; `container_issues` counts containers whose latest issue outcome is MISSING or REJECTED.
- Facility-level breakdown covers sender organizations and the central destination. Include each distinct sender/destination organization once per Shipment. `shipment_count` counts distinct associated Shipments, `completed_count` counts Shipments currently `COMPLETED` or `COMPLETED_WITH_ISSUES`, and `with_issues_count` counts Shipments currently `COMPLETED_WITH_ISSUES`.
- Active REGIONAL_ANALYST and REGIONAL_OPERATIONS may read aggregates only. The summary has one top-level `region_id`: infer it when exactly one active region is granted; require an authorized `region_id` when multiple are granted. An optional organization filter only narrows results within that region.
- Event timeline and as-of analytics are not equivalent: v0.6.1 current dashboard is **latest state**, and audit reports are event history.

## 6. New/relevant error codes

| Error | Meaning |
|---|---|
| `CUSTODY_INCIDENT_REQUIRED` | Physical custody disputed; cannot perform ordinary cancellation/confirmation |
| `EXCEPTIONAL_RECEIPT_NOT_READY` | Two attestations/authorization missing; no receipt transition |
| `ATTESTATION_DUPLICATE_ACTOR` | Same user attesting twice in incident |
| `PROPOSAL_NOT_WITHDRAWABLE` | Proposal not PENDING or actor not the original proposer |
| `DELIVERY_DEADLINE_REQUIRED` / `DELIVERY_DEADLINE_INVALID` | Seal requires future deadline |
| `PILOT_DESTINATION_INVALID` | Destination mismatch with configured central Lab |
| `OFFLINE_REVIEW_REQUIRED` | Snapshot expired or action uncertain: no retroactive apply |
| `OFFLINE_COMMAND_STALE` | Relevant state version changed; no automatic rebase |
| `IDEMPOTENCY_KEY_REUSED` | Same Courier command ID with changed canonical request |
| `NOTIFICATION_NOT_FOUND` | Notification outside caller's own Inbox (masked 404) |

All state-changing endpoints require a stable Idempotency-Key except Courier batch sync where each command has client_command_id; GET requires none. Use 409 for business conflicts, 422 for invalid shape, scoped 403 or masked 404 for authorization. Exceptional endpoints are implemented in Stage E; the witness, scope and concurrency requirements above remain acceptance and regression checks. Current implementation and verification status is in the [roadmap](19-roadmap-v06.md).
