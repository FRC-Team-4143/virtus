"""
Goals — team goals and student goals.

Two independent lists, deliberately: a student goal has no foreign key to a team goal and
never rolls up into one. See CLAUDE.md "Goals are two separate lists" for why.

Like every domain service in this codebase, nothing here commits — functions `flush()` so
the caller can read back the written row, and the *router* commits alongside its own
`audit.record()` call.
"""
from datetime import date
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import GoalCategory, GoalStatus, GoalTeam, Member, StudentGoal, Subteam, TeamGoal


class GoalError(ValueError):
    """A goal couldn't be created or edited as asked."""


# Team goals sort by category in this fixed order (Robot Performance first), then by the
# admin's explicit ordering, then title. `sorted()` is stable, so the SQL ORDER BY below
# is what breaks ties within a category.
_CATEGORY_ORDER: dict[GoalCategory, int] = {c: i for i, c in enumerate(GoalCategory)}


def current_season() -> str:
    return settings.current_season


# --- shared helpers -------------------------------------------------------------------

def parse_status(raw: Optional[str]) -> GoalStatus:
    """A status string from a form, defaulting to `not_started` for anything unknown."""
    try:
        return GoalStatus(raw)
    except (ValueError, TypeError):
        return GoalStatus.not_started


def parse_category(raw: Optional[str]) -> GoalCategory:
    """A category string from a form, defaulting to Robot Performance for anything unknown."""
    try:
        return GoalCategory(raw)
    except (ValueError, TypeError):
        return GoalCategory.robot_performance


def parse_team(raw: Optional[str]) -> Optional[GoalTeam]:
    """A team string from a form or query param. Returns `None` for blank/unknown so a
    caller can treat that as "no team filter" / "leave unchanged"."""
    try:
        return GoalTeam(raw)
    except (ValueError, TypeError):
        return None


def _clean_title(title: str) -> str:
    title = (title or "").strip()
    if not title:
        raise GoalError("A goal needs a title.")
    return title[:200]


async def list_subteams(db: AsyncSession, *, include_inactive: bool = False) -> Sequence[Subteam]:
    stmt = select(Subteam).order_by(Subteam.label)
    if not include_inactive:
        stmt = stmt.where(Subteam.is_active.is_(True))
    return (await db.execute(stmt)).scalars().all()


async def seasons(db: AsyncSession) -> list[str]:
    """Every season that has goals or is the configured current one, newest label first."""
    rows = set((await db.execute(select(TeamGoal.season).distinct())).scalars().all())
    rows |= set((await db.execute(select(StudentGoal.season).distinct())).scalars().all())
    rows.add(current_season())
    return sorted(rows, reverse=True)


# --- team goals -----------------------------------------------------------------------

async def list_team_goals(
    db: AsyncSession, *, season: Optional[str] = None,
    category: Optional[GoalCategory] = None, team: Optional[GoalTeam] = None,
) -> Sequence[TeamGoal]:
    stmt = select(TeamGoal)
    if season:
        stmt = stmt.where(TeamGoal.season == season)
    if category:
        stmt = stmt.where(TeamGoal.category == category)
    if team:
        stmt = stmt.where(TeamGoal.team == team)
    goals = (await db.execute(
        stmt.order_by(TeamGoal.sort_order, TeamGoal.title)
    )).scalars().all()
    # Category order is fixed (Robot Performance → Award → Learning and Culture); the
    # stable sort keeps the sort_order/title tiebreak from the query above.
    return sorted(goals, key=lambda g: _CATEGORY_ORDER[g.category])


async def group_team_goals_by_category(
    db: AsyncSession, *, season: str, team: Optional[GoalTeam] = None
) -> list[tuple[str, list[TeamGoal]]]:
    """Team goals bucketed for the board view: [(category label, goals), ...] in the fixed
    Robot Performance → Award → Learning and Culture order.

    Returns `[]` when the season (within the team filter) has no goals at all, so the board
    can show its empty state; otherwise every category is present — an empty one included —
    so the board reads as a consistent scorecard.
    """
    goals = await list_team_goals(db, season=season, team=team)
    if not goals:
        return []
    buckets: dict[GoalCategory, list[TeamGoal]] = {c: [] for c in GoalCategory}
    for goal in goals:
        buckets[goal.category].append(goal)
    return [(cat.label, buckets[cat]) for cat in GoalCategory]


