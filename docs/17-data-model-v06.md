# LabTrack v0.6 — Relational Model, Invariants and Transactions

> **Исторический базовый документ v0.6.** v0.6.1 supersedes contradictory requirements; current decisions and API are in [Decision Register v0.6.1](20-decisions-v061.md), [Custody Exceptions](21-custody-exceptions-v061.md), [Offline Integrity](22-offline-integrity-v061.md), [DB Delta](23-data-model-v061.md), [API](24-api-and-operations-v061.md). Keep v0.6 rules only where unchanged.

**Status:** design specification, not an applied Alembic migration.  
**Baseline:** [Data Model v0.5](12-data-model-v05.md); **v0.6 authoritative** [Workflow](16-domain-workflows-v06.md). Database: PostgreSQL, `READ COMMITTED`, SQLAlchemy async.

## 1. Minimal changes to the 12-table baseline

Retain `regions`, `organizations`, `users`, `organization_memberships`, `regional_memberships`, `samples`, `shipments`, `transport_containers`, `container_items`, `handoffs`, `audit_events`, `idempotency_records`. Retain v0.5 add-ons `issue_reason_codes`, `container_issues`, `receiving_sessions`, `receipt_revisions`, `sample_external_references`.

**v0.6 adds:** `container_issue_resolution_proposals` (two-party human approval). Additional offline client commands use `idempotency_records` for durable replay; client IndexedDB is not the server source of truth.

### Updated tables and fields

| Table | Add / change | Rule |
|---|---|---|
| `shipments` | `status` enum gains `PARTIALLY_DELIVERED`; retain DELIVERY_BLOCKED/CLOSED_UNDELIVERED, `manifest_revision`, `version`, `completed_at` | Coarse status derived from container dispositions; check by transaction service |
| `transport_containers` | `delivery_status` NOT NULL enum `PREPARED,IN_TRANSIT,DELIVERY_PENDING,RECEIVED,BLOCKED,UNDELIVERED_CLOSED`; `received_at NULL, received_by NULL` | Delivery state belongs **to container**, not Shipment. PREPARED set at creation; IN_TRANSIT after confirmed Pickup |
| `handoffs` | `container_id UUID NULL` + existing Shipment ID, kind, attempt_no, status, initiator, confirmer, timestamps, withdrawal | PICKUP: container_id NULL; DELIVERY: container_id NOT NULL; Shipment ID must equal container's Shipment. One Pickup per Shipment, independent Delivery attempts per container |
| `container_issues` | existing `container_id,kind,reason_code_id,status,reported_by,resolved_by,reported_at,resolved_at,resolution` | Exactly one OPEN issue per container, matching code kind/scope; once approved, status RESOLVED or CLOSED_UNDELIVERED; no implicit Sample item result |
| `receiving_sessions` | retain `shipment_id,sequence,status,opened_by,closed_by,reopen_reason,timestamps` | Open at first **container** RECEIVED, and one OPEN per Shipment |
| `container_items` | `receipt_result,receipt_reason_code_id,receipt_comment,receipt_recorded_by,receipt_recorded_at` | Receipt fields writable iff container RECEIVED and its two-party DELIVERY Handoff confirmed, and Shipment has OPEN receiving session. No receipt on undelivered container |
| `receipt_revisions` | old/new outcome, reason and comment, actor, timestamp, session | Append-only on **every** item change, including first classification |
| `sample_external_references` | retain 1:N refs | Synthetic opaque only; no patient name or actual LIS call |
| `audit_events` | retain append-only event_type/entity/actor/request_id/details | Include target container, independent two-party confirmation actors and timestamp |
| `idempotency_records` | existing `actor_id,operation,key,request_hash,response` | Offline uses fixed `operation='courier_offline_command'`, `key=client_command_id`, all action types share namespace per actor |

### New table: container_issue_resolution_proposals

