# LabTrack v0.6.1 — Approved Custody Incidents & Exceptional Receipt

**Status: APPROVED FOR IMPLEMENTATION (synthetic MVP), 2026-11-01.** OPEN-01–03 closed in [Decision Register](20-decisions-v061.md). This is an implementable *specification*, not deployed functionality, legal proof or a digital signature.

## 1. Normal and exceptional receipt evidence

Normal Pickup remains Shipment-wide, separately confirmed by Sender and assigned Courier; Normal Delivery remains per-container, initiated by Courier and confirmed by destination Lab. Their roles and original audit identities are immutable.

**Exceptional Delivery** applies only to a **physically presented** sealed container when the assigned Courier cannot initiate/finish their Delivery confirmation. Two independent, physically present employees of the destination Lab, **at least one LAB_MANAGER**, each attest personally under their own authenticated account. A region-scoped REGIONAL_OPERATIONS member records the separate administrative decision, which may create EXCEPTIONAL receipt evidence. **Never create or modify a Courier-signed CONFIRMED Handoff on their behalf.**

A person with both roles counts as a single witness; witness actors cannot equal one another or the regional resolver.

## 2. Entity and event model

- `CustodyIncident`: id, shipment_id, container_id nullable, handoff_id nullable, phase PICKUP/DELIVERY, kind, status, reason, opened_by/at, resolved_by/at, outcome, audit. Types are administrative, **not** extra Sample/Container discrepancy kinds.
- `CustodyAttestation`: incident_id, actor user_id, org_id/role snapshot, assertion of observed facts, container code if relevant, server timestamp; unique (incident_id,user_id), append-only.
- `ContainerReceiptEvidence`: unique container_id; method NORMAL (confirmed_delivery_handoff_id) **xor** EXCEPTIONAL (resolved_incident_id); verified_by, server verified_at, immutable.
- `Handoff.status=SUPERSEDED`: only a **PENDING DELIVERY** attempt can be superseded by authorized exceptional receipt. Existing CONFIRMED handoffs remain immutable. A missing Courier is never recorded as confirmed.
- Proposed incident statuses `OPEN → PENDING_EVIDENCE → UNDER_REVIEW → RESOLVED` or `REJECTED`. A `RESOLVED` incident records a specific outcome, which **may mean administrative closure without changing custody**.

Suggested incident kinds: COURIER_UNAVAILABLE_AFTER_PICKUP, PICKUP_UNCONFIRMED, COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE, HANDOFF_STALLED, OFFLINE_CONFLICT.

## 3. Final authorization / outcome matrix (OPEN-03)

| Phase / scenario | Opens review | Required attestations | Administrative finalizer | Permitted effect |
|---|---|---|---|---|
| Unfinished Pickup, Courier unavailable | REGIONAL_OPERATIONS of Shipment region | Origin `SENDER_OPERATOR` attests location / whether physical release occurred | REGION-scoped `REGIONAL_OPERATIONS` | If no physical release, Sender uses **existing** Pickup withdraw and possibly pre-pickup cancel. Incident resolution cannot confirm Pickup. If custody disputed, record `ESCALATED_NO_CUSTODY_CHANGE` |
| Long-stalled ordinary Delivery with no physical receipt proof | REGIONAL_OPERATIONS | Destination LAB_MANAGER confirms what lab physically observed | REGIONAL_OPERATIONS | Review only: no fake Delivery confirmation; use Container MISSING/REJECTED and approved two-stage issue resolution as applicable, else `ESCALATED_NO_CUSTODY_CHANGE` |
| Container physically present; Courier confirmation unavailable | REGIONAL_OPERATIONS | **Two different active destination-Lab staff, physically present; at least one LAB_MANAGER** | REGIONAL_OPERATIONS (not either witness) | If full Shipment Pickup was **already CONFIRMED** and other guards pass, create EXCEPTIONAL evidence and move only that container to RECEIVED atomically |
| Courier absent after confirmed whole-Shipment Pickup, undelivered container | REGIONAL_OPERATIONS, or normal scoped report of container MISSING | Lab-side observations as applicable, or existing issue reporting evidence | REGIONAL_OPERATIONS reviews, while **separate** ContainerIssue approval still requires distinct LAB_MANAGER | Only normal ContainerIssue RETRY_DELIVERY / CLOSE_UNDELIVERED route changes a blocked container; do not reassign Courier |
| Offline stale command / disputed physical action | REGIONAL_OPERATIONS | Sender for origin-side facts or LAB_MANAGER for destination facts | REGIONAL_OPERATIONS | Investigate; if current state permits, **new live authenticated operation** by rightful actor, otherwise incident remains unresolved/escalated. Never execute original stale payload retroactively |

