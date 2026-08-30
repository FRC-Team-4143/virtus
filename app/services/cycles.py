"""
Review cycles — the admin-opened container a round of reviews lives in.

Lifecycle: `draft` -> `open` -> `closed` (and `closed` -> `open` to reopen).

  draft   admins build the roster and pick reviewers; nothing is visible to members.
  open    the competency set is frozen onto the cycle, and reviews become writable.
  closed  reviews are locked read-only but stay visible.

Nothing here commits — services `flush()`, the router commits alongside `audit.record()`.
"""
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Competency, CycleCompetency, Member, MemberKind, Review, ReviewAssignment,
    ReviewCycle, ReviewKind, ReviewStatus, CycleStatus,
)
from app.utils import now_utc


class CycleError(ValueError):
    """A cycle couldn't be moved to the requested state."""


async def get_cycle(db: AsyncSession, cycle_id: int) -> Optional[ReviewCycle]:
    return (await db.execute(
        select(ReviewCycle)
        .where(ReviewCycle.id == cycle_id)
        .options(selectinload(ReviewCycle.competencies))
    )).scalars().first()


async def list_cycles(db: AsyncSession, *, season: Optional[str] = None) -> Sequence[ReviewCycle]:
    stmt = select(ReviewCycle)
    if season:
        stmt = stmt.where(ReviewCycle.season == season)
    return (await db.execute(stmt.order_by(ReviewCycle.created_at.desc()))).scalars().all()


async def open_cycles(db: AsyncSession) -> Sequence[ReviewCycle]:
    return (await db.execute(
        select(ReviewCycle)
        .where(ReviewCycle.status == CycleStatus.open)
        .order_by(ReviewCycle.created_at.desc())
    )).scalars().all()


async def load_assignments(
    db: AsyncSession, cycle_id: int, *, reviewer_id: Optional[int] = None
) -> Sequence[ReviewAssignment]:
    """A cycle's assignments with member, reviewer, and reviews eagerly loaded.

    Everything that renders an assignment needs all three, and lazy loading them would be
    a per-row round trip under async SQLAlchemy (which raises rather than blocking).
    """
    stmt = (
        select(ReviewAssignment)
        .where(ReviewAssignment.cycle_id == cycle_id)
        .options(
            selectinload(ReviewAssignment.member),
            selectinload(ReviewAssignment.reviewer),
            selectinload(ReviewAssignment.reviews),
        )
    )
    if reviewer_id is not None:
        stmt = stmt.where(ReviewAssignment.reviewer_member_id == reviewer_id)
    rows = (await db.execute(stmt)).scalars().all()
    return sorted(rows, key=lambda a: ((a.subteam_label or "~"), a.member.name.lower()))


async def get_assignment(db: AsyncSession, assignment_id: int) -> Optional[ReviewAssignment]:
    return (await db.execute(
        select(ReviewAssignment)
        .where(ReviewAssignment.id == assignment_id)
        .options(
            selectinload(ReviewAssignment.member),
            selectinload(ReviewAssignment.reviewer),
            selectinload(ReviewAssignment.reviews),
            selectinload(ReviewAssignment.cycle).selectinload(ReviewCycle.competencies),
        )
    )).scalars().first()


async def get_assignment_for_member(
    db: AsyncSession, cycle_id: int, member_id: int
) -> Optional[ReviewAssignment]:
    row = (await db.execute(
        select(ReviewAssignment.id).where(
            ReviewAssignment.cycle_id == cycle_id, ReviewAssignment.member_id == member_id
        )
    )).scalars().first()
    return await get_assignment(db, row) if row else None


