"""
Timezone helpers shared across the app. All datetimes in the database are stored as naive
UTC; these convert to/from the configured local timezone (default: America/New_York).
"""
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/New_York")


_UTC = ZoneInfo("UTC")


def utc_to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a naive UTC datetime to a naive local datetime."""
    if dt is None:
        return None
    return dt.replace(tzinfo=_UTC).astimezone(_tz()).replace(tzinfo=None)


def now_utc() -> datetime:
    """Current moment as a naive UTC datetime (matches how the DB stores times)."""
    return datetime.utcnow()
