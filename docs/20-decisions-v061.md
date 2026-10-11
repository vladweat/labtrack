# LabTrack v0.6.1 — Decision Register (Specification Hardening)

**Status: APPROVED / FROZEN for the synthetic MVP (2026-11-01).** All 19 hardening answers and OPEN-01–04 have explicit user approval. At the time this decision register was written, no backend, migrations or tests had been implemented. Approved requirements override conflicting v0.6 assumptions. Real-world compliance remains out of scope.

## 1. Approved — Chain of custody (P0)

| ID | Choice | Accepted rule |
|---|---|---|
| Q01 | A | After confirmed Pickup, unavailable assigned Courier is **not replaced retroactively**. Affected not-presented containers use MISSING followed by regional proposal and Lab Manager approval of RETRY/CLOSE_UNDELIVERED. No in-product custody transfer to a substitute |
| Q02 | A | Sender may withdraw pending Pickup. Where Courier cannot be replaced after seal, Shipment may be cancelled **under existing cancellation rules** without another transport |
| Q03 | B | Physically presented container without available Courier confirmation may use **exceptional receipt attested by two independent witnesses**, recorded with audit; it must not masquerade as a Courier-signed handoff |
| Q04 | A | A problem discovered after container RECEIVED never rewrites confirmed handoff or Lab custody; each affected Sample individually becomes REJECTED with valid reason_code + audit |
| Q05 | A | REGIONAL_OPERATIONS proposer can withdraw their own PENDING ContainerIssue resolution proposal with reason + audit; LAB_MANAGER can approve/reject; max one PENDING proposal per issue |
| Q06 | B | For prolonged stalled Handoff, regional operator opens incident/review; destination LAB_MANAGER or origin Sender confirms the relevant side; never forge missing participant confirmation |

**Safety implication of Q02 (proposed):** normal withdrawal/cancel is valid only when physical release has not occurred; if custody is disputed, resolve through incident/review rather than implying samples never left.

## 2. Approved — Offline, conflict, idempotency (P0)

| ID | Choice | Accepted rule |
|---|---|---|
| Q07 | B | Use server-issued manifest snapshot time/version; device time is only indicative; questionable/old actions go to manual review, never auto-confirm |
| Q08 | A | Keep full replay responses for offline commands for **90 days**, then compact applied-command tombstones in history prevent custody reapplication; ordinary HTTP idempotency keys may use separate TTL |
| Q09 | A | **Any relevant change** in Shipment state before synchronization triggers REJECTED + manual resolution; never automatically rebase |
| Q10 | A | Two or more devices may keep independent Courier queues, UUID client_command_id, server locks and stale checks; no automatic queue merge |
| Q11 | A | After review of offline command older than 24h, **do not apply original stale payload retroactively**: new authenticated live operation if state permits; otherwise incident |

Server-issued snapshot timestamp is **not forensic proof of the physical action time**. The server cannot observe commands while entirely offline.

## 3. Approved — Reference data and API (P1)

| ID | Choice | Accepted rule |
|---|---|---|
| Q12 | A | expected_delivery_at optional in DRAFT, **required and future relative to server time at seal** |
| Q13 | A | Pilot has exactly one configured/seeded destination_lab_id. Validate CreateShipment and seal; client cannot redirect to another laboratory |
| Q14 | A | Fixed six-code scope/kind reason catalog seeded at bootstrap/migrations, Russian labels; no admin CRUD |
| Q15 | B | Persistent in-app notification Inbox with read/unread, own PostgreSQL table and API, best-effort Celery delivery |

## 4. Approved — Analytics, timeouts (P1)

| ID | Choice | Accepted rule |
|---|---|---|
| Q16 | A | Analytics calendar buckets day/week **UTC**; UI converts timestamps to user's display timezone |
| Q17 | A | Full Shipment delivery duration counts **only if every container RECEIVED**, from confirmed Pickup server time to latest Container RECEIVED server time. Partial Shipment: separate per-received-container metric. No successful times for undelivered |
| Q18 | A | Current dashboard derives latest Shipment + Sample values; history from append-only audit/revisions; reopening never duplicates Shipment or counters |
| Q19 | A | Config demo thresholds: PICKUP_PENDING 4h, DELIVERY_PENDING 4h, resolution proposal PENDING 24h; expected_delivery_at for overdue. **Alert only**, never auto-confirm |

### Fixed initial reason catalog: proposed scope/kind mapping of Q14

| code | scope | kind | RU label |
|---|---|---|---|
| SAMPLE_NOT_FOUND | SAMPLE | MISSING | Пробирка не обнаружена |
| SAMPLE_LABEL_MISMATCH | SAMPLE | REJECTED | Маркировка пробирки не соответствует |
| SAMPLE_CONTAINER_COMPROMISED | SAMPLE | REJECTED | Нарушена целостность контейнера с пробиркой |
| CONTAINER_NOT_PRESENT | CONTAINER | MISSING | Контейнер не предъявлен |
| CONTAINER_SEAL_BROKEN | CONTAINER | REJECTED | Пломба контейнера нарушена |
| CONTAINER_LABEL_MISMATCH | CONTAINER | REJECTED | Маркировка контейнера не соответствует |

