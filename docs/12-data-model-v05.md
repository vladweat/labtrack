# LabTrack v0.5 — PostgreSQL Data Model Delta and Migration Design

> **Исторический документ (до v0.6).** Приоритет имеют [Decisions v0.6](15-decisions-v06.md), [Domain v0.6](16-domain-workflows-v06.md), [Data Model v0.6](17-data-model-v06.md), [API v0.6](18-api-v06.md). Этот текст может содержать устаревшие all-or-nothing handoff и unilateral resolution; не использовать его как актуальный контракт.

**Status:** proposed physical schema changes, **no SQL migrations have been executed**. The v0.4 schema is documented in [06-data-model.md](06-data-model.md). Accepted product decisions and state transitions: [10-domain-hardening-v05.md](10-domain-hardening-v05.md).

## 1. Change strategy

Keep existing modular monolith and current tables; **migrate forward**, do not rewrite old migration history. PostgreSQL remains the sole source of truth. A Shipment is still the lock boundary. A tube may belong to one container only, and cancellation never releases a tube for reshipping in P0.

Two central choices reduce accidental dual truth:

- **Sample discrepancies** remain the current receipt fields on `container_items`, with revisions recorded separately. Do not create second mutable SampleIssue status.
- **Container discrepancies** live in `container_issues` because they occur **before receiving**, while physical handoff is still blocked.

## 2. New / extended tables

| Table | Fields | Constraints / notes |
|---|---|---|
| `issue_reason_codes` | `id UUID PK, scope TEXT, kind TEXT, code TEXT, label TEXT, is_active BOOLEAN, created_at TIMESTAMPTZ` | `scope IN ('SAMPLE','CONTAINER')`, `kind IN ('MISSING','REJECTED')`, `UNIQUE(scope,kind,code)`; deactivation preserves historical reference |
| `container_issues` | `id, shipment_id, container_id, kind, reason_code_id, comment NULL, status, reported_by, reported_at, resolved_by NULL, resolved_at NULL, resolution NULL, resolution_comment NULL` | FK shipment/container/user/reason; `kind=MISSING|REJECTED`; `status=OPEN|RESOLVED|CLOSED_UNDELIVERED`; no two OPEN issues for one container within a Shipment; container belongs to Shipment; record even if actual container absent |
| `receiving_sessions` | `id, shipment_id, sequence, status, opened_by, opened_at, reopen_reason NULL, closed_by NULL, closed_at NULL` | `UNIQUE(shipment_id,sequence)`; `status=OPEN|CLOSED`; at most one OPEN session per shipment |
| `receipt_revisions` | `id, receiving_session_id, container_item_id, old_result NULL, new_result, old_reason_code_id NULL, new_reason_code_id NULL, old_comment NULL, new_comment NULL, changed_by, changed_at` | Append-only; every change including initial result produces revision; old values copied from locked item; receiving session belongs to item's Shipment |
| `sample_external_references` | `id, sample_id, system_code, reference_kind, external_id, created_at` | `UNIQUE(sample_id,system_code,reference_kind,external_id)`; opaque synthetic refs only; cardinality 1:N; no patient identifiers |

**Existing tables to extend:**

- `shipments.status` CHECK adds `DELIVERY_BLOCKED`, `CLOSED_UNDELIVERED`; add `closed_at TIMESTAMPTZ NULL, closed_by UUID NULL, closure_reason TEXT NULL` for manual undelivered closure. Add `manifest_revision INTEGER NOT NULL DEFAULT 0`: increment during DRAFT manifest edits, freeze at seal, and keep independent of `shipments.version`.
- `organization_memberships.role` allows `LAB_MANAGER` in a laboratory.
- `regional_memberships.role` allows `REGIONAL_OPERATIONS` in a region, separate from read-only analyst.
- `container_items`: replace text-only `receipt_reason` with `receipt_reason_code_id UUID NULL` and `receipt_comment TEXT NULL`; preserve `receipt_result`, `receipt_recorded_by`, `receipt_recorded_at`. For a repository with real data, backfill first, then remove `receipt_reason`; current repo is documentation-only.
- `handoffs`: add `withdrawn_by UUID NULL`, `withdrawal_reason TEXT NULL`; a blocked Delivery can withdraw a PENDING handoff via scoped operational action and its audit; keep original actor/times.
- `audit_events`: use existing append-only table; event type + safe details including `client_command_id` and untrusted `client_performed_at` when applicable. Do not replace `occurred_at` with a device timestamp.
- `idempotency_records`: reuse current key/operation/request hash/response schema; enforce global lock-order protocol. For all offline commands set `operation='courier_offline_command'` (one stable namespace per actor) so reusing `client_command_id` with a different action or Shipment collides under `UNIQUE(actor_id,operation,key)`; the canonical request hash includes the entire command. **No duplicate offline-command persistence table is mandatory in MVP**: retain per-command replay responses here; per-command audit gives diagnostics.