async def create_team_goal(
    db: AsyncSession,
    *,
    title: str,
    description: Optional[str] = None,
    season: Optional[str] = None,
    category: GoalCategory = GoalCategory.robot_performance,
    team: GoalTeam = GoalTeam.organization,
    target_date: Optional[date] = None,
    status: GoalStatus = GoalStatus.not_started,
    created_by_code: Optional[str] = None,
) -> TeamGoal:
    season = season or current_season()
    next_order = (await db.execute(
        select(func.coalesce(func.max(TeamGoal.sort_order), 0)).where(TeamGoal.season == season)
    )).scalar_one()
    goal = TeamGoal(
        title=_clean_title(title),
        description=(description or "").strip() or None,
        season=season,
        category=category,
        team=team,
        target_date=target_date,
        status=status,
        sort_order=next_order + 10,
        created_by_code=created_by_code,
    )
    db.add(goal)
    await db.flush()
    return goal


async def update_team_goal(db: AsyncSession, goal: TeamGoal, **fields) -> TeamGoal:
    if "title" in fields:
        goal.title = _clean_title(fields.pop("title"))
    if "description" in fields:
        goal.description = (fields.pop("description") or "").strip() or None
    for key, value in fields.items():
        setattr(goal, key, value)
    await db.flush()
    return goal


# --- student goals --------------------------------------------------------------------

def required_personal_goals() -> int:
    """How many development goals a student must have on file per season (0 = no rule)."""
    return max(0, settings.required_personal_goals)


async def personal_goal_shortfall(
    db: AsyncSession, member_id: int, *, season: Optional[str] = None
) -> int:
    """How many more goals this member still needs for `season` to meet the minimum —
    0 once they're there (or if the requirement is switched off)."""
    need = required_personal_goals()
    if need == 0:
        return 0
    have = len(await list_student_goals(db, member_id, season=season or current_season()))
    return max(0, need - have)


async def list_student_goals(
    db: AsyncSession, member_id: int, *, season: Optional[str] = None
) -> Sequence[StudentGoal]:
    stmt = select(StudentGoal).where(StudentGoal.member_id == member_id)
    if season:
        stmt = stmt.where(StudentGoal.season == season)
    return (await db.execute(
        # Done goals sink to the bottom; the rest keep newest-first.
        stmt.order_by(
            (StudentGoal.status == GoalStatus.done).asc(),
            StudentGoal.target_date.is_(None).asc(),
            StudentGoal.target_date,
            StudentGoal.created_at.desc(),
        )
    )).scalars().all()


async def create_student_goal(
    db: AsyncSession,
    member: Member,
    *,
    title: str,
    description: Optional[str] = None,
    season: Optional[str] = None,
    target_date: Optional[date] = None,
    status: GoalStatus = GoalStatus.not_started,
    created_by_code: Optional[str] = None,
) -> StudentGoal:
    goal = StudentGoal(
        member_id=member.id,
        title=_clean_title(title),
        description=(description or "").strip() or None,
        season=season or current_season(),
        target_date=target_date,
        status=status,
        created_by_code=created_by_code,
    )
    db.add(goal)
    await db.flush()
    return goal


async def update_student_goal(db: AsyncSession, goal: StudentGoal, **fields) -> StudentGoal:
    if "title" in fields:
        goal.title = _clean_title(fields.pop("title"))
    if "description" in fields:
        goal.description = (fields.pop("description") or "").strip() or None
    for key, value in fields.items():
        setattr(goal, key, value)
    await db.flush()
    return goal


async def get_student_goal_for(
    db: AsyncSession, goal_id: int, member_id: int
) -> Optional[StudentGoal]:
    """A student's own goal by id, or None.

    The `member_id` filter is the ownership check itself rather than a separate `if`, so
    there is no path that loads someone else's goal and then decides what to do with it.
    """
    return (await db.execute(
        select(StudentGoal).where(
            StudentGoal.id == goal_id, StudentGoal.member_id == member_id
        )
    )).scalars().first()
