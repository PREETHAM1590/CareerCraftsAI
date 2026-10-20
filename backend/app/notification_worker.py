"""Run with `python -m app.notification_worker`.

Executes NotificationWorkflow and its activities on their own task queues,
separate from the main temporal-worker (app/temporal_worker.py) — deploy
this on different hardware if you like, so a notification/email backlog can
never starve the job-search/application worker's capacity.

Two Worker instances share this process:
  - "notifications": NotificationWorkflow, create_notification_activity,
    mark_delivery_dead_activity. No special rate limit — these only touch
    the database.
  - "notifications-email": send_notification_email_activity only, capped at
    NOTIFICATION_EMAIL_RATE_LIMIT_PER_SECOND (server-enforced by Temporal,
    holds even with several worker processes polling this queue at once) to
    stay under Resend's own per-team rate limit.
"""

import asyncio
import logging
import signal

from temporalio.worker import Worker

from app.core.config import settings
from app.core.temporal_client import get_temporal_client, reset_temporal_client
from app.workflows.notification_registry import (
    NOTIFICATION_ACTIVITIES,
    NOTIFICATION_EMAIL_ACTIVITIES,
    NOTIFICATION_WORKFLOWS,
)

logger = logging.getLogger(__name__)


async def main() -> None:
    settings.validate_temporal_configuration()
    client = await get_temporal_client()

    notifications_worker = Worker(
        client,
        task_queue=settings.TEMPORAL_NOTIFICATION_TASK_QUEUE,
        workflows=NOTIFICATION_WORKFLOWS,
        activities=NOTIFICATION_ACTIVITIES,
        max_concurrent_activities=settings.NOTIFICATION_WORKER_CONCURRENCY,
    )
    email_worker = Worker(
        client,
        task_queue=settings.TEMPORAL_NOTIFICATION_EMAIL_TASK_QUEUE,
        activities=NOTIFICATION_EMAIL_ACTIVITIES,
        max_concurrent_activities=settings.EMAIL_WORKER_CONCURRENCY,
        max_task_queue_activities_per_second=settings.NOTIFICATION_EMAIL_RATE_LIMIT_PER_SECOND,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    logger.info(
        "Notification worker starting: address=%s namespace=%s queues=%s,%s "
        "email_rate_limit=%.1f/s",
        settings.TEMPORAL_ADDRESS,
        settings.TEMPORAL_NAMESPACE,
        settings.TEMPORAL_NOTIFICATION_TASK_QUEUE,
        settings.TEMPORAL_NOTIFICATION_EMAIL_TASK_QUEUE,
        settings.NOTIFICATION_EMAIL_RATE_LIMIT_PER_SECOND,
    )
    async with notifications_worker, email_worker:
        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass
    logger.info("Notification worker shut down gracefully")
    reset_temporal_client()


if __name__ == "__main__":
    logging.basicConfig(level=settings.LOG_LEVEL)
    asyncio.run(main())
