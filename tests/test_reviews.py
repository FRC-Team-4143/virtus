"""
Review authorization, draft privacy, and the submit rules.

The authorization tests are the important ones: a reviewer's right to write comes from the
`ReviewAssignment`, never from a Legion group, which is what lets a student subteam lead
review their members with no admin access.
"""
import pytest

from app.models import CycleStatus, MemberKind, ReviewKind, ReviewStatus, SCORE_LABELS
from app.services import cycles as cycle_service, reviews as review_service


async def _assignment_for(db, cycle, member, reviewer=None):
    assignment = await cycle_service.get_assignment_for_member(db, cycle.id, member.id)
    if reviewer is not None:
        await cycle_service.set_reviewer(db, assignment, reviewer.id)
        await db.commit()
        assignment = await cycle_service.get_assignment_for_member(db, cycle.id, member.id)
    return assignment


# ── can_write ────────────────────────────────────────────────────────────────────

async def test_only_the_student_may_write_their_self_review(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    other = await make_member("Sam Student")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student)

    assert review_service.can_write(assignment, ReviewKind.self_review, student)
    assert not review_service.can_write(assignment, ReviewKind.self_review, other)
    assert not review_service.can_write(assignment, ReviewKind.self_review, None)


async def test_a_group_less_student_lead_may_write_their_assigned_review(
    db, make_member, competencies, make_cycle
):
    """The design's whole point — no `virtus-*` group anywhere in this test."""
    student = await make_member("Sara Student", subteam_slug="design")
    lead = await make_member("Lena Lead", subteam_slug="design")  # a student, no groups
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student, reviewer=lead)

    assert lead.kind == MemberKind.student
    assert lead.group_slugs is None
    assert review_service.can_write(assignment, ReviewKind.reviewer, lead)


async def test_an_unassigned_person_may_not_write_the_reviewer_review(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    bystander = await make_member("Bo Bystander", kind=MemberKind.mentor)
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student, reviewer=lead)

    assert not review_service.can_write(assignment, ReviewKind.reviewer, bystander)
    # ...including the student themselves.
    assert not review_service.can_write(assignment, ReviewKind.reviewer, student)


async def test_nobody_may_write_once_the_cycle_is_closed(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student, reviewer=lead)

    await cycle_service.close_cycle(db, cycle)
    await db.commit()
    assignment = await cycle_service.get_assignment_for_member(db, cycle.id, student.id)

    assert not review_service.can_write(assignment, ReviewKind.self_review, student)
    assert not review_service.can_write(assignment, ReviewKind.reviewer, lead)


# ── can_read ─────────────────────────────────────────────────────────────────────

async def test_a_draft_is_private_to_its_author_including_from_the_student(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student, reviewer=lead)

    review = await review_service.get_or_create(db, assignment, ReviewKind.reviewer, lead)
    await db.commit()

    assert review.status == ReviewStatus.draft
    assert review_service.can_read(assignment, review, lead)                    # author
    assert not review_service.can_read(assignment, review, student)             # subject
    assert not review_service.can_read(assignment, review, None, is_staff=True)  # staff


async def test_a_submitted_review_is_visible_to_the_student_and_staff(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    outsider = await make_member("Nosy Nick")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student, reviewer=lead)

    review = await review_service.get_or_create(db, assignment, ReviewKind.reviewer, lead)
    ratings = await review_service.load_ratings(db, review)
    await review_service.save(
        db, review, strengths="Great work",
        scores={r.cycle_competency_id: 3 for r in ratings}, submit=True,
    )
    await db.commit()

    assert review.status == ReviewStatus.submitted
    assert review_service.can_read(assignment, review, student)
    assert review_service.can_read(assignment, review, outsider, is_staff=True)
    assert not review_service.can_read(assignment, review, outsider)


# ── private notes ────────────────────────────────────────────────────────────────

async def test_private_notes_are_hidden_from_the_subject_even_as_staff(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    boss = await make_member("Val Verywise", kind=MemberKind.mentor)
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student, reviewer=lead)

    review = await review_service.get_or_create(db, assignment, ReviewKind.reviewer, lead)
    ratings = await review_service.load_ratings(db, review)
    await review_service.save(
        db, review, strengths="Solid contributor",
        private_notes="Considering them for lead next year — don't say yet.",
        scores={r.cycle_competency_id: 3 for r in ratings}, submit=True,
    )
    await db.commit()

    assert review.private_notes.startswith("Considering them for lead")
    # The reviewer who wrote them, and any staff above the reviewer, can read them.
    assert review_service.can_read_private_notes(assignment, review, lead)
    assert review_service.can_read_private_notes(assignment, review, boss, is_staff=True)
    # The subject never can — not as a plain student, and not even holding a staff group.
    assert not review_service.can_read_private_notes(assignment, review, student)
    assert not review_service.can_read_private_notes(assignment, review, student, is_staff=True)
    # ...even though they *can* read the review itself once it's submitted.
    assert review_service.can_read(assignment, review, student)


async def test_a_self_review_never_carries_private_notes(
    db, make_member, competencies, make_cycle
):
    """Belt and braces: even if `save()` is handed a value, the field is meaningless on a
    self-review — its author is the subject, so `can_read_private_notes` is False for the
    only person who could ever see it."""
    student = await make_member("Sara Student")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student)
    review = await review_service.get_or_create(db, assignment, ReviewKind.self_review, student)

    await review_service.save(db, review, private_notes="whatever")
    await db.commit()
    assert not review_service.can_read_private_notes(assignment, review, student, is_staff=True)


