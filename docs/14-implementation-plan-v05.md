# LabTrack v0.5 — Implementation Roadmap and Verification

> **Исторический документ (до v0.6).** Приоритет имеют [Decisions v0.6](15-decisions-v06.md), [Domain v0.6](16-domain-workflows-v06.md), [Data Model v0.6](17-data-model-v06.md), [API v0.6](18-api-v06.md). Этот текст может содержать устаревшие all-or-nothing handoff и unilateral resolution; не использовать его как актуальный контракт.

**Goal:** production-like MVP with synthetic data, reproducible deployment, real PostgreSQL integration tests, CI, observability and a Courier offline queue. No real healthcare deployment is claimed.

**Approved product decisions:** [10-domain-hardening-v05.md](10-domain-hardening-v05.md).  
**Design specs:** [Data Model](12-data-model-v05.md), [API](13-api-v05.md), [Offline Sync](11-offline-sync-protocol.md).

## 1. Scope boundaries

**Included:** one pilot region, one central laboratory, multiple senders, individual tubes in containers, bilateral handoffs, MISSING/REJECTED at sample/container levels, manual blocked-delivery resolution, correctable receiving and lab-manager reopening, opaque external references, short-lived local JWT auth, offline Courier sync, audit, dashboards, best-effort notifications.

**Excluded:** actual patient data, real LIS/MIS integration, clinical evaluation, GPS/IoT, legally qualified digital signatures, multiple regional production deployments, automatic partial delivery and redistribution, multi-hop routing, offline Sender/Lab workflows and reusing samples from a cancelled Shipment.

Two operational assumptions still need product approval **before a real pilot**: physical custody of refused containers and legal meaning of offline timestamps. They must never be silently inferred from a successful sync.

## 2. Engineering increments

| Increment | Deliverables | Acceptance |
|---|---|---|
| A. Foundation | FastAPI, typed config, PostgreSQL, SQLAlchemy, Alembic, Docker Compose, health endpoints, structured logs, pytest, CI | Reproducible empty environment; migrations apply; health/readiness and CI pass |
| B. Access & Reference Data | Bootstrap CLI, users, Argon2id auth, short-lived JWT, org/region memberships, reason-code fixtures | Negative IDOR and role tests; regional ops/lab manager scoped |
| C. Tubes & Manifest | Samples, opaque external refs, Shipment, containers/items, Courier assignment, seal, PostgreSQL manifest triggers | Unique sample assignment; 1..N populated containers; direct SQL sealed mutation rejected |
| D. Handoffs | Pickup and Delivery, two actors, idempotency, Handoff attempts, audit | One confirmation never finalizes; no self-confirmation; concurrent commands safe |
| E. Container exceptions | Report MISSING/REJECTED container, block delivery, regional manual RETRY/CLOSE | No partial handoff; old handoff withdrawn on block; child samples unchanged |
| F. Receiving & Reopen | Sample results, reason codes, revisions, sessions, manager reopen, summary metrics | All item results explicit; revisions preserve audit, outcomes recomputed on completion |
| G. Offline Courier | Courier PWA offline cache, durable queue, sync API, client-command IDs, conflict UI | Offline command never changes server before sync; secure replay after lost response |
| H. Production-like finish | Celery/Redis best-effort notifications, stalled dashboards, metrics, CI quality gates, backups/runbooks/load tests | End-to-end acceptance on synthetic data; recovery/observability demonstrated |

Do not build the offline client before server handoff/state invariants and sync endpoint integration tests are stable. Backend modules should expose shared application commands for online REST and offline sync to avoid divergent logic.

## 3. Architecture changes in place

- Keep modular monolith; extend `shipments` module with delivery block/manual resolution and receiving sessions. Do not create separate distributed services.
- Introduce region-scoped `REGIONAL_OPERATIONS` and lab-scoped `LAB_MANAGER`, not blanket superusers.
- `container_issues` is authoritative only for container-level problems; `container_items` is authoritative for tube outcomes.
- Freeze sealed manifest with PostgreSQL triggers and domain checks; receipt revisions remain writable while receiving is reopened.
- Treat every offline command as a queued client **proposal**, later accepted or rejected by the server after full authorization, state and idempotency checks.
- Keep Celery notifications best effort; Dashboard must still surface stalled handoffs if Redis or worker delivery fails.

## 4. Test matrix and non-negotiable CI coverage

| Category | Required scenario |
|---|---|
| Unit | State transitions, reason-code matching, scope, manifest comparisons, offline result mapping |
| SQL integrity | One tube in one container; region constraints; issue uniqueness; one OPEN receiving session; sealed manifest triggers |
| Concurrency | Seal vs item update; two competing Courier confirmations; double issue reporting; double resolution; concurrent reopen |
| Chain of custody | Sender only → PICKUP_PENDING; Courier only delivery → DELIVERY_PENDING; no automatic tube acceptance |
| Container exceptions | Missing/refused container blocks delivery; no partial receipt; manual retry creates new handoff attempt |
| Receiving | MISSING/REJECTED require matching codes; corrections before close; manager-only reopen preserves history |
| Offline | Retry after lost response; stale status or manifest; revoked Courier, expired token, concurrent online/offline action |
| Security | Cross-org IDOR, cross-region ops, read-only analyst, inaccessible external reference, no patient data in payload or logs |
| Failure recovery | Audit insertion failure rolls back business change; Redis down cannot erase committed business state |
| E2E | 10 tubes / 2 containers, 4 confirmations, 8 accepted/1 missing/1 rejected; plus blocked container and reopened receiving scenario |

Use a real PostgreSQL instance (e.g. Docker test service) for all concurrency and trigger tests; SQLite does not provide equivalent semantics.

## 5. Suggested quality gates

- Format/lint/type-check: Ruff and a static type checker; project chooses exact versions at implementation.
- Unit + integration + API schema contract tests, with CI fail on migration drift and schema invalidity.
- Generate actual FastAPI OpenAPI at runtime and diff against the agreed design contract. Maintain one authoritative source after implementation.
- Seed only synthetic identities and sample codes.
- Basic load tests on representative data, with measured p50/p95 (do not assert undocumented speedups).
- Structured request/audit logs, health/live and health/ready, basic dashboard and incident metrics.
- Backup + restore smoke test before declaring production-like readiness; success metrics documented but no unverified SLA.
- Non-root containers, secret handling, no sensitive data in exceptions, minimal JWT lifetime and auth throttling.

## 6. Definition of Done for v0.5 documentation

- [x] 20 questionnaire choices recorded without erasing prior agreed decisions.
- [x] Separate container-level issue and per-tube receipt concepts documented.
- [x] Delivery blocked, administrative close and retry flows specified.
- [x] Reopening and immutable receipt history specified.
- [x] Courier offline protocol and per-command idempotency defined.
- [x] PostgreSQL schema delta / trigger invariants documented.
- [x] API deltas documented; OpenAPI draft aligned with new routes.
- [ ] Operational custody assumptions approved by relevant stakeholder.
- [ ] Real schema migration implemented and tested (implementation stage).
- [ ] OpenAPI draft validated against generated FastAPI schema (implementation stage).
- [ ] Backend, PWA, CI and automated tests implemented (implementation stage).

**Documentation-complete is not implementation-complete.** v0.5 stabilizes the intended model and identifies remaining approvals; no migrations, HTTP endpoints, browser client or production infrastructure are implied by this plan.