## 5. Preserved from v0.6

- One pilot region, one central Lab, only synthetic data; Sample in sealed Container in Shipment; physical manifest unchanged after seal (service + PostgreSQL triggers).
- **Normal** Pickup: Sender + assigned Courier, whole Shipment. **Normal** Delivery: Courier + Lab, per container; partial receiving allowed for independently RECEIVED containers.
- MISSING and REJECTED remain the only *logistics discrepancy types*. CustodyIncident is an administrative case, not a third discrepancy kind.
- REGIONAL_OPERATIONS proposes resolution and a different destination LAB_MANAGER approves; rejected or withdrawn proposals do not mutate container custody.
- Manager reopen with reason + append-only revisions. Undelivered container items have NULL outcomes; mixed completion and terminal closure follow v0.6.
- Redis/Celery never authoritative for physical custody.

## 6. Final approvals — OPEN-01–04 are **CLOSED / APPROVED**

The user explicitly approved all four recommendations on 2026-11-01. No additional product vote is required for the synthetic MVP.

| Decision | Final approved rule |
|---|---|
| OPEN-01 | Exceptional container receipt requires **two different physically present, authenticated employees of the destination laboratory**, including **at least one active LAB_MANAGER**. Each confirms personally, in their own account; a user holding multiple roles still counts once. |
| OPEN-02 | **No exceptional RECEIVED without a confirmed whole-Shipment Pickup.** If Pickup is pending, disputed or unconfirmed, first resolve its own CustodyIncident. Incident resolution does not fabricate a Courier confirmation; proceed only if the original Pickup is actually confirmed via the normal authorized process. |
| OPEN-03 | **Regional operations starts review.** SENDER_OPERATOR provides origin-side facts; destination LAB_MANAGER confirms lab-side facts. A separate **administrative incident decision** records the outcome, never a missing actor's digital signature. See the deterministic role/outcome matrix in [Custody Exceptions](21-custody-exceptions-v061.md). |
| OPEN-04 | A stale Courier command is rejected for change in **manifest_revision, target Container/Handoff version, confirmed Pickup/custody phase or current actor authorization**. Unrelated changes elsewhere in Shipment (e.g. a different container's tube classification) are *not automatically* relevant and must not cause conflict when every command dependency is unchanged. Never silently rebase a relevant change. |

### Approved implementation policy derived from OPEN-03

For the administrative decision in OPEN-03, REGIONAL_OPERATIONS of the Shipment region records a **separate incident resolution** after required first-party attestations; their administrative action does **not** become a Courier or Sender Handoff confirmation.

- **Origin/Pickup review:** active Sender Operator of origin org attests origin-side facts. If physical release is confirmed *not to have occurred*, Sender may exercise the normal Pickup withdrawal/cancellation workflow. Regional closure of an incident does not itself mark Pickup CONFIRMED. If whereabouts are disputed, record unresolved/escalated outcome and preserve Handoff state.
- **Destination/Delivery review:** destination LAB_MANAGER attests lab-side facts. For exceptional receipt, the OPEN-01 two-person destination Lab testimony (one LAB_MANAGER) is mandatory, along with confirmed Pickup; region operator's separately audited resolution may verify it and mark the container RECEIVED **with EXCEPTIONAL evidence**, not a forged normal Handoff. Missing/no evidence: keep delivery unconfirmed and record unresolved outcome or existing ContainerIssue path.
- **Independence:** resolution reviewer may not attest as the absent participant; a regional resolver cannot be one of the two lab witnesses. Authorization is checked at decision time, and audit records who reviewed evidence.

These mechanics specify how the approved separation of roles is implemented; they are not legal custody certifications.

### Non-blocking external review (OPEN-05)

Real medical regulation, formal custody evidence, retention/privacy, and clinical compliance require expert sign-off **before real deployment**. They are not requirements for the synthetic-only MVP.

**Specification gate:** OPEN-01–04 are resolved; exceptional and offline workflows are now **approved for implementation** (subject to ordinary code review, real PostgreSQL tests, authorization enforcement, audit and security tests). There are no remaining product-approval blockers for the MVP.

## 7. Canonical v0.6.1 follow-ups

[Custody exceptions](21-custody-exceptions-v061.md) · [Offline integrity](22-offline-integrity-v061.md) · [Data Model](23-data-model-v061.md) · [API & operations](24-api-and-operations-v061.md) · [Test Plan](25-test-plan-v061.md) · [OpenAPI draft](../openapi/labtrack-v1.json).

Earlier v0.6 documents remain as history; this decision register has precedence on changed choices.
