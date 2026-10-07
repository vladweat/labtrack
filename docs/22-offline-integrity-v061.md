# LabTrack v0.6.1 — Offline Integrity, Snapshot Age and Idempotency

**Status: APPROVED FOR IMPLEMENTATION (2026-11-01).** Approved Q07–Q11 and final OPEN-04 definition in [Decision Register](20-decisions-v061.md). Preserve v0.6 per-container delivery. The server is authoritative, not device time.

## 1. Client commands unchanged in meaning

Only Courier can create offline proposals:
- COURIER_CONFIRM_PICKUP: **whole Shipment**, full sealed container set, confirms second party of pending Sender Pickup.
- COURIER_INITIATE_CONTAINER_DELIVERY: **one container**, creates pending Delivery to be separately confirmed by Lab online.

Neither command is a valid completed handoff until a PostgreSQL commit. Local `PENDING_SYNC` is not custody transfer. Two Courier devices may keep separate queues; do not merge them client-side or infer global ordering.

## 2. Server-issued manifest snapshots (Q07)

On authorized GET /courier/shipments/{shipment_id}/offline-manifest, persist or cryptographically authenticate a server-issued snapshot:
- `snapshot_id UUID`
- `issued_to_user_id` (and relevant Courier assignment and region scope)
- `shipment_id`, `manifest_revision`, signed/validated set of container codes
- `issued_at TIMESTAMPTZ` (DB/server timestamp)
- optional `shipment_state_version_at_issue` and per-container `state_version_at_issue`
- expiry/review policy version.

Client commands include immutable `snapshot_id`, `client_command_id`, target, expected manifest and target state versions, and untrusted `client_performed_at`. Client local `queued_at` helps display age but is not trusted as proof.

**Conservative engineering policy, not forensic fact:** if snapshot issued more than 24 hours before sync, mark command REVIEW_REQUIRED and do **not** mutate custody. A newer snapshot does not silently refresh an old queued command; re-create a fresh online action only after review. Snapshot issued 25h ago does not prove that user clicked 25h ago. Time from device can never supply the authoritative custody event timestamp.

A server cannot alert on an offline command it has never received. Local client warning after ~24h is UI guidance only; server Dashboard separately detects overdue persisted handoffs.

## 3. FINAL: relevant state conflict (OPEN-04 approved)

**Approved invariant:** a command is rejected with `OFFLINE_COMMAND_STALE` if **any of its relevant dependencies** changed after the authoritative snapshot, even if the status later returns to the original enum (ABA). No automatic rebasing. A change to unrelated data is not inherently a conflict.

**Pickup command dependencies** (whole Shipment):
- Immutable `manifest_revision` and exact sealed container set;
- Pickup Handoff/Shipment **Pickup custody-phase version** (including new attempts and withdrawals);
- assigned Courier identity and active current actor authorization;
- Shipment phase still authorizes the second Sender→Courier confirmation, with the actual Sender release pending.

**Single-container Delivery initiation dependencies**:
- immutable `manifest_revision`;
- target Container `state_version` and its Delivery Handoff attempt/version;
- whole-Shipment Pickup already CONFIRMED; Pickup custody-phase validity/current Courier assignment;
- current authorized actor/scope and target Container status IN_TRANSIT.

**Not a conflict on its own:** a Sample classification or receipt revision in a different already-RECEIVED Container, an unrelated notification, analytics recomputation, or another unrelated Shipment bookkeeping version increment **provided all relevant tokens are unchanged**. Do not compare global `Shipment.version` alone and wrongly reject an independent container delivery.

All relevant version checks occur under parent Shipment `FOR UPDATE`; the server computes a normalized dependency set and rejects on mismatch. A target transition `IN_TRANSIT → DELIVERY_PENDING → IN_TRANSIT` increments `state_version` and is stale despite returning to the same state label. Unauthorized actor remains rejected even if all version tokens match.

## 4. 90-day replay + tombstone (Q08)

All Courier offline actions share one immutable key namespace `(actor_id, operation='courier_offline_command', client_command_id)`. Hash canonicalized full original command, including snapshot_id, Shipment/Container/action, codes, expected versions, claimed time; prevent same UUID for a different payload.

- For at least **90 days** after first accepted command, keep original response and authoritative `recorded_at` for exact replay.
- After detailed response retention ends, keep a compact **immutable applied-command tombstone** with actor/command ID/request hash, underlying event/transaction ID, authoritative recorded_at and minimal replay result; no duplicate application at any point. The actual tombstone retention policy must account for replay protection and production privacy requirements.
- Same ID + same body → ALREADY_APPLIED and original (or post-90d minimal) authoritative outcome; same ID + different payload → IDEMPOTENCY_KEY_REUSED; neither mutates custody.
- If crash after transaction commit before HTTP response, replay consults persisted response/tombstone. Store transaction result/audit/command ID atomically.
- Ordinary REST `Idempotency-Key` can use separately configured retention; **never reuse short HTTP TTL as Courier custody replay horizon**.
- If command had a definite REJECTED outcome rather than APPLIED, retain command identity/rejection history appropriately so replay of same ID never bypasses policy by being reinterpreted later.

## 5. Stale/manual-review handling (Q11)

For REVIEW_REQUIRED or REJECTED due to state change:
1. Do not confirm original stale payload and do not rewrite `client_performed_at` as server physical custody.
2. Present reason and event history; human review investigates what physically occurred.
3. If server state permits **new** online confirmation, create fresh authenticated action with **new ID** and current scan/evidence; do not revive original offline command.
4. If physical and server custody differ, open CustodyIncident (no proxy signature).
5. Queue clear only after durable acknowledgement/manual disposition; log device/server timestamp distinctly.

## 6. Concurrency, two devices and actor security

- Per-command transaction: idempotency reservation → Shipment FOR UPDATE → relevant container and Handoff locks → validate current Courier assignment/role/snapshot/state → business mutation + audit + replay → commit.
- Commands of same Shipment serialize; two devices do not automatically resolve order. Different UUIDs for same custody action: one may commit, the other sees stale/INVALID_STATE.
- A Courier account disabled after download is denied at sync regardless of snapshot freshness; cache replay to an unauthorized actor must not disclose current operational data.
- Max 20 commands/batch; per-command result APPLIED, ALREADY_APPLIED, REJECTED, REVIEW_REQUIRED or RETRYABLE. Never falsely report batch-wide business success.
- Access JWT is never queued or stored in IndexedDB; reauthenticate after expiration; purge local data on logout/account change.

## 7. Error matrix / tests

| Case | Required behavior |
|---|---|
| Snapshot older than 24h | REVIEW_REQUIRED, no custody mutation |
| Device clock altered | Server age/check unaffected; keep claimed timestamp for audit as untrusted |
| Two devices submit distinct but conflicting commands | Exactly one applicable transition, other rejected |
| Same command replay after 90-day response TTL | Tombstone prevents reapplication |
| Same ID with changed payload | IDEMPOTENCY_KEY_REUSED |
| Shipment.version bumps due only to unrelated container item update | **Not stale** when manifest, target Container/Handoff, Pickup phase and authorization dependencies all still match |
| Target container changes then returns to same enum | Target state_version conflict detects ABA |
| Login revoked after snapshot | FORBIDDEN/401, no mutation |
| Network response lost after commit | Replay safe, no duplicate custody or audit |
| Offline action physically happened but cannot be synced | Incident/review, not forged event |

**No legal offline signature or certainty about physical action time is claimed.**