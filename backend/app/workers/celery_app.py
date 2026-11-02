"""Celery configuration for best-effort notification scanning."""

from celery import Celery
from celery.signals import setup_logging

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
celery_app = Celery("labtrack", include=["app.workers.notifications"])


@setup_logging.connect
def _configure_celery_logging(**_kwargs: object) -> None:
    configure_logging(get_settings().log_level)


celery_app.conf.update(
    broker_url=settings.celery_broker_url.get_secret_value(),
    task_ignore_result=True,
    enable_utc=True,
    timezone="UTC",
    beat_schedule={
        "scan-stalled-notifications": {
            "task": "app.workers.notifications.scan_stalled_notifications",
            "schedule": settings.stalled_notification_scan_seconds,
        }
    },
)
