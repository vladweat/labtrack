# LabTrack v0.5 — Courier Offline Sync Protocol

> **Исторический документ (до v0.6).** Приоритет имеют [Decisions v0.6](15-decisions-v06.md), [Domain v0.6](16-domain-workflows-v06.md), [Data Model v0.6](17-data-model-v06.md), [API v0.6](18-api-v06.md). Этот текст может содержать устаревшие all-or-nothing handoff и unilateral resolution; не использовать его как актуальный контракт.

**Status:** proposed technical contract for the product-approved courier offline queue.  
**Related:** [Domain rules](10-domain-hardening-v05.md), [Data Model v0.5](12-data-model-v05.md), [REST API v0.5](13-api-v05.md).

## 1. Scope and invariants

**Offline-first is enabled only for the Courier client**, not for Sender, Lab or Regional Operations in MVP.

Supported offline commands:
- `COURIER_CONFIRM_PICKUP` (second part of sender → courier handoff);
- `COURIER_CONFIRM_DELIVERY` (first part of courier → lab handoff).

The client may scan QR codes and prepare commands offline; it may **not** unilaterally finalize a handoff, mutate Server Shipment state, or auto-accept individual samples. The UI must clearly display `PENDING_SYNC` until server acknowledgement.

The source of truth is server PostgreSQL. A claim that an offline command was performed at time T is an **untrusted device report**; authoritative `recorded_at` is the server DB time at successful sync. Device clock changes and duplicated submission must not alter the server timeline.

## 2. Courier client storage and session

Recommended minimal PWA:
- Service worker for application shell only.
- IndexedDB stores scoped manifests for currently assigned shipments and durable queue entries, **no JWT or passwords** and no patient data.
- Manifest cache: `shipment_id, manifest_revision/version, status_at_download, container_codes, expected_delivery_at, cached_at`; only after successful authorized online fetch.
- Queue entry: `client_command_id UUID, shipment_id UUID, action, container_codes[], expected_manifest_version, client_performed_at, queued_at, local_status, attempts`.
- `client_command_id` is generated **once** per user action and preserved across retries. The queue is not cleared until the client has persisted the authoritative result.
- On logout/account switch: delete local manifests, commands and derived state; prevent one Courier from seeing another's queue.
- Offline device security: HTTPS, tight CSP, XSS protection, minimal local data, storage quota and expiry policy. IndexedDB does not guarantee encryption at rest on every device. For deployment on actual shared devices an additional security review is mandatory.
- If JWT expires while offline, actions can remain queued but **are not trusted**; user must reauthenticate before synchronization. Do not embed access token in queued payload.

**Default proposal, not approved product policy:** show a warning after 24h without synchronization and route to manual review. Do not auto-confirm or auto-delete an unacknowledged command solely due to age. Device backlog can be pruned only after definitive server acknowledgement or explicit user/admin intervention with audit.

## 3. Synchronization endpoint

`POST /api/v1/courier/sync` (authenticated, Bearer JWT). No separate batch-wide idempotency key is necessary: **each command contains an immutable `client_command_id`**, reused as its server idempotency key.

Request:

```json
{
  "commands": [
    {
      "client_command_id": "d7777777-7777-4777-8777-777777777777",
      "shipment_id": "33333333-3333-4333-8333-333333333333",
      "action": "COURIER_CONFIRM_PICKUP",
      "container_codes": ["LT-C-EXAMPLE-01", "LT-C-EXAMPLE-02"],
      "expected_manifest_version": 4,
      "client_performed_at": "2026-11-01T09:25:00Z"
    }
  ]
}
```

**API design rules:**
- Maximum 20 commands per batch, maximum 50 distinct container codes per command (**proposed operational limits**).
- Validate syntactic format before processing. Duplicate `client_command_id` with divergent payload inside one batch returns 422 for the batch.
- Per-command atomicity: process commands in supplied order and persist **each** one in a separate PostgreSQL transaction. A later failure must **not** roll back an earlier successful action. The response contains a result for each submitted command.
- To avoid ambiguous ordering across requests, client must not concurrently sync the same Shipment queue from multiple browser tabs. Server still detects races via Shipment lock; do not depend on client discipline alone.
- Before processing a command, authenticate current user, verify active COURIER membership in the Shipment region, assigned `courier_user_id`, current state, referenced manifest codes, and user authorization.
- `expected_manifest_version` identifies the sealed manifest snapshot, **not** the mutable `Shipment.version` (which increments during handoff). Introduce stable `manifest_revision` or `manifest_digest` fixed at seal; a sequence of valid handoff operations must not self-conflict due to Shipment.version changes.
- When a manifest mismatch or stale authorization exists, reject the command rather than silently rebasing.
- Batch response is `200` if schema and authentication succeeded, even when some individual commands conflict. Never report full success if only some applied.