async def test_private_notes_survive_submission_like_the_other_prose(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student, reviewer=lead)
    review = await review_service.get_or_create(db, assignment, ReviewKind.reviewer, lead)
    ratings = await review_service.load_ratings(db, review)

    await review_service.save(db, review, private_notes="draft thought")
    await db.commit()
    assert review.private_notes == "draft thought"

    await review_service.save(
        db, review, private_notes="final thought",
        scores={r.cycle_competency_id: 3 for r in ratings}, submit=True,
    )
    await db.commit()
    assert review.status == ReviewStatus.submitted
    assert review.private_notes == "final thought"


# ── writing ──────────────────────────────────────────────────────────────────────

async def test_get_or_create_makes_one_rating_per_frozen_competency(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student)

    review = await review_service.get_or_create(db, assignment, ReviewKind.self_review, student)
    await db.commit()
    ratings = await review_service.load_ratings(db, review)

    assert len(ratings) == len(competencies)
    assert all(r.score is None for r in ratings)
    # Calling again returns the same review, not a second one.
    again = await review_service.get_or_create(db, assignment, ReviewKind.self_review, student)
    assert again.id == review.id


async def test_submit_requires_every_competency_rated(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student)
    review = await review_service.get_or_create(db, assignment, ReviewKind.self_review, student)
    ratings = await review_service.load_ratings(db, review)

    partial = {ratings[0].cycle_competency_id: 3}
    with pytest.raises(review_service.ReviewError, match=ratings[1].competency.name):
        await review_service.save(db, review, scores=partial, submit=True)
    assert review.status == ReviewStatus.draft

    # ...but saving a draft with the same partial answers is fine.
    await review_service.save(db, review, scores=partial)
    await db.commit()
    assert review.status == ReviewStatus.draft
    assert (await review_service.load_ratings(db, review))[0].score == 3


async def test_the_scale_is_the_five_point_rainbow(db):
    assert list(SCORE_LABELS) == [1, 2, 3, 4, 5]
    assert SCORE_LABELS[1] == "Needs support"
    assert SCORE_LABELS[5] == "Exceptional"


async def test_an_out_of_range_score_is_treated_as_unanswered(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student)
    review = await review_service.get_or_create(db, assignment, ReviewKind.self_review, student)
    ratings = await review_service.load_ratings(db, review)

    await review_service.save(db, review, scores={
        ratings[0].cycle_competency_id: "5",    # top of the new scale — valid
        ratings[1].cycle_competency_id: "6",    # one past it — dropped
        ratings[2].cycle_competency_id: "99",
    })
    await db.commit()
    scored = await review_service.load_ratings(db, review)
    assert scored[0].score == 5
    assert scored[1].score is None
    assert scored[2].score is None


async def test_a_submitted_review_cannot_be_edited(db, make_member, competencies, make_cycle):
    student = await make_member("Sara Student")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student)
    review = await review_service.get_or_create(db, assignment, ReviewKind.self_review, student)
    ratings = await review_service.load_ratings(db, review)
    await review_service.save(
        db, review, strengths="Original",
        scores={r.cycle_competency_id: 4 for r in ratings}, submit=True,
    )
    await db.commit()

    with pytest.raises(review_service.ReviewError, match="already been submitted"):
        await review_service.save(db, review, strengths="Sneaky edit")
    assert review.strengths == "Original"

    # An admin can reopen it, and then it's editable again.
    await review_service.unsubmit(db, review)
    await db.commit()
    await review_service.save(db, review, strengths="Corrected")
    await db.commit()
    assert review.status == ReviewStatus.draft
    assert review.strengths == "Corrected"


# ── side by side ─────────────────────────────────────────────────────────────────

async def test_comparison_shows_both_sides_and_the_gap(
    db, make_member, competencies, make_cycle
):
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student, reviewer=lead)

    self_review = await review_service.get_or_create(db, assignment, ReviewKind.self_review, student)
    ratings = await review_service.load_ratings(db, self_review)
    await review_service.save(
        db, self_review, scores={r.cycle_competency_id: 2 for r in ratings}, submit=True
    )
    reviewer_review = await review_service.get_or_create(db, assignment, ReviewKind.reviewer, lead)
    r_ratings = await review_service.load_ratings(db, reviewer_review)
    await review_service.save(
        db, reviewer_review, scores={r.cycle_competency_id: 4 for r in r_ratings}, submit=True
    )
    await db.commit()

    rows = await review_service.comparison(db, assignment)
    assert len(rows) == len(competencies)
    assert all(r["self_score"] == 2 for r in rows)
    assert all(r["reviewer_score"] == 4 for r in rows)
    assert all(r["delta"] == 2 for r in rows)


async def test_comparison_hides_an_unsubmitted_side(db, make_member, competencies, make_cycle):
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    cycle = await make_cycle()
    assignment = await _assignment_for(db, cycle, student, reviewer=lead)

    draft = await review_service.get_or_create(db, assignment, ReviewKind.reviewer, lead)
    ratings = await review_service.load_ratings(db, draft)
    await review_service.save(db, draft, scores={r.cycle_competency_id: 1 for r in ratings})
    await db.commit()

    rows = await review_service.comparison(db, assignment)
    assert all(r["reviewer_score"] is None for r in rows)
    assert all(r["delta"] is None for r in rows)