**Demo constraint:** sample codes, organization codes, external references and all fixtures are synthetic. An opaque external ID can be linkable to a patient in a real LIS; therefore it is **not automatically anonymous**. Access scope and retention for real data require separate review.

## 3. CHECK and FK rules

`container_items`:

```sql
CHECK (
  (
    receipt_result IS NULL AND receipt_reason_code_id IS NULL
    AND receipt_comment IS NULL AND receipt_recorded_at IS NULL
    AND receipt_recorded_by IS NULL
  ) OR (
    receipt_result = 'ACCEPTED' AND receipt_reason_code_id IS NULL
    AND receipt_comment IS NULL AND receipt_recorded_at IS NOT NULL
    AND receipt_recorded_by IS NOT NULL
  ) OR (
    receipt_result IN ('MISSING','REJECTED')
    AND receipt_reason_code_id IS NOT NULL
    AND receipt_recorded_at IS NOT NULL AND receipt_recorded_by IS NOT NULL
  )
);
```

`receipt_comment` length 1..500 when present after trimming (CHECK). Note: FK to an existing reason-code row is not alone enough to validate its `scope` and `kind`; implement a composite FK on `(scope,kind,code)` using appropriate schema fields or a protected database trigger, **plus** service validation. Do not claim plain `reason_code_id` FK enforces matching scope/kind.

`container_issues`:

- Require non-null reason code for both kinds, optional comment.
- Verify container belongs to Shipment using a composite FK: add `UNIQUE(transport_containers.id, shipment_id)` and reference `(container_id,shipment_id)`.
- Deny OPEN issue duplicate for the same `(shipment_id,container_id)`:

```sql
CREATE UNIQUE INDEX uq_container_open_issue
ON container_issues (shipment_id, container_id)
WHERE status = 'OPEN';
```

- `resolved_by`, `resolved_at`, `resolution` are required for `RESOLVED` / `CLOSED_UNDELIVERED`; `resolution` is RETRY_DELIVERY or CLOSE_UNDELIVERED.
- `issue_reason_codes.scope='CONTAINER'` and matching `kind` must be verified in the command layer or enforced with a composite FK/trigger.

`receiving_sessions`:

```sql
CREATE UNIQUE INDEX uq_one_open_receiving_session
ON receiving_sessions (shipment_id)
WHERE status = 'OPEN';
```

Every new session increases sequence under the locked Shipment. The close operation marks the session CLOSED in the same transaction as Shipment finalization. No session is opened for CLOSED_UNDELIVERED.

`receipt_revisions` must be append-only and tied to the right Shipment; otherwise a wrong session could be used to forge an edit. Service validation, protected FK model, and DB restrictions on UPDATE/DELETE provide the guards. Create a revision on **every** receipt update, not just corrections.

`handoffs`: a pending Delivery handoff may be withdrawn by REGIONAL_OPERATIONS when a valid container issue blocks delivery. Persist `withdrawn_by`, `withdrawal_reason`, `withdrawn_at` in the same transaction. Preserve prior `attempt_no` history. Existing partial UNIQUE index still limits one non-withdrawn handoff per kind.

## 4. PostgreSQL trigger for sealed manifest

**Goal:** after seal, block changes to the **identity and membership** of every Shipment container and every Sample in the manifest, even via accidental SQL paths. Continue to allow normal receipt updates in RECEIVING/reopened RECEIVING.

Recommended trigger guards:

