"""
Runtime-configurable app settings backed by the `app_settings` key/value table.

Holds small cross-cutting values: the Legion roster-sync watermark, and any UI-editable
settings not worth their own column.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting

LEGION_LAST_SYNCED_KEY = "legion_last_synced"


async def get_setting(db: AsyncSession, key: str) -> Optional[str]:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalars().first()
    return row.value if row else None


async def set_setting(db: AsyncSession, key: str, value: Optional[str]) -> None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalars().first()
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await db.commit()
