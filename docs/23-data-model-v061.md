# LabTrack v0.6.1 — PostgreSQL Delta & Integrity Rules

**Status:** Alembic revisions `0001_foundation_identity` through `0005_stage_e` implement Stages A–E, including ContainerIssue proposals, custody incidents and attestations, and NORMAL/EXCEPTIONAL receipt evidence. Offline, Inbox, analytics, and other unimplemented v0.6.1 schema changes below remain design. Build on [v0.6 relational model](17-data-model-v06.md); preserve Shipment-first locking, partially delivered receiving, and sealed manifest triggers. User-approved changes: [20-decisions-v061.md](20-decisions-v061.md).

## 1. New tables (proposed physical mapping)

| Table | Core columns | Invariant |
|---|---|---|
| `custody_incidents` | id, shipment_id, container_id NULL, handoff_id NULL, phase, kind, status, reason, opened_by/at, resolved_by/at NULL, resolution NULL | Scoped incident and human review; **not** a third MISSING/REJECTED type |
| `custody_attestations` | id, incident_id, user_id, org_id_at_time, role_at_time, container_code NULL, statement, physically_present NULL, physical_release_occurred NULL, containers_at_origin NULL, recorded_at | Unique (incident_id,user_id); append-only; one account counts once. Pickup attestation has origin facts; Delivery attestation asserts personal physical presence |
| `container_receipt_evidence` | id, container_id, method NORMAL|EXCEPTIONAL, handoff_id NULL, incident_id NULL, verified_at/by | Exactly one verified receipt per container; method-specific FK shape (XOR) |
| `courier_manifest_snapshots` | id, shipment_id, issued_to_user_id, manifest_revision, shipment_phase_version, issued_at, container_versions payload or child rows, digest | Immutable issue time, scoped to Courier; no private patient data |
| `offline_command_tombstones` | actor_id, client_command_id, request_hash, event_id, applied_at, compact_response/status | Unique actor+client_command_id under fixed offline namespace; survives full-response cleanup |
| `notifications` | id, recipient_user_id, event_id, type, payload JSONB, created_at, read_at NULL | Personal inbox, idempotent natural key (recipient_user_id,event_id,type) |

For the full 90-day detailed replay use existing `idempotency_records` with durable `request_hash` and response. Preserve compact tombstone after detailed record expires. Avoid reusing an expired key as new custody command.

**Receipt evidence (OPEN-01–03 APPROVED):** use partial/composite FK and protected service invariants: NORMAL requires a confirmed DELIVERY Handoff of that container; EXCEPTIONAL requires a previously CONFIRMED whole-Shipment Pickup, **resolved incident**, two physically present, authenticated **distinct destination-laboratory employees** as witnesses with **at least one active LAB_MANAGER**, and a **REGIONAL_OPERATIONS administrative finalizer who is not a witness**. One verified receipt evidence per container, immutable. Verify current actor memberships and physical-presence attestations under Shipment lock with audit; DB FKs alone cannot attest human physical presence.

## 2. Existing schema deltas

### `handoffs`
- Status CHECK adds `SUPERSEDED` only for pending Delivery replaced by exceptional receipt; never rewrite confirmed handoff. Keep immutable old actors/timestamps.
- Add needed `superseded_by_incident_id`, `superseded_at` and reason, mutually consistent with status.
- Preserve per-container uniqueness of active pending/confirmed Delivery Handoff; SUPERSEDED is terminal and allows new attempt only when policy authorizes one.

### `container_issue_resolution_proposals`
- Existing PENDING/APPROVED/REJECTED/WITHDRAWN status enumeration retained.
- Add `withdrawn_by UUID NULL`, `withdrawn_at TIMESTAMPTZ NULL`, `withdrawal_reason TEXT NULL`.
- For WITHDRAWN require all withdrawal metadata and nonblank reason; actor must equal proposed_by while current role/scope still valid; validate role in service layer and audit.
- Keep PostgreSQL partial UNIQUE index: only one PENDING proposal per issue. Decision vs withdrawal race under parent Shipment lock; loser 409, no duplicate audit transition.

### Final authorization constraints for CustodyIncident (OPEN-03)

- Regional operator of Shipment region **opens** the incident and makes a separate administrative final decision, without impersonating the absent participant. `resolved_by` is a regional user, not a Courier handoff signer.
- Origin-side Pickup review needs an active origin `SENDER_OPERATOR` attestation. Incident close does **not** set `handoffs.PICKUP=CONFIRMED`; withdrawal/cancellation remains an independent guarded Sender command. Disputed custody remains unchanged and is logged as `ESCALATED_NO_CUSTODY_CHANGE`.
- Ordinary lab-side stalled Delivery needs destination `LAB_MANAGER` attestation to facts, not a surrogate Courier confirmation.
- Exceptional Delivery requires 2 unique witness user IDs at the destination Lab (at least one `LAB_MANAGER`) + confirmed Pickup + regional administrative decision. `UNIQUE(incident_id,user_id)` guards duplication; service enforces role, location claim, current membership and reviewer-witness separation.
- Resolve incident, create verified exceptional receipt evidence, supersede only pending Delivery Handoff, set container RECEIVED and open first receiving session **in one transaction**. For EXCEPTIONAL, `transport_containers.received_by=NULL` (avoid falsely representing REGIONAL_OPERATIONS as a physical recipient); `received_at` is server verification time, `container_receipt_evidence.verified_by` is the regional reviewer, and `custody_attestations` identify physical witnesses. NORMAL retains `received_by=actual_lab_confirmer`. Incomplete/invalid evidence must not commit the state change.

### Offline version relevance (OPEN-04)

Do not reject a Courier command merely because `Shipment.version` changed due to a different container's Sample outcome. Compare `manifest_revision`, **target** Container/Handoff custody version, confirmed Pickup phase/version, Courier assignment and active authorization. For whole-Shipment Pickup, compare Pickup phase/handoff version and complete manifest. Any relevant change (including an ABA target-state cycle) is stale. Version increments and checks occur under Shipment parent lock, and no relevant stale command is automatically rebased.

### `transport_containers`, `shipments`
- Add monotonically increasing `transport_containers.state_version BIGINT NOT NULL DEFAULT 1`. Every custody state/handoff attempt mutation for that container increments version, even state ABA.
- Add authoritative Pickup custody/phase version on Shipment or Pickup Handoff, to detect relevant Pickup changes without confusing unrelated receipt edits.
- Existing `manifest_revision` stays frozen after seal; no modifications to identity/membership through direct SQL.

### `shipments` creation/seal
- `expected_delivery_at` nullable in DRAFT; seal requires non-null value strictly greater than DB `CURRENT_TIMESTAMP` as evaluated in transaction (avoid stale app clock). This is cross-field/state validation in service and optionally DB protected trigger.
- Destination must match single seeded/configured pilot lab on both CreateShipment and seal (do **not** hardcode UUID into SQL migration). Region and organization memberships checked under transaction.

### `issue_reason_codes`
Fixed 6 seeded (scope,kind,code) rows, RU labels as in [Decision Register](20-decisions-v061.md). Seed unique codes, no user editing endpoints; foreign keys from issue/sample result still validated for matching scope+kind (composite FK or guarded trigger).

## 3. Notification semantics

- Each notification is persisted in PostgreSQL; `read_at` NULL means unread, UTC server clock.
- Use unique key `(recipient_user_id,event_id,type)` to deduplicate retries.
- Notification creation via Celery best effort means Inbox **may miss** a notification if worker/publish fails. **Source of truth for stalled cases remains query from Shipment/Handoff/Proposal timestamps in PostgreSQL**, not Inbox.
- Only recipient can mark their notification read. A regional escalation cannot reveal tube-level data to REGION_ANALYST.
- If durable Inbox publication is later required as invariant, use transactional outbox; **not** implied by best-effort product choice.

## 4. Transaction boundaries & lock order

For all writes including attestation, exceptional finalization (once approved), proposal withdrawal, and offline sync:
1. Authenticate/validate shape, begin TX.
2. Reserve idempotency key or per-command replay identity first.
3. Acquire `SELECT ... FOR UPDATE` Shipment as aggregate root.
4. Lock target containers, handoffs, incidents/proposals, receipt evidence rows in deterministic order.
5. Check roles, physical/custody status, manifest/snapshot versions, conflicting evidence, valid reason and recipient scope; update entities, append audit/revisions, save replay response in same commit.
6. Commit; schedule optional Celery best-effort after commit.

A trigger reacting to direct SQL may acquire child row locks before the parent; this can deadlock against parent-first application TX. Restrict DB roles and integration-test direct SQL seal races, retry SQLSTATE 40P01 when appropriate. Never claim triggers alone prove deadlock freedom.

## 5. Key SQL patterns (illustrative, **not** complete migrations)

```sql
CREATE UNIQUE INDEX uq_custody_attestation_actor
  ON custody_attestations (incident_id,user_id);

CREATE UNIQUE INDEX uq_container_receipt_evidence
  ON container_receipt_evidence (container_id);

CREATE UNIQUE INDEX uq_notification_delivery
  ON notifications (recipient_user_id,event_id,type);

CREATE INDEX ix_notification_recipient_unread
  ON notifications (recipient_user_id,created_at DESC)
  WHERE read_at IS NULL;

CREATE UNIQUE INDEX uq_offline_command_tombstone
  ON offline_command_tombstones (actor_id,client_command_id);
```

Checks: receipt evidence XOR normal_handoff_id/incident_id; immutable evidence; proposal withdrawal metadata; reason codes match scope/kind; one open receiving session per Shipment; sealed manifest immutable; no Sample result without verified Container receipt evidence.

## 6. Migration/verification strategy

1. Introduce new tables first; nullable references and safe new enums before changing write paths.
2. Add state_version and Pickup phase version, guarded by service mutations.
3. Add proposal withdrawal fields and status processing.
4. Seed six codes; backfill demo-only fixtures where needed.
5. Add idempotency tombstone and 90d response retention logic; verify replay across archival.
6. Add durable Inbox/read_at and indexes; connect Celery best-effort tasks.
7. Add incident/evidence models with **approved witness/pickup/role guards** and unit + PostgreSQL tests; feature flag may be used for controlled rollout but no product sign-off remains pending.
8. Apply real Alembic migrations against PostgreSQL, validate upgrade and rollback/forward on disposable DB; test row-level security boundaries at application layer.

Stage E's schema and guards are implemented in `0005_stage_e` and passed the PostgreSQL migration cycle and backend acceptance suite. Remaining migration items in this design—including offline command tombstones and Inbox tables—are not claimed as implemented.