async def assignments_for_reviewer(
    db: AsyncSession, reviewer_id: int, *, statuses: Sequence[CycleStatus] = (CycleStatus.open,)
) -> Sequence[ReviewAssignment]:
    """Every assignment this person owes a review on — the "Reviews I owe" list.

    Only reaches into cycles in the given statuses so a reviewer isn't shown work for a
    draft cycle that hasn't been announced yet.
    """
    rows = (await db.execute(
        select(ReviewAssignment)
        .join(ReviewCycle, ReviewAssignment.cycle_id == ReviewCycle.id)
        .where(
            ReviewAssignment.reviewer_member_id == reviewer_id,
            ReviewCycle.status.in_(list(statuses)),
        )
        .options(
            selectinload(ReviewAssignment.member),
            selectinload(ReviewAssignment.reviews),
            selectinload(ReviewAssignment.cycle),
        )
    )).scalars().all()
    return sorted(rows, key=lambda a: a.member.name.lower())


# --- building a cycle -----------------------------------------------------------------

async def create_cycle(
    db: AsyncSession, *, name: str, season: str, opens_at=None, closes_at=None
) -> ReviewCycle:
    name = (name or "").strip()
    if not name:
        raise CycleError("A cycle needs a name.")
    cycle = ReviewCycle(
        name=name[:200], season=season, opens_at=opens_at, closes_at=closes_at,
        status=CycleStatus.draft,
    )
    db.add(cycle)
    await db.flush()
    return cycle


async def populate_roster(db: AsyncSession, cycle: ReviewCycle) -> int:
    """Give every active student a slot in the cycle. Idempotent — re-running after new
    students sync in adds only the missing ones and never disturbs existing assignments
    (which may already carry a chosen reviewer)."""
    if cycle.status == CycleStatus.closed:
        raise CycleError("A closed cycle's roster can't be changed.")

    existing = set((await db.execute(
        select(ReviewAssignment.member_id).where(ReviewAssignment.cycle_id == cycle.id)
    )).scalars().all())
    students = (await db.execute(
        select(Member).where(
            Member.kind == MemberKind.student, Member.is_active.is_(True)
        )
    )).scalars().all()

    added = 0
    for student in students:
        if student.id in existing:
            continue
        db.add(ReviewAssignment(
            cycle_id=cycle.id,
            member_id=student.id,
            subteam_slug=student.subteam_slug,
            subteam_label=student.subteam_label,
        ))
        added += 1
    await db.flush()
    return added


async def set_reviewer(
    db: AsyncSession, assignment: ReviewAssignment, reviewer_id: Optional[int]
) -> ReviewAssignment:
    """Point an assignment at a reviewer (or clear it with None).

    Reassigning does **not** delete a reviewer review already in progress — the draft
    stays attached to the assignment, so a handover keeps whatever was already written.
    """
    if assignment.cycle.status == CycleStatus.closed:
        raise CycleError("A closed cycle's reviewers can't be changed.")
    if reviewer_id is not None and reviewer_id == assignment.member_id:
        raise CycleError("A student can't be their own reviewer.")
    assignment.reviewer_member_id = reviewer_id
    await db.flush()
    return assignment


async def bulk_assign_subteam(
    db: AsyncSession, cycle: ReviewCycle, *, subteam_slug: Optional[str], reviewer_id: int
) -> int:
    """Point every unassigned slot on one subteam at a reviewer.

    Deliberately skips slots that already have a reviewer, so running it for a second
    subteam (or re-running it after a manual tweak) can't stomp earlier choices. Pass
    `subteam_slug=None` for the students Legion has on no subteam.
    """
    assignments = await load_assignments(db, cycle.id)
    count = 0
    for a in assignments:
        if a.subteam_slug != subteam_slug or a.reviewer_member_id is not None:
            continue
        if a.member_id == reviewer_id:
            continue  # never make someone their own reviewer
        a.reviewer_member_id = reviewer_id
        count += 1
    await db.flush()
    return count


# --- lifecycle ------------------------------------------------------------------------

