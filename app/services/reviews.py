"""
Reviews — the self-review and the assigned reviewer's official review, plus the rules for
who may read or write each one.

Authorization lives here rather than in the routers because a reviewer's right to write is
**not** a Legion group: it comes from being named on the `ReviewAssignment`. That's what
lets a student subteam lead review their members without any `/admin` access at all. Both
the portal routes and the admin routes funnel through the same two predicates so they
can't drift apart.

Nothing here commits — services `flush()`, the router commits alongside `audit.record()`.
"""
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import (
    CycleStatus, Member, Review, ReviewAssignment, ReviewKind, ReviewRating, ReviewStatus,
    SCORE_LABELS,
)
from app.utils import now_utc


class ReviewError(ValueError):
    """A review couldn't be saved or submitted as asked."""


# --- authorization --------------------------------------------------------------------

def can_write(assignment: ReviewAssignment, kind: ReviewKind, member: Optional[Member]) -> bool:
    """May `member` write this review right now?

    The self-review belongs to the student; the official review belongs to whoever is
    named as reviewer. Staff are deliberately *not* granted write access here — an admin
    who needs to write a review assigns it to themselves first, so the authorship on the
    record always matches who was actually responsible for it.
    """
    if member is None or assignment.cycle.status != CycleStatus.open:
        return False
    if kind == ReviewKind.self_review:
        return assignment.member_id == member.id
    return assignment.reviewer_member_id == member.id


def can_read(
    assignment: ReviewAssignment,
    review: Optional[Review],
    member: Optional[Member],
    *,
    is_staff: bool = False,
) -> bool:
    """May this person read the review?

    A **draft** is private to its author — including from the student it's about, so
    nobody reads a half-written assessment of themselves. Once **submitted** it opens up
    to the subject student and to all staff, which is the visibility model the rest of the
    app assumes.
    """
    if review is None:
        return False
    if member is not None and review.author_member_id == member.id:
        return True
    if review.status != ReviewStatus.submitted:
        return False
    if is_staff:
        return True
    return member is not None and assignment.member_id == member.id


def can_read_private_notes(
    assignment: ReviewAssignment,
    review: Optional[Review],
    member: Optional[Member],
    *,
    is_staff: bool = False,
) -> bool:
    """May this person read the reviewer's **private notes** on `review`?

    Stricter than `can_read`, and the subject-exclusion is the whole point: the person the
    review is *about* never sees these notes — not once the review is submitted, and not
    even if they themselves hold a staff group. They exist for the reviewer who wrote them
    and for staff standing above that reviewer: somewhere to record something that is
    deliberately not part of the feedback the student gets back.
    """
    if review is None or member is None:
        return False
    if assignment.member_id == member.id:
        return False
    if review.author_member_id == member.id:
        return True
    return is_staff


# --- fetching / creating --------------------------------------------------------------

async def get_or_create(
    db: AsyncSession, assignment: ReviewAssignment, kind: ReviewKind, author: Member
) -> Review:
    """The draft to edit, created on first open along with one blank rating row per
    competency in the cycle's frozen snapshot."""
    existing = assignment.review_of(kind)
    if existing is not None:
        return existing

    review = Review(
        assignment_id=assignment.id,
        kind=kind,
        author_member_id=author.id,
        status=ReviewStatus.draft,
    )
    db.add(review)
    await db.flush()
    for cc in assignment.cycle.competencies:
        db.add(ReviewRating(review_id=review.id, cycle_competency_id=cc.id))
    await db.flush()
    await db.refresh(review, ["ratings"])
    assignment.reviews.append(review)
    return review


async def load_ratings(db: AsyncSession, review: Review) -> Sequence[ReviewRating]:
    """A review's ratings in the cycle's competency order."""
    rows = (await db.execute(
        select(ReviewRating)
        .where(ReviewRating.review_id == review.id)
        .options(selectinload(ReviewRating.competency))
    )).scalars().all()
    return sorted(rows, key=lambda r: r.competency.sort_order)


# --- writing --------------------------------------------------------------------------

def _clean_score(raw) -> Optional[int]:
    """A 1-5 score from a form field. Anything outside the scale becomes None (unanswered)
    rather than an error — the submit check is what insists every question is answered."""
    try:
        score = int(raw)
    except (TypeError, ValueError):
        return None
    return score if score in SCORE_LABELS else None


