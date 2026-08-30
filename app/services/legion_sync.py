"""
Legion roster sync — pulls the source-of-truth roster from Legion's read-only API and
upserts it into Virtus's local `members` and `subteams` mirrors.

Data flows one way: Legion -> Virtus. Virtus never writes roster data back. Members are
keyed on Legion's stable `member_code`; legacy rows created before a member had a code are
back-linked by `slack_user_id` (unique) then by exact name on first sync. Incremental
syncs pass `updated_since` (the previous sync's start time) so only changed members are
fetched.

Subteams come from a separate `/api/subteams` pull, which has no incremental filter and is
small enough to re-read in full every time. Virtus needs them for team-goal ownership and
for the "assign everyone on subteam X to reviewer Y" bulk action.
"""
import logging
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Member, MemberKind, Subteam
from app.services.app_settings import LEGION_LAST_SYNCED_KEY, get_setting, set_setting

log = logging.getLogger(__name__)


class LegionSyncError(RuntimeError):
    """Raised when the sync can't run (misconfigured or Legion unreachable)."""


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict:
    resp = await client.get(path, params={k: v for k, v in params.items() if v is not None})
    resp.raise_for_status()
    return resp.json()


async def sync_roster(db: AsyncSession, *, full: bool = False) -> str:
    """Pull members from Legion and upsert the local `members` mirror.
    Pass `full=True` to ignore the incremental watermark and re-pull everyone.
    Returns a short human summary. Raises `LegionSyncError` on config/transport failure."""
    if not settings.legion_base_url or not settings.legion_api_key:
        raise LegionSyncError("Legion is not configured (set LEGION_BASE_URL and LEGION_API_KEY).")

    sync_start = datetime.utcnow().isoformat()
    since = None if full else await get_setting(db, LEGION_LAST_SYNCED_KEY)
    headers = {"X-API-Key": settings.legion_api_key}
    try:
        async with httpx.AsyncClient(
            base_url=settings.legion_base_url, headers=headers, timeout=30
        ) as client:
            members = (await _get(client, "/api/members", updated_since=since))["members"]
            subteams = (await _get(client, "/api/subteams"))["subteams"]
    except (httpx.HTTPError, KeyError) as e:
        raise LegionSyncError(f"Legion API request failed: {e}") from e

    subteam_count = await _upsert_subteams(db, subteams)
    count = await _upsert_members(db, members)

    # Watermark = this sync's start; a member changed mid-sync is re-pulled next time (>=).
    await set_setting(db, LEGION_LAST_SYNCED_KEY, sync_start)  # commits
    summary = f"{count} member(s), {subteam_count} subteam(s)"
    log.info("Legion sync complete: %s (since=%s)", summary, since or "full")
    return summary


async def _find_local(db: AsyncSession, member: dict) -> Optional[Member]:
    """Locate the local row for a Legion member: by member_code, else back-link by
    slack_user_id, else by exact (case-insensitive) name."""
    code = member["member_code"]
    row = (await db.execute(select(Member).where(Member.member_code == code))).scalars().first()
    if row:
        return row
    slack_id = member.get("slack_user_id")
    if slack_id:
        row = (await db.execute(
            select(Member).where(Member.slack_user_id == slack_id)
        )).scalars().first()
        if row:
            return row
    return (await db.execute(
        select(Member).where(func.lower(Member.name) == member["name"].lower())
    )).scalars().first()


def _group_slugs(member: dict) -> Optional[str]:
    """Legion's /api/members serializes `groups` as a list of {slug, label} (or bare
    slugs). Flatten to a comma-joined slug string, or None when empty."""
    groups = member.get("groups") or []
    slugs = []
    for g in groups:
        if isinstance(g, dict):
            slug = g.get("slug")
        else:
            slug = g
        if slug:
            slugs.append(slug)
    return ",".join(slugs) if slugs else None


async def _upsert_subteams(db: AsyncSession, subteams: list[dict]) -> int:
    """Mirror Legion's subteam list. Keyed on `slug` (Legion's stable identifier).

    Rows are never deleted here: a subteam retired in Legion still needs its label so an
    older team goal or a closed cycle's grouping keeps rendering as words rather than a
    bare slug. `is_active` is what hides it from the pickers.
    """
    seen = set()
    for s in subteams:
        slug = s.get("slug")
        if not slug:
            continue
        seen.add(slug)
        row = (await db.execute(select(Subteam).where(Subteam.slug == slug))).scalars().first()
        if row is None:
            row = Subteam(slug=slug)
            db.add(row)
        row.label = s.get("label") or slug
        row.is_active = bool(s.get("is_active", True))

    # A subteam that vanished from Legion's list entirely is deactivated, not deleted.
    for row in (await db.execute(select(Subteam))).scalars().all():
        if row.slug not in seen:
            row.is_active = False

    return len(seen)


async def _upsert_members(db: AsyncSession, members: list[dict]) -> int:
    count = 0
    for m in members:
        row = await _find_local(db, m)
        if row is None:
            row = Member(member_code=m["member_code"])
            db.add(row)
        row.member_code = m["member_code"]
        row.name = m["name"]
        row.kind = MemberKind.mentor if m["role"] == "mentor" else MemberKind.student
        row.team_number = m.get("team_number")
        row.slack_user_id = m.get("slack_user_id")
        subteam = m.get("subteam") or {}
        row.subteam_slug = subteam.get("slug")
        row.subteam_label = subteam.get("label")
        row.grade = m.get("grade")
        row.graduation_year = m.get("graduation_year")
        row.group_slugs = _group_slugs(m)
        row.is_active = m["is_active"]
        row.archived_at = None if m["is_active"] else (row.archived_at or datetime.utcnow())
        count += 1

    await db.commit()
    return count