async def open_cycle(db: AsyncSession, cycle: ReviewCycle) -> ReviewCycle:
    """Move a draft cycle to open, freezing the competency list onto it.

    The snapshot is the whole point: from here on the form is fixed, so renaming or
    retiring a master `Competency` can't rewrite this cycle's questions, and the self and
    reviewer forms are guaranteed identical.
    """
    if cycle.status != CycleStatus.draft:
        raise CycleError("Only a draft cycle can be opened.")

    active = (await db.execute(
        select(Competency).where(Competency.is_active.is_(True)).order_by(Competency.sort_order)
    )).scalars().all()
    if not active:
        raise CycleError("Add at least one competency before opening a cycle.")

    has_roster = (await db.execute(
        select(func.count()).select_from(ReviewAssignment)
        .where(ReviewAssignment.cycle_id == cycle.id)
    )).scalar_one()
    if not has_roster:
        raise CycleError("Add students to the cycle before opening it.")

    for i, c in enumerate(active):
        db.add(CycleCompetency(
            cycle_id=cycle.id, competency_id=c.id, name=c.name,
            description=c.description, sort_order=i * 10,
        ))
    cycle.status = CycleStatus.open
    cycle.opens_at = cycle.opens_at or now_utc()
    await db.flush()
    # The snapshot rows were added by FK, not through `cycle.competencies`, so the loaded
    # collection is stale — and on a persistent object touching it would fire a lazy load,
    # which raises under async SQLAlchemy. Refresh it here so every caller (and the review
    # form, which reads `assignment.cycle.competencies`) sees the frozen list immediately.
    await db.refresh(cycle, ["competencies"])
    return cycle


async def close_cycle(db: AsyncSession, cycle: ReviewCycle) -> ReviewCycle:
    if cycle.status != CycleStatus.open:
        raise CycleError("Only an open cycle can be closed.")
    cycle.status = CycleStatus.closed
    cycle.closes_at = cycle.closes_at or now_utc()
    await db.flush()
    return cycle


async def reopen_cycle(db: AsyncSession, cycle: ReviewCycle) -> ReviewCycle:
    """Reopen a closed cycle so a late review can still be written.

    Keeps the original competency snapshot — re-snapshotting would change the form out
    from under the reviews already submitted against it.
    """
    if cycle.status != CycleStatus.closed:
        raise CycleError("Only a closed cycle can be reopened.")
    cycle.status = CycleStatus.open
    await db.flush()
    return cycle


# --- progress -------------------------------------------------------------------------

def completion(assignments: Sequence[ReviewAssignment]) -> dict:
    """Counts for the completion dashboard. Pure — takes rows already loaded by
    `load_assignments` so the caller does one query, not three."""
    total = len(assignments)
    self_done = reviewer_done = both_done = unassigned = 0
    for a in assignments:
        s = a.review_of(ReviewKind.self_review)
        r = a.review_of(ReviewKind.reviewer)
        s_ok = bool(s and s.status == ReviewStatus.submitted)
        r_ok = bool(r and r.status == ReviewStatus.submitted)
        self_done += s_ok
        reviewer_done += r_ok
        both_done += s_ok and r_ok
        unassigned += a.reviewer_member_id is None
    return {
        "total": total,
        "self_done": self_done,
        "reviewer_done": reviewer_done,
        "both_done": both_done,
        "unassigned": unassigned,
        "percent": round(100 * both_done / total) if total else 0,
    }


async def outstanding_reviews(db: AsyncSession, cycle: ReviewCycle) -> list[tuple[Member, str]]:
    """Everyone who still owes something on this cycle: [(person, what), ...].

    Used by both the "remind everyone" button and the scheduled reminder job, so the two
    can never disagree about who is behind.
    """
    out: list[tuple[Member, str]] = []
    for a in await load_assignments(db, cycle.id):
        s = a.review_of(ReviewKind.self_review)
        if not (s and s.status == ReviewStatus.submitted):
            out.append((a.member, "your self-review"))
        r = a.review_of(ReviewKind.reviewer)
        if a.reviewer and not (r and r.status == ReviewStatus.submitted):
            out.append((a.reviewer, f"your review of {a.member.name}"))
    return out
