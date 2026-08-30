"""Cycle lifecycle, the competency snapshot, and reviewer assignment."""
import pytest
from sqlalchemy import select

from app.models import (
    Competency, CycleCompetency, CycleStatus, MemberKind, ReviewAssignment,
)
from app.services import cycles as cycle_service


async def test_populate_roster_adds_active_students_only(db, make_member, competencies):
    await make_member("Sara Student")
    await make_member("Mo Mentor", kind=MemberKind.mentor)
    await make_member("Gone Grad", is_active=False)

    cycle = await cycle_service.create_cycle(db, name="Midpoint", season="2026")
    added = await cycle_service.populate_roster(db, cycle)
    await db.commit()

    assert added == 1
    rows = await cycle_service.load_assignments(db, cycle.id)
    assert [a.member.name for a in rows] == ["Sara Student"]


async def test_populate_roster_is_idempotent_and_keeps_reviewers(db, make_member, competencies):
    student = await make_member("Sara Student")
    mentor = await make_member("Mo Mentor", kind=MemberKind.mentor)
    cycle = await cycle_service.create_cycle(db, name="Midpoint", season="2026")
    await cycle_service.populate_roster(db, cycle)
    await db.commit()

    assignment = (await cycle_service.load_assignments(db, cycle.id))[0]
    await cycle_service.set_reviewer(db, assignment, mentor.id)
    await db.commit()

    # A second student joins the roster later.
    await make_member("Later Larry")
    added = await cycle_service.populate_roster(db, cycle)
    await db.commit()

    assert added == 1
    rows = {a.member.name: a for a in await cycle_service.load_assignments(db, cycle.id)}
    assert len(rows) == 2
    # The already-chosen reviewer survived.
    assert rows["Sara Student"].reviewer_member_id == mentor.id


async def test_cannot_open_without_a_roster(db, competencies):
    cycle = await cycle_service.create_cycle(db, name="Empty", season="2026")
    with pytest.raises(cycle_service.CycleError, match="students"):
        await cycle_service.open_cycle(db, cycle)


async def test_cannot_open_without_competencies(db, make_member):
    await make_member("Sara Student")
    cycle = await cycle_service.create_cycle(db, name="No form", season="2026")
    await cycle_service.populate_roster(db, cycle)
    with pytest.raises(cycle_service.CycleError, match="competency"):
        await cycle_service.open_cycle(db, cycle)


async def test_opening_snapshots_competencies(db, make_member, competencies, make_cycle):
    await make_member("Sara Student")
    cycle = await make_cycle("Midpoint")

    snapshot = (await db.execute(
        select(CycleCompetency).where(CycleCompetency.cycle_id == cycle.id)
        .order_by(CycleCompetency.sort_order)
    )).scalars().all()
    assert [c.name for c in snapshot] == [c.name for c in competencies]
    assert cycle.status == CycleStatus.open
    assert cycle.opens_at is not None


async def test_snapshot_survives_renaming_the_master_competency(
    db, make_member, competencies, make_cycle
):
    """The whole point of the snapshot: editing the master list must never rewrite the
    form a finished cycle was answered against."""
    await make_member("Sara Student")
    cycle = await make_cycle("Midpoint")
    original = competencies[0].name

    master = await db.get(Competency, competencies[0].id)
    master.name = "Completely Different Wording"
    master.is_active = False
    await db.commit()

    snapshot = (await db.execute(
        select(CycleCompetency).where(CycleCompetency.cycle_id == cycle.id)
        .order_by(CycleCompetency.sort_order)
    )).scalars().all()
    assert snapshot[0].name == original

    # And a *new* cycle picks up the edit — minus the now-archived one.
    cycle2 = await make_cycle("Later")
    names2 = [c.name for c in (await db.execute(
        select(CycleCompetency).where(CycleCompetency.cycle_id == cycle2.id)
    )).scalars().all()]
    assert original not in names2
    assert "Completely Different Wording" not in names2  # archived, so excluded


async def test_reopen_keeps_the_original_snapshot(db, make_member, competencies, make_cycle):
    await make_member("Sara Student")
    cycle = await make_cycle("Midpoint")
    before = len(cycle.competencies)

    await cycle_service.close_cycle(db, cycle)
    await db.commit()
    assert cycle.status == CycleStatus.closed

    await cycle_service.reopen_cycle(db, cycle)
    await db.commit()

    after = (await db.execute(
        select(CycleCompetency).where(CycleCompetency.cycle_id == cycle.id)
    )).scalars().all()
    assert cycle.status == CycleStatus.open
    assert len(after) == before  # not re-snapshotted on top of the old rows


async def test_a_student_cannot_review_themselves(db, make_member, competencies):
    student = await make_member("Sara Student")
    cycle = await cycle_service.create_cycle(db, name="Midpoint", season="2026")
    await cycle_service.populate_roster(db, cycle)
    await db.commit()
    assignment = (await cycle_service.load_assignments(db, cycle.id))[0]

    with pytest.raises(cycle_service.CycleError, match="own reviewer"):
        await cycle_service.set_reviewer(db, assignment, student.id)


async def test_bulk_assign_skips_already_assigned(db, make_member, competencies):
    a = await make_member("Design Dana", subteam_slug="design", subteam_label="Design")
    b = await make_member("Design Dev", subteam_slug="design", subteam_label="Design")
    await make_member("Software Sam", subteam_slug="software", subteam_label="Software")
    lead = await make_member("Lead Lee", kind=MemberKind.mentor)
    other = await make_member("Other Oli", kind=MemberKind.mentor)

    cycle = await cycle_service.create_cycle(db, name="Midpoint", season="2026")
    await cycle_service.populate_roster(db, cycle)
    await db.commit()

    rows = {x.member.name: x for x in await cycle_service.load_assignments(db, cycle.id)}
    await cycle_service.set_reviewer(db, rows["Design Dana"], other.id)
    await db.commit()

    count = await cycle_service.bulk_assign_subteam(
        db, cycle, subteam_slug="design", reviewer_id=lead.id
    )
    await db.commit()

    rows = {x.member.name: x for x in await cycle_service.load_assignments(db, cycle.id)}
    assert count == 1
    assert rows["Design Dana"].reviewer_member_id == other.id   # untouched
    assert rows["Design Dev"].reviewer_member_id == lead.id     # filled in
    assert rows["Software Sam"].reviewer_member_id is None      # different subteam


async def test_completion_counts(db, make_member, competencies, make_cycle):
    await make_member("Sara Student")
    await make_member("Sam Student")
    mentor = await make_member("Mo Mentor", kind=MemberKind.mentor)
    cycle = await make_cycle("Midpoint")

    assignments = await cycle_service.load_assignments(db, cycle.id)
    await cycle_service.set_reviewer(db, assignments[0], mentor.id)
    await db.commit()

    stats = cycle_service.completion(await cycle_service.load_assignments(db, cycle.id))
    assert stats["total"] == 2
    assert stats["unassigned"] == 1
    assert stats["both_done"] == 0
    assert stats["percent"] == 0