Response:

```json
{
  "results": [
    {
      "client_command_id": "d7777777-7777-4777-8777-777777777777",
      "outcome": "APPLIED",
      "shipment_id": "33333333-3333-4333-8333-333333333333",
      "server_status": "IN_TRANSIT",
      "recorded_at": "2026-11-01T09:30:00Z",
      "error": null
    }
  ]
}
```

Per-command `outcome`:
- `APPLIED`: first successful commit, matching current state;
- `ALREADY_APPLIED`: exact replay of the same `client_command_id` and request payload; return the original authoritative recorded result;
- `REJECTED`: no mutation; include deterministic error object (`INVALID_STATE`, `MANIFEST_MISMATCH`, `FORBIDDEN`, `IDEMPOTENCY_KEY_REUSED`);
- `RETRYABLE`: no confirmed commit, transient infrastructure failure; retry **same** ID after backoff/re-auth.

**Commit uncertainty:** if network fails after a commit but before response, client retains command PENDING_SYNC and retries its unchanged `client_command_id`; server returns ALREADY_APPLIED. Never generate a new ID for a retry.

## 4. Server-side idempotency and lock order

Reuse `idempotency_records` with unique `(actor_id,operation,key)`. **For every Courier offline command use the same `operation='courier_offline_command'` and `key=client_command_id`, regardless of the underlying action or Shipment.** Compute `request_hash` over canonical `shipment_id`, `action`, `container_codes`, `expected_manifest_version` and `client_performed_at`. This ensures the same ID with a different action/Shipment/payload returns `IDEMPOTENCY_KEY_REUSED` instead of creating a second command.

Always:
1. Begin transaction; reserve/check idempotency record (unique index).
2. Check active actor/authorization; lock parent Shipment FOR UPDATE.
3. Verify state, manifest_revision, sorted/deduped scanned container codes, and handoff actor separation.
4. Apply transition + handoff update + audit event, save response in the same transaction, commit.
5. Return recorded result. Only then optional Celery best-effort notification.

**Record both** `client_performed_at` (claimed and untrusted) and `recorded_at` (server). They must never be confused in custody history. Response replay and client queue cleanup are safe even if notifications are lost.

If the queue contains pickup and delivery for the same shipment, process them sequentially. Delivery can succeed only after Pickup has already been committed to IN_TRANSIT; if state is not ready, reject/keep action for explicit user review, never silently force preceding transitions.

For normal online endpoints and sync, the **same application commands** must be used, not duplicate state machine implementations.

## 5. Failure / conflict matrix

| Scenario | Server behavior | Client behavior |
|---|---|---|
| Offline Courier scans all containers but Sender has not confirmed release | `REJECTED INVALID_STATE` | Show conflict; manual action required |
| Sender confirmed release; Courier submits valid pickup | APPLIED, IN_TRANSIT | Mark complete after response |
| Courier sends delivery while pending pickup not synced | REJECTED INVALID_STATE | Keep flagged, retry only after user review / sync pickup |
| Lab attempts receipt before Courier delivery sync | Lab API returns 409 INVALID_STATE | Lab waits for visible server delivery event |
| Same client_command_id retried with identical request | ALREADY_APPLIED, same recorded result | Drop persisted queue record safely |
| Same ID retried with different action/container list | REJECTED IDEMPOTENCY_KEY_REUSED | Never silently substitute payload |
| Courier account deactivated during offline interval | REJECTED FORBIDDEN / 401 after re-auth | Do not apply stale action |
| Shipment enters DELIVERY_BLOCKED while command is offline | REJECTED INVALID_STATE | Show manual resolution path |
| Container codes do not match sealed manifest | REJECTED MANIFEST_MISMATCH | Re-scan / contact operations |
| HTTP timeout after commit | Replayed result on retry | Preserve same ID; no duplicate event |
| JWT expires offline | HTTP 401, no processing | Re-login, then retry same command IDs |
| Device clock wrong | No change to server timestamp | Show server time and optional device estimate separately |
| Redis/Celery unavailable | Business command may still commit | No misleading transaction failure due to notification |

## 6. Observability and E2E tests

Metrics: queued commands per active Courier (client local only), sync latency, sync conflicts by code, number of REJECTED/RETRYABLE, duplicate replays, longest pending age, stalled handoff count.

Required integration tests: identical replay after server commit with lost HTTP response, concurrent sync/online pickup for same shipment, multiple queued actions in one batch with second conflict, stale manifest revision, stolen/expired token, revoked Courier membership, duplicate IDs with changed body, wrong container set, local queue surviving page reload, different-user logout purge.

**Not in MVP:** offline Lab acceptance, offline Sender release, conflict-free replicated data types, cross-device queue reconciliation, device certification, legally reliable offline-signature timestamps or physical GPS.
