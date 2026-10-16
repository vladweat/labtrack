# LabTrack v0.6.1 — Verification, Test Matrix & Implementation Gates

**Scope:** requirement-level verification plan and implementation gates. Foundation and Manifest/Stage B are implemented with real PostgreSQL migrations and integration tests; later custody, receiving, offline, and analytics workflows remain unimplemented. Start from [v0.6 plan](19-roadmap-v06.md) and hardening decisions [20](20-decisions-v061.md). Use **real PostgreSQL** for locks, triggers, partial indexes and migration testing.

## 1. P0 — Chain of custody

| Test | Precondition and expected result |
|---|---|
| unavailable_courier_after_pickup | Whole-Shipment Pickup CONFIRMED. Courier unavailable; cannot change courier ID; each undelivered container MISSING, regional proposal and distinct lab approval; no forged delivery |
| pending_pickup_withdraw_no_physical_transfer | Sender confirms containers still at origin, withdraws Pickup then can cancel under READY_FOR_PICKUP rules; audit includes reason |
| pending_pickup_physical_custody_disputed | Suspected physical release; normal cancellation must not falsely declare no transfer; incident required |
| exceptional_receipt_two_independent_witnesses | One witness or one account with two roles insufficient; two valid distinct attestations necessary; must not set Courier confirmed_by |
| exceptional_receipt_role_and_pickup_guard | Two distinct destination Lab staff required, at least one active LAB_MANAGER; regional resolver is not a witness; Pickup-unconfirmed cannot create RECEIVED; 403/409, no incident acting as Courier Pickup |
| normal_vs_exceptional_concurrent_receipt | Two concurrent finalize paths produce at most one verified receipt evidence and one OPEN receiving session |
| post_received_container_problem | Do not revert CONFIRMED handoff, mark each affected Sample REJECTED with scoped reason and revision/audit, reopen receiving if necessary |
| proposal_owner_withdraw | Proposer can withdraw own PENDING with reason, another user cannot; next proposal valid afterward |
| proposal_decide_withdraw_race | Approval and withdrawal concurrent under Shipment lock: exactly one wins, other 409; one custody effect |
| stalled_handoff_review | Regional operator creates incident; appropriate Sender/Lab confirms own facts; no impersonation or auto-confirm |

### Final OPEN-01–03 authorization and outcomes

| Test | Expected |
|---|---|
| exceptional_two_lab_operators | Two destination Lab Operators with no LAB_MANAGER are **insufficient** |
| exceptional_manager_plus_operator | Two distinct destination Lab users, one active LAB_MANAGER, satisfy witness-role requirement when both attest personal physical presence |
| exceptional_resolver_not_witness | Regional reviewer does not count as either destination Lab witness; no proxy Courier signature |
| exceptional_pickup_not_confirmed | Even two eligible witnesses cannot move container to RECEIVED without a normally confirmed whole-Shipment Pickup |
| pickup_incident_admin_only | Origin Sender attestation + regional NO_PHYSICAL_TRANSFER review does **not** mark Pickup CONFIRMED or auto-cancel; Sender performs separate guarded withdraw |
| stalled_delivery_admin_only | Lab Manager attestation + regional ESCALATED_NO_CUSTODY_CHANGE review leaves handoff unconfirmed |
| exceptional_admin_finalize_atomic | Region resolves EXCEPTIONAL_RECEIPT_VERIFIED + receipt evidence + container RECEIVED + receiving session in one TX; concurrent normal route produces only one winning evidence |
| cross_scope_or_revoked_attestation | Wrong Lab, wrong region, inactive membership or same witness twice rejected; full audit retained |

## 2. P0 — Offline correctness

| Test | Invariant |
|---|---|
| server_issued_snapshot_age | Issued_at from DB; snapshot older than 24h yields REVIEW_REQUIRED, no change; client clock tampering irrelevant |
| duplicate_across_90_day_window | Same ID/payload returns original replay, one audit/transition |
| replay_after_response_archive | Once detailed response removed, tombstone still prevents reapplying custody |
| changed_body_same_id | conflict, no new custody event |
| two_devices_same_container | Independent queues with distinct IDs: one valid state transition, other stale/INVALID_STATE |
| target_state_aba | IN_TRANSIT→DELIVERY_PENDING→IN_TRANSIT must increment state_version; stale command rejected even though enum matches |
| unrelated_shipment_change | Another container's tube result or notification increments Shipment.version, but target Container/Handoff, manifest, Pickup phase and active Courier authorization remain unchanged: valid command may apply; **no relevant state rebase** |
| revoked_courier | Snapshot issued before role revocation still denied at sync |
| stale_after_manual_review | Original command not retrospectively applied; new authenticated live command/incident |
| timeout_after_commit | Retrying exact command ID returns recorded outcome without duplicate event |

