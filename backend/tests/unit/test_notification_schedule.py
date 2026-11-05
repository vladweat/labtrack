"""Celery Beat registration contract for the best-effort notification scan."""

from app.workers.celery_app import celery_app
from app.workers.notifications import scan_stalled_notifications

TASK_NAME = "app.workers.notifications.scan_stalled_notifications"


def test_notification_scan_is_registered_once_at_the_five_minute_interval() -> None:
    scheduled_scans = [
        entry for entry in celery_app.conf.beat_schedule.values() if entry.get("task") == TASK_NAME
    ]

    assert len(scheduled_scans) == 1
    assert scheduled_scans[0]["schedule"] == 300
    assert celery_app.tasks[TASK_NAME].name == scan_stalled_notifications.name == TASK_NAME