async def save(
    db: AsyncSession,
    review: Review,
    *,
    strengths: Optional[str] = None,
    growth_areas: Optional[str] = None,
    overall_comment: Optional[str] = None,
    private_notes: Optional[str] = None,
    scores: Optional[dict[int, object]] = None,
    comments: Optional[dict[int, str]] = None,
    self_goal_shortfall: int = 0,
    submit: bool = False,
) -> Review:
    """Write the form back, optionally submitting it.

    `scores`/`comments` are keyed by `cycle_competency_id`. A submitted review is frozen:
    saving one again is refused rather than silently ignored, so a stale browser tab can't
    quietly overwrite a review someone already stood behind.

    `self_goal_shortfall` (self-reviews only, computed by the caller) blocks a submit while
    the student is under the personal-goal minimum — the review is meant to be read next to
    that list. Checked after the "every competency rated" gate, so "fill in the form" is
    the first thing they're told.
    """
    if review.status == ReviewStatus.submitted:
        raise ReviewError("This review has already been submitted and can no longer be edited.")

    review.strengths = (strengths or "").strip() or None
    review.growth_areas = (growth_areas or "").strip() or None
    review.overall_comment = (overall_comment or "").strip() or None
    review.private_notes = (private_notes or "").strip() or None

    ratings = await load_ratings(db, review)
    for rating in ratings:
        if scores is not None and rating.cycle_competency_id in scores:
            rating.score = _clean_score(scores[rating.cycle_competency_id])
        if comments is not None and rating.cycle_competency_id in comments:
            rating.comment = (comments[rating.cycle_competency_id] or "").strip() or None

    if submit:
        missing = [r.competency.name for r in ratings if r.score is None]
        if missing:
            raise ReviewError("Rate every competency before submitting: " + ", ".join(missing))
        if review.kind == ReviewKind.self_review and self_goal_shortfall > 0:
            need = settings.required_personal_goals
            raise ReviewError(
                f"Set at least {need} personal goals for this season before submitting your "
                f"self-review — you're {self_goal_shortfall} short. Add them under “My Goals”, "
                f"then come back and submit."
            )
        review.status = ReviewStatus.submitted
        review.submitted_at = now_utc()

    await db.flush()
    return review


async def unsubmit(db: AsyncSession, review: Review) -> Review:
    """Reopen a submitted review for editing (admin correction path).

    Kept admin-only and audited: the point of submission is that a student can trust what
    they read won't change underneath them, so undoing it is a deliberate act, not a
    convenience the author gets on their own.
    """
    review.status = ReviewStatus.draft
    review.submitted_at = None
    await db.flush()
    return review


# --- side-by-side ---------------------------------------------------------------------

async def comparison(db: AsyncSession, assignment: ReviewAssignment) -> list[dict]:
    """Rows for the self-vs-reviewer view: one per competency, with both scores.

    Built off the cycle's frozen competency list rather than either review's ratings, so a
    competency both sides skipped still shows up as an empty row instead of vanishing.
    """
    self_review = assignment.review_of(ReviewKind.self_review)
    reviewer_review = assignment.review_of(ReviewKind.reviewer)

    async def _by_competency(review: Optional[Review]) -> dict[int, ReviewRating]:
        if review is None or review.status != ReviewStatus.submitted:
            return {}
        return {r.cycle_competency_id: r for r in await load_ratings(db, review)}

    self_map = await _by_competency(self_review)
    reviewer_map = await _by_competency(reviewer_review)

    rows = []
    for cc in sorted(assignment.cycle.competencies, key=lambda c: c.sort_order):
        s = self_map.get(cc.id)
        r = reviewer_map.get(cc.id)
        rows.append({
            "competency": cc,
            "self_score": s.score if s else None,
            "self_comment": s.comment if s else None,
            "reviewer_score": r.score if r else None,
            "reviewer_comment": r.comment if r else None,
            # The gap is what makes the side-by-side worth reading: a student who rates
            # themselves two points below their reviewer is the conversation to have.
            "delta": (r.score - s.score) if (s and r and s.score and r.score) else None,
        })
    return rows