**Distinct roles and authorizations:** actor identity is from JWT; server verifies current active membership and Shipment region/destination. Region opener may also be the administrative finalizer if still authorized, but **may not count as an attesting laboratory witness**. For normal ContainerIssue resolution the existing separate proposer/approver rule still applies; administrative incident closure cannot bypass that second-person approval.

## 4. Exceptional receipt transaction (OPEN-01 / OPEN-02)

Required preconditions:
1. Manifest sealed, exact container belongs to Shipment, and **whole-Shipment Pickup was already confirmed by normal Sender/Courier handoff**. A pending, cancelled, or disputed Pickup prohibits exceptional receipt **even if incident review exists**. Incident resolution never fabricates a confirmed Courier Pickup.
2. Container is IN_TRANSIT or DELIVERY_PENDING and not previously RECEIVED, CLOSED_UNDELIVERED or otherwise terminal; no verified receipt evidence already exists.
3. Incident opened by REGIONAL_OPERATIONS for that container, linked to unavailable-Courier case, with audit and reason; all attestations refer to the same physical container ID/code.
4. Two distinct witness accounts, both active members of **destination laboratory**, at least one active LAB_MANAGER; both independently attest actual physical presentation; each is physically present (human assertion recorded; software cannot independently prove physical presence).
5. REGIONAL_OPERATIONS administrative resolver is **not one of witnesses** and is currently authorized in Shipment region; current scope/identity and original handoff attempts revalidated.

In **one PostgreSQL transaction**, lock Shipment → Container → linked Handoff/Incident/Evidence; verify attestations, statuses, version and no existing verified evidence; mark any PENDING Delivery handoff SUPERSEDED, create finalized EXCEPTIONAL evidence, set container RECEIVED and server received_at, keep `container.received_by=NULL` for EXCEPTIONAL (the regional decision-maker is **not** a physical Lab receiver), set `receipt_evidence.verified_by` to the regional decision-maker, preserve the actual lab witnesses in attestations, open first receiving session if needed, update Shipment aggregate state and append immutable audit. An EXCEPTIONAL evidence record references the deciding regional actor and the attestations/incident: it **does not** claim that actor was the physical receiving Lab Operator.

If rejected or contested, leave container unreceived; retain all attestations, incident history and error/review reason. Concurrent normal Lab confirmation vs exceptional finalizer: at most one verified receipt wins; other gets deterministic 409.

## 5. Other approved recovery behavior

- **Q01:** after confirmed Pickup, do not replace Courier. Each container not presented to lab may be marked MISSING (CONTAINER_NOT_PRESENT) and individually goes through regional proposal + destination LAB_MANAGER approval for retry or terminal closure. RETRY cannot invent a new Courier.
- **Q02:** Sender may withdraw own PENDING Pickup only when verified containers remain at origin and physical transfer did not occur; otherwise origin incident/review, no fictional cancellation. No reassignment after seal.
- **Q04:** after RECEIVED keep custody/handoff. Each **affected Sample** individually REJECTED with matching Sample reason code, audit and revision, and LAB_MANAGER reopening if session was completed.
- **Q05:** REGIONAL_OPERATIONS can withdraw **their own** PENDING resolution proposal with mandatory reason. Rejected/withdrawn proposal has no custody effect. Approval/withdrawal race serialized under Shipment lock.
- **Q06:** long-stalled Handoff uses incident evidence matrix above, no automatic confirmation or proxy signature.

## 6. Invariant for tube-level receiving

`can_receive_item := parent_container.delivery_status=RECEIVED AND (verified NORMAL evidence OR verified EXCEPTIONAL evidence) AND Shipment has an OPEN receiving_session AND actor is authorized destination Lab user`.

For EXCEPTIONAL method, **confirmed Pickup is an additional invariant**. No receipt evidence of either kind bulk-accepts all Sample items.

## 7. Negative acceptance tests

- A single staff member holding both LAB_OPERATOR and LAB_MANAGER roles cannot witness twice.
- Two Lab Operators without any LAB_MANAGER fail. Witnesses from origin/other region fail. Resolver counted as witness fails.
- Pending/disputed Pickup blocks exceptional receipt. An incident can be administratively resolved without turning Pickup CONFIRMED.
- A direct SQL attempt to insert verified exceptional receipt without linked incident/two valid witnesses is blocked by protected service/trigger rules.
- Concurrent normal Delivery vs exceptional confirmation cannot double-create evidence or an OPEN receiving session.
- Courier account never receives fabricated confirmation, even if physical receipt succeeds exceptionally.
- Wrong-region regional resolver and revoked witnesses cannot finalize; ensure current membership checked at decision time.
- Existing two-stage ContainerIssue resolution still cannot be bypassed by incident resolution.

This is approved **for coding and test implementation**, not operational deployment with patient data.
