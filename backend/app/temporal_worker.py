"""Run with `python -m app.temporal_worker`. Scale this service independently.

This process executes every durable job — agent runs, job searches,
applications, follow-ups — and registers the recurring Schedules (daily job
search and maintenance). Nothing
the API starts makes progress unless at least one of these is running.
"""

import asyncio
import logging
import signal

from temporalio.worker import Worker

from app.core.config import settings
from app.core.temporal_client import get_temporal_client, reset_temporal_client
from app.workflows.registry import ACTIVITIES, WORKFLOWS
from app.workflows.scheduled import ensure_schedules

logger = logging.getLogger(__name__)


async def main() -> None:
    settings.validate_temporal_configuration()
    client = await get_temporal_client()
    if settings.TEMPORAL_SCHEDULES_ENABLED:
        await ensure_schedules(client)

    worker = Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        max_concurrent_activities=settings.TEMPORAL_WORKER_CONCURRENCY,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows' default loop —
            # Ctrl+C still raises KeyboardInterrupt, caught below.
            pass

    logger.info(
        "Temporal worker starting: address=%s namespace=%s task_queue=%s concurrency=%d",
        settings.TEMPORAL_ADDRESS,
        settings.TEMPORAL_NAMESPACE,
        settings.TEMPORAL_TASK_QUEUE,
        settings.TEMPORAL_WORKER_CONCURRENCY,
    )
    async with worker:
        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass
    logger.info("Temporal worker shut down gracefully")
    reset_temporal_client()


if __name__ == "__main__":
    from app.core.pii_vault import install_pii_log_scrubbing

    logging.basicConfig(level=settings.LOG_LEVEL)
    install_pii_log_scrubbing()
    asyncio.run(main())
