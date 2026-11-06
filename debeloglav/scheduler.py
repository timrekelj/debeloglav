"""Runs scheduled tasks when they are due. Tasks live in SQLite, so they survive restarts."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from croniter import croniter

from . import store
from .context import TIMEZONE

log = logging.getLogger("debeloglav.scheduler")

MIN_CRON_INTERVAL = timedelta(minutes=15)
POLL_SECONDS = 30


def next_cron_run(cron: str, after: datetime) -> datetime:
    """The next time a local-time cron expression fires after `after`, in UTC."""
    return croniter(cron, after.astimezone(TIMEZONE)).get_next(datetime).astimezone(UTC)


def cron_interval(cron: str) -> timedelta:
    """The shortest gap between runs over the next few firings. Raises ValueError for a bad expression."""
    if not croniter.is_valid(cron) or len(cron.split()) != 5:
        raise ValueError(
            f"{cron!r} is not a 5-field cron expression, e.g. '0 8 * * *'."
        )
    runs = croniter(cron, datetime.now(TIMEZONE))
    times = [runs.get_next(datetime) for _ in range(6)]
    return min(b - a for a, b in zip(times, times[1:]))


async def run_forever(run_task: Callable[[store.Task], Awaitable[None]]) -> None:
    """Every POLL_SECONDS, start each due task. A missed run fires once, late; it is not repeated."""
    running: set[asyncio.Task[None]] = set()
    while True:
        try:
            now = datetime.now(UTC)
            for task in store.due_tasks(now):
                # Move the task on before it runs, so a slow or failed run never fires twice.
                store.reschedule_task(
                    task.id, next_cron_run(task.cron, now) if task.cron else None
                )
                job = asyncio.create_task(_run(run_task, task))
                running.add(job)
                job.add_done_callback(running.discard)
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(POLL_SECONDS)


async def _run(
    run_task: Callable[[store.Task], Awaitable[None]], task: store.Task
) -> None:
    try:
        await run_task(task)
    except Exception:
        log.exception("Scheduled task #%s failed", task.id)