**Q12 API dependency:** `PATCH /shipments/{shipment_id}` updates expected_delivery_at in DRAFT; a draft originally created with null must be able to acquire a future deadline and seal. After seal deadline editing is forbidden in this MVP contract.

## 3. P1 — Data validation, API and operations

| Test | Requirement |
|---|---|
| draft_without_deadline | CreateShipment DRAFT accepts null expected_delivery_at |
| update_draft_deadline_and_seal | Draft update endpoint sets future deadline, then seal succeeds; past/null at seal rejected |
| pilot_destination_enforced | Wrong Lab destination rejected both on CreateShipment and seal even with forged API request |
| six_reason_codes | All six seeded with exact scope/kind/RU labels; inactive/mismatched code rejected; no CRUD |
| inbox_read_unread_idor | User can read own, not another user's notification; read twice idempotent; unread-count changes once |
| duplicate_notification_task | Celery retry cannot duplicate (recipient,event,type) row; Redis outage leaves persisted business status available via dashboard |
| stall_thresholds | Pickup 4h, Container Delivery 4h, pending proposal 24h, shipment expected_delivery_at; alert only, no custody mutation |
| analytics_day_week_utc | Day boundaries UTC and Monday-based UTC week; from inclusive/to exclusive |
| analytics_full_vs_partial | All-container RECEIVED Shipment included in full mean; partial only in container-specific mean; undelivered excluded |
| analytics_reopen | Latest results and state counted once, event/revision history append only |
| sealed_manifest_db | Direct SQL insert/update/delete after seal blocked, and concurrent seal vs update serialized/retried safely |

## 4. Cross-cutting gates

- Auth: Argon2id, short-lived JWT, per-use-case active RBAC and tenant/region scope, IDOR masked 404 when appropriate, tests for region analyst aggregate-only.
- DB: each state mutation + audit + revisions + idempotency response shares one PostgreSQL transaction; no partial effects upon audit failure.
- Triggers: DB owner/runtime roles distinct; direct SQL bypass test; test SQLSTATE 40P01 retry without double commit.
- Stage B acceptance: Sample/external-reference scope; optional draft deadline; configured destination validation; courier region membership; one-to-many containers; duplicate Sample exclusion; same-key replay and changed-body conflict; six reason-code labels; seal deadline/state; direct SQL immutability and runtime non-owner privilege checks.
- API: OpenAPI JSON schema validation and contract tests comparing FastAPI runtime-generated specification to agreed design; explicit 409/422/403/404 cases.
- Offline: no access tokens in IndexedDB, queue survives refresh, per-account logout purge, per-command ack persisted before queue removal.
- Operations: container event history reconstructs who asserted custody and via which evidence method; never confuse EXCEPTIONAL with normal Courier confirmation.
- Production-like demo: synthetic fixtures, smoke test, backup/restore, structured logs and CI. No real medical compliance certification is claimed.

## 5. Definition of Ready vs Done

**Specification Ready for Foundation:** all 19 selected answers recorded, v0.6.1 API and schema draft coherent, core normal workflow unchanged.

**Exceptional Receipt Ready for Implementation:** OPEN-01 (two distinct destination Lab witnesses, at least one LAB_MANAGER), OPEN-02 (already CONFIRMED whole-Shipment Pickup), OPEN-03 (region opens/reviews; origin Sender or destination Lab attests as themselves; region records separate outcome) are **APPROVED**. Implementation must pass strict authorization, physical-evidence attestation and PostgreSQL concurrency tests; approval is not proof the code works.

**Offline Ready for Implementation:** OPEN-04 is **APPROVED**: manifest revision, target Container/Handoff version, Pickup phase and active authorization are relevant; unrelated Shipment bookkeeping alone is not. 24h snapshot guard, 90d replay, tombstones and two-device conflicts remain required tests.

**Stage B implemented:** migrations and core FastAPI routes run against PostgreSQL; tests cover the primary sample-to-sealed-manifest flow, idempotent replay, and database trigger protection. The wider v0.6.1 OpenAPI contract, offline client, operational dashboards, and later custody/receiving workflows are not complete.

**Scope control:** no new third Sample/Container discrepancy type, no phantom witness signatures, no retrospective custody acceptance from stale commands, no Courier reassignment after seal.
