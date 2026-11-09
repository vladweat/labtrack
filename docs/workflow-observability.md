# Workflow observability

LabTrack writes a small set of low-cardinality workflow events through the
existing JSON logger. There is no Prometheus dependency or metrics endpoint in
this repository. The events are available in the API/worker process logs; a
dashboard needs an existing JSON log collector, which this repository does not
deploy or configure.

## Events

| Event (`message`) | Meaning | Bounded fields |
|---|---|---|
| `offline_sync_command` | One server result for one Courier offline command | `command_type=pickup|delivery`, `outcome=applied|replayed|rejected|retryable|review_required`, `reason_category`, `count=1` |
| `custody_command_failure` | A failed known state-changing custody endpoint | `command_type`, status-derived `reason_category`, `count=1` |
| `stalled_notification_scan_completed` | Notification scan and threshold-qualified Pickup/Delivery backlog counts | `candidate_case_count`, `notification_insert_count`, `notification_dedup_count`, `stalled_pickup_count`, `stalled_delivery_count`, `duration_ms` |
| `stalled_notification_scan_failed` | Existing notification scan failure; task still raises for Celery retry/visibility | `outcome=failure`, `error_type` |
| `stalled_work_count_failed` | The separate read-only backlog aggregate failed; notification persistence remains committed | `error_type` |

Offline event outcomes mirror implemented results: `APPLIED` becomes `applied`,
`ALREADY_APPLIED` becomes `replayed`, and the existing `REJECTED`, `RETRYABLE`,
and `REVIEW_REQUIRED` values are normalized to lowercase. Offline sync does not
return a `CONFLICT` outcome, so none is invented. A stale-state rejection is
counted as `rejected` with `reason_category=stale_state`.

Custody failure reason categories come from the HTTP status only:
`authorization`, `missing_or_hidden`, `conflict`, `validation`, `rate_limited`,
`server_error`, or `request_error`. Offline categories use fixed internal
reason-code mappings and never emit raw error codes or messages.

The Pickup and Delivery fields count current cases past their configured stall
thresholds (defaults: four hours). They are computed by the existing shared
stalled-case SQL in the scheduled Celery task, currently every five minutes by
default. They do not count all pending work, do not require a notification
recipient, and do not alter custody state. The count query runs after notification
inserts commit and outside API request handling. If it fails, the scan logs
`stalled_work_count_failed`; notification persistence remains successful.

All event fields use fixed enums or numeric values. Events contain no patient,
sample, container, shipment, user, organization, or region identifiers; no email,
free text, document data, access token, or raw URL. The standard request logger
also includes a server-generated `request_id` for controlled log correlation.
Keep it as an ordinary log field: **do not promote `request_id`, route data, or
other per-request values into metric labels/indexed labels**. Only index the
bounded fields shown above. Apply the log platform's usual access and retention
controls to the complete application logs.

## Example dashboard and alerts

These are illustrative Loki LogQL queries. Their syntax follows the official
[LogQL query guide](https://grafana.com/docs/loki/latest/query/) and
[metric query reference](https://grafana.com/docs/enterprise-logs/latest/query/metric_queries/).
Replace the `service` stream label with the label your own collector uses; no
Loki, dashboard, or alert is deployed by this repository.

Offline command rate by bounded result and reason, over the last five minutes:

```logql
sum by (command_type, outcome, reason_category) (
  count_over_time(
    {service="labtrack-api"}
      | json
      | message="offline_sync_command"
      [5m]
  )
)
```

Custody failures over fifteen minutes:

```logql
sum by (command_type, reason_category) (
  count_over_time(
    {service="labtrack-api"}
      | json
      | message="custody_command_failure"
      [15m]
  )
)
```

Latest threshold-qualified backlog sample within the last ten minutes:

```logql
max by (service) (
  last_over_time(
    {service="labtrack-worker"}
      | json
      | message="stalled_notification_scan_completed"
      | unwrap stalled_pickup_count
      [10m]
  )
)
```

Use the same query with `unwrap stalled_delivery_count` for Delivery. A useful
starter dashboard has panels for offline outcomes, custody failures, and the two
backlog counts. Consider paging or notifying an operator when:

- the scheduled scan has no completion event for more than two expected scan
  intervals (10 minutes with the default five-minute interval), or emits
  `stalled_notification_scan_failed` repeatedly;
- `stalled_work_count_failed` occurs, since backlog visibility is unavailable
  even if notification inserts succeeded;
- Pickup/Delivery backlog remains nonzero across several scans, or offline
  `review_required` results need an operational review.

Treat these as investigation signals, not service-level objectives. A nonzero
backlog must not trigger automatic confirmation, withdrawal, closure, or other
custody mutation. `replayed` is expected idempotency behavior and should not
page by itself.

## Operator runbook

1. Find the relevant `offline_sync_command`, `custody_command_failure`, or
   worker scan event. Aggregate on bounded event fields only.
2. For one API request, correlate with the server-generated `X-Request-ID` in
   application logs. Do not use resource IDs or request IDs as dashboard labels.
3. For a stalled case, review the authorized
   `GET /api/v1/operations/stalled-shipments` view. It reads authoritative
   PostgreSQL state and is independent of Inbox delivery. Follow the workflow's
   existing role checks and escalation process; do not mutate state based only
   on a counter.
4. If the notification scan fails, check Celery worker/Beat and Redis health,
   then PostgreSQL connectivity. Inbox notifications are best effort; use the
   operations view as the source of current stalled cases.
5. If only `stalled_work_count_failed` appears, inspect the worker's database
   connectivity/query errors and confirm a later scan restores both count
   fields. That aggregate is independent of notification persistence.

The sample queries assume a JSON-capable log aggregation platform and are not
verified against a deployed collector. Without one, inspect local Compose logs
or the deployment's existing process logs; the repository has no durable log
retention, dashboard, or alerting configuration.