1. On `transport_containers` INSERT/DELETE or UPDATE of `shipment_id/code`: derive parent Shipment and acquire `SELECT ... FOR UPDATE` on `shipments`; only permit parent `DRAFT`.
2. On `container_items` INSERT/DELETE or UPDATE of `container_id/sample_id`: resolve parent Shipment, lock it first and require `DRAFT`.
3. For UPDATE of **receipt-only fields**, allow only while parent Shipment is `RECEIVING`, with OPEN receiving session and the current actor authorized by the application. Note: a DB trigger without a reliable DB actor context cannot enforce user RBAC; application must enforce that separately.
4. Updates to `samples.code/origin_organization_id` are forbidden after creation. Moving a Sample between containers is allowed only during `DRAFT` under the same Shipment lock.
5. Locking must follow a consistent order. Application first locks parent Shipment; trigger then reuses/rechecks the parent lock. Direct SQL also blocks on it, preventing a `seal` race.
6. For movement between different Shipments, a lock order based on sorted Shipment IDs is required if supported; alternatively **do not support direct cross-Shipment moves** in MVP.
7. Runtime DB role cannot disable triggers; migration owner role is distinct. Test with a direct SQL mutation after seal: it must fail and leave records unchanged.

**Important PostgreSQL correctness detail:** a BEFORE row-level trigger that reads a parent must acquire a lock before checking its status to avoid races with concurrent `seal`. A naive unprotected SELECT would not suffice. Trigger design should be reviewed with concurrent PostgreSQL integration tests.

The manifest remains fixed after seal even if a lab manager reopens receiving.

## 5. Transaction boundary and lock order

For **every** write use case (online and offline sync), use this order:

1. Authenticate actor and validate basic request.
2. Open DB transaction and reserve/check idempotency key, if required.
3. Lock Shipment `SELECT ... FOR UPDATE`.
4. Check current role/scope and state; lock/update child rows in deterministic order.
5. Append audit event, set updated aggregate `version`, save replayable response.
6. Commit; then publish optional best-effort Celery notification.

Never create a code path that locks child objects then parent Shipment, or locks a Shipment before reserving its idempotency key. Concurrent use of the **same idempotency key** is serialized by the unique key; other keys serialize on Shipment. For commands touching distinct Shipments (e.g., unusual future reallocation), establish global lock ordering.

For permission changes during retries, re-check that the actor is currently active/authorized **before disclosing** any saved replay response. A cache hit must not resurrect revoked access.

## 6. Migration sequence and indexes

1. Add new role values and Shipment status CHECK; update application enum validation at the same deployment boundary.
2. Create reason codes; seed stable codes for each scope+kind, with no PHI.
3. Add receipt code/comment columns and receiving_sessions/receipt_revisions; backfill any existing data in staged migration if the database is not empty.
4. Add container_issues and extend handoffs for privileged withdrawal and closure metadata.
5. Add sample_external_references; update scoped queries.
6. Add indexes on `container_issues(shipment_id,status)`, `receiving_sessions(shipment_id,sequence DESC)`, `receipt_revisions(container_item_id,changed_at)`, `sample_external_references(sample_id)`, and `issue_reason_codes(scope,kind,is_active)`.
7. Create trigger functions and attach them with migrations; verify direct SQL and concurrent edit vs seal.
8. Run Alembic upgrade + smoke checks on real PostgreSQL; test rollback/forward migration on disposable database before production.

**Do not use the SQL blocks above as complete runnable migrations.** They are precise design requirements; actual migration scripts must define types, defaults, FK and privileges consistently.

## 7. High-priority tests

- Container MISSING and container REJECTED must not silently mark any sample RECEIVED/MISSING/REJECTED.
- Blocking from IN_TRANSIT or DELIVERY_PENDING is atomic and closes pending delivery handoff if present.
- Closing blocked Shipment yields CLOSED_UNDELIVERED and **never** counts as successful delivery.
- RETRY_DELIVERY requires regional operations authorization, no OPEN issues, and a new Delivery attempt.
- Only LAB_MANAGER can reopen; revisions/audit preserve the original receipt outcomes.
- Concurrent reopening allows one OPEN session; concurrent receipt updates serialize on Shipment.
- Direct SQL INSERT/UPDATE/DELETE on sealed manifest fails; receipt fields still editable only during RECEIVING under the guarded path.
- Deactivated reason code cannot be chosen for new incidents, but historical records keep their code.
- Unique constraint retains samples after cancelled Shipment: repeat assignment remains forbidden, by product decision.
- Offline command replay with a different payload under same ID returns a conflict without double event.