| Column | Type | Purpose |
|---|---|---|
| `id` | UUID PK | Immutable proposal identity |
| `issue_id` | UUID NOT NULL FK | One container issue |
| `action` | TEXT NOT NULL | `RETRY_DELIVERY` or `CLOSE_UNDELIVERED` |
| `reason` | TEXT NOT NULL (1–500) | Regional operator's explanation |
| `status` | TEXT NOT NULL | `PENDING,APPROVED,REJECTED,WITHDRAWN` |
| `proposed_by` | UUID NOT NULL FK | REGIONAL_OPERATIONS |
| `proposed_at` | TIMESTAMPTZ NOT NULL | Server time |
| `decided_by` | UUID NULL FK | Destination LAB_MANAGER, different user |
| `decided_at` | TIMESTAMPTZ NULL | Server time |
| `decision_reason` | TEXT NULL | Mandatory for REJECTED; optional for APPROVED |

Check `proposed_by <> decided_by` when decided, pending/decided timestamps/actor consistency. Create unique partial index:

```sql
CREATE UNIQUE INDEX uq_issue_pending_proposal
ON container_issue_resolution_proposals(issue_id)
WHERE status='PENDING';
```

A proposal does not update the issue or container. Approval in one transaction verifies active issue, BLOCKED state, same Shipment/region/destination and live membership, and atomically updates proposal, issue, container, Shipment aggregate status and audit. Rejection updates only proposal/audit and leaves issue OPEN/BLOCKED. Proposed decisions that have already become stale receive 409.

## 2. Conditional handoff uniqueness and integrity

Keep `Handoff.kind` either PICKUP or DELIVERY. The former `UNIQUE(shipment_id,kind,attempt_no)` must **not** apply to DELIVERY after introducing per-container handoffs; replace with conditional unique indexes:

```sql
CREATE UNIQUE INDEX uq_pickup_attempt
ON handoffs(shipment_id,attempt_no)
WHERE kind='PICKUP';

CREATE UNIQUE INDEX uq_delivery_attempt
ON handoffs(container_id,attempt_no)
WHERE kind='DELIVERY';

CREATE UNIQUE INDEX uq_pickup_active_or_confirmed
ON handoffs(shipment_id)
WHERE kind='PICKUP' AND status IN ('PENDING','CONFIRMED');

CREATE UNIQUE INDEX uq_delivery_active_or_confirmed
ON handoffs(container_id)
WHERE kind='DELIVERY' AND status IN ('PENDING','CONFIRMED');
```

DB `CHECK`: `(kind='PICKUP' AND container_id IS NULL) OR (kind='DELIVERY' AND container_id IS NOT NULL)`. Enforce `handoffs(shipment_id,container_id)` matching `transport_containers(shipment_id,id)` using appropriate composite FK where container_id NOT NULL, plus mandatory Shipment FK. Existing confirmed handoffs cannot be withdrawn, overwritten or deleted.

`transport_containers.received_by` must match the confirmed DELIVERY Handoff actor (transaction service / guarded DB invariant), which differs from initiator. Do not auto-accept all `container_items`.

## 3. Reason codes and receipt validation

Seed `issue_reason_codes(scope,kind,code,label,is_active)`, `scope=SAMPLE|CONTAINER`, `kind=MISSING|REJECTED`. User chose **fixed seed catalog**, not CRUD management.

- `container_issues.reason_code_id` matches `scope=CONTAINER,kind=container_issues.kind`.
- `container_items.receipt_reason_code_id` matches `scope=SAMPLE,kind=receipt_result` for MISSING/REJECTED; NULL for ACCEPTED. `receipt_comment` optional and max 500 chars.
- Simple UUID FK alone is not sufficient to validate scope and kind. Use a protected trigger or composite FK incorporating scope/kind, with application validation.
- `receipt_result=NULL` on UNDELIVERED_CLOSED is **required**, not a data-quality defect; do not synthesize outcomes.

## 4. Explicit terminal invariants

For Shipment S, let:

`N = total containers` (N ≥ 1 after seal), `R = RECEIVED`, `U = UNDELIVERED_CLOSED`.

- `complete_receiving` allowed iff `R >= 1`, `R + U = N`, a receiving session is OPEN, every item belonging to a RECEIVED container has non-null result, **every item in UNDELIVERED_CLOSED containers has NULL result**.
- If `U>0` or any RECEIVED item is MISSING/REJECTED → `COMPLETED_WITH_ISSUES`; otherwise `COMPLETED`.
- `R=0, U=N` → `CLOSED_UNDELIVERED`; no completed receiving session can exist for this Shipment.
- An unresolved container means `R+U < N`; shipment completion is rejected, even if every currently RECEIVED item has a result.
- On manager reopen, keep R/U fixed, open a new session, reset live Shipment status to RECEIVING, and preserve previous session/audit/revision history.

