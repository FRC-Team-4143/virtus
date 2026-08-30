"""
APScheduler jobs:
  1. Legion roster sync — hourly incremental pull.
  2. Review reminders — a daily nudge to anyone still owing a review on a cycle that is
     about to close.
  3. Weekly SQLite backup.
"""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import AsyncSessionLocal

log = logging.getLogger(__name__)


async def job_nightly_backup() -> None:
    from app.services.backup import is_sqlite, nightly_backup
    if not is_sqlite():
        return
    try:
        nightly_backup()
    except Exception:  # never let a backup failure crash the scheduler
        log.exception("Backup failed")


async def job_legion_sync() -> None:
    """Pull the roster from Legion. No-op (with a log line) when Legion isn't configured."""
    if not settings.updates_enabled:
        log.info("Legion sync skipped (updates_enabled=false)")
        return
    if not settings.legion_base_url or not settings.legion_api_key:
        log.info("Legion sync skipped (LEGION_BASE_URL/LEGION_API_KEY not set)")
        return
    from app.services.legion_sync import sync_roster
    try:
        async with AsyncSessionLocal() as db:
            summary = await sync_roster(db)
        log.info("Scheduled Legion sync: %s", summary)
    except Exception:  # never let a sync failure crash the scheduler
        log.exception("Scheduled Legion sync failed")


async def job_review_reminders() -> None:
    """DM anyone still owing a review on an open cycle inside its closing window.

    Runs off the same `outstanding_reviews()` the admin "Remind everyone" button uses, so
    the automated nudge and the manual one can never disagree about who is behind. Only
    fires inside the last `review_reminder_days` before a cycle's `closes_at`: a reminder
    three weeks out is noise people learn to ignore.
    """
    if not settings.updates_enabled or settings.review_reminder_days <= 0:
        return
    from datetime import timedelta

    from app.services.cycles import open_cycles, outstanding_reviews
    from app.services.notify import notify_outstanding
    from app.utils import now_utc

    try:
        async with AsyncSessionLocal() as db:
            for cycle in await open_cycles(db):
                if cycle.closes_at is None:
                    continue  # an open-ended cycle has no deadline to remind against
                window = cycle.closes_at - timedelta(days=settings.review_reminder_days)
                if not (window <= now_utc() <= cycle.closes_at):
                    continue
                outstanding = await outstanding_reviews(db, cycle)
                sent = await notify_outstanding(cycle.name, outstanding)
                log.info("Review reminders for %s: %s sent", cycle.name, sent)
    except Exception:  # never let a reminder failure crash the scheduler
        log.exception("Scheduled review reminders failed")


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    """(Re)register all scheduled jobs from the current settings. Uses
    ``replace_existing=True`` so it is safe to call on a running scheduler."""
    bh, bm = settings.backup_time.split(":")
    scheduler.add_job(
        job_nightly_backup,
        CronTrigger(day_of_week=settings.backup_day, hour=int(bh), minute=int(bm), timezone=settings.timezone),
        id="nightly_backup",
        replace_existing=True,
    )

    scheduler.add_job(
        job_legion_sync,
        CronTrigger(minute=0, timezone=settings.timezone),
        id="legion_sync",
        replace_existing=True,
    )

    # Late morning: a reminder that lands mid-day gets acted on, one at 3am gets buried.
    scheduler.add_job(
        job_review_reminders,
        CronTrigger(hour=10, minute=0, timezone=settings.timezone),
        id="review_reminders",
        replace_existing=True,
    )


def reschedule_all(scheduler) -> None:
    if scheduler is None:
        return
    register_jobs(scheduler)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    register_jobs(scheduler)
    return scheduler