Compute the coarse Shipment status under the parent row lock using the precedence function in [Workflow](16-domain-workflows-v06.md). Do **not** attempt a simple `CHECK` that cross-counts child records.

## 5. Lock order, isolation and safe idempotency

All mutating commands (including approvals, per-container delivery, receipt and offline sync):

1. Parse/authenticate and validate request shape. Start transaction.
2. Reserve/check idempotency key under global actor + operation + key scope; use stable `courier_offline_command` namespace for sync requests.
3. Lock **Shipment first**: `SELECT ... FOR UPDATE`. Authorization and active membership are rechecked before returning a cached response.
4. Lock required container(s) / issue / proposal / handoff / receiving session and item(s) in a **deterministic order**, then validate state and role. Exactly one Shipment is modified in current MVP.
5. Write all dependent changes, append events/revisions, recalculate coarse status and bump Shipment.version, save idempotency result in the **same transaction**.
6. Commit; then best-effort publish Celery/Redis notification. A lost notification does not undo the business event.

Concurrent delivery for **different containers of the same Shipment** serializes at the parent lock; intentional simple/robust choice for the regional pilot.

If offline command is >24h old or stale, **do not apply it automatically**. Return a stable review/conflict result; store appropriate server-side audit when the command reaches the server, not while unseen on the device. Server cannot monitor offline device backlog it has not received.

## 6. PostgreSQL trigger guarantees and realistic deadlock note

User approved DB triggers to protect the sealed manifest:
- Reject INSERT/DELETE/UPDATE of Shipment-to-container membership and container-to-sample identity after seal.
- Only permit changing receipt attributes on items whose container is RECEIVED and the Shipment has an OPEN receiving session, through authorized application path.
- Keep received container status/confirmed handoff immutable and receipt revision events append-only.

**Important:** row-level triggers invoked on direct SQL may acquire a child row lock **before** locking the parent Shipment. Thus a trigger reading/locking Shipment is necessary for seal consistency but by itself **does not prove global deadlock freedom** against a concurrent application transaction that locks Shipment first. Restrict direct writes using DB roles, ensure application uses the parent-first lock protocol, and test deadlock detection/retry for unexpected direct SQL. Do not promise impossible guarantees from trigger-only ordering.

## 7. Migration and indexes

The repository currently contains **no implemented backend/migration files**. These are target DDL rules, not executed migrations.

Recommended migration order:
1. Extend Shipment status check with PARTIALLY_DELIVERED, update the business-state policy, add container.delivery_status and received_* columns.
2. Make DELIVERY handoffs per-container; replace old uniqueness; add partial indexes and composite FK.
3. Introduce container_issue_resolution_proposals; migrate direct resolution path to proposal/decision. Retain issue history.
4. Update receiving guards so received containers can be processed while Shipment PARTIALLY_DELIVERED.
5. Add reason code seeds, receiving session triggers, sealed manifest guards, backfill synthetic fixtures.
6. Index: `transport_containers(shipment_id,delivery_status)`, `container_issues(container_id,status)`, `resolution_proposals(issue_id,status)`, `container_items(container_id,receipt_result)`, `handoffs(container_id,kind,status)`, `receipt_revisions(container_item_id,changed_at)`.
7. Add integration tests running **real PostgreSQL**; rerun Alembic upgrade on disposable DB.

## 8. SQL/integration acceptance matrix

- Two separate containers can be received in parallel requests without any double handoff; operations serialize safely at parent lock.
- Block C3 without affecting C1/C2. C1/C2 items can be marked; C3 items cannot.
- Create PENDING proposal → delivery state unchanged; wrong-region operator cannot propose.
- LAB_MANAGER approval requires different `user_id` and destination Lab membership, and cannot approve stale/rejected proposal.
- Two approvals of one proposal race → one transition, one audit; same idempotency replay returns stored result.
- Sealed manifest direct SQL mutations fail. Reopened receiving never permits changing container/sample membership.
- Cannot finalize Shipment while any container unresolved. Mixed undelivered+received closes as COMPLETED_WITH_ISSUES; all undelivered closes as CLOSED_UNDELIVERED.
- Return distinct claimed client time and authoritative server time and handle duplicate/stale sync.
