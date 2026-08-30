import enum
from datetime import date, datetime
from typing import Optional, List

from sqlalchemy import (
    Integer, String, Boolean, Date, DateTime, Text,
    ForeignKey, UniqueConstraint, Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MemberKind(str, enum.Enum):
    """Mirrors Legion's `role`. Both kinds are synced and can sign in. Students are the
    ones who hold goals and get reviewed; mentors are reviewers and staff."""
    student = "student"
    mentor = "mentor"


class GoalStatus(str, enum.Enum):
    """Shared by team goals and student goals — one vocabulary so a status chip means the
    same thing everywhere in the app."""
    not_started = "not_started"
    on_track = "on_track"
    at_risk = "at_risk"
    done = "done"


class GoalCategory(str, enum.Enum):
    """The three fixed buckets every team goal falls in. Replaces the old "group team
    goals by their owning Legion subteam" model — this is a small fixed taxonomy, not
    roster data. Defined in display order (Robot Performance first)."""
    robot_performance = "robot_performance"
    award = "award"
    learning_and_culture = "learning_and_culture"

    @property
    def label(self) -> str:
        return {
            GoalCategory.robot_performance: "Robot Performance",
            GoalCategory.award: "Award",
            GoalCategory.learning_and_culture: "Learning and Culture",
        }[self]


class GoalTeam(str, enum.Enum):
    """Which team a goal belongs to. `organization` = the whole program / both teams at
    once, not a third team. Stored by value (`"4143"`), so `SAEnum` needs
    `values_callable` — the member *names* can't start with a digit."""
    team_4143 = "4143"
    team_4423 = "4423"
    organization = "organization"

    @property
    def label(self) -> str:
        return {
            GoalTeam.team_4143: "4143",
            GoalTeam.team_4423: "4423",
            GoalTeam.organization: "Organization",
        }[self]


class CycleStatus(str, enum.Enum):
    draft = "draft"      # admin is building the roster/assignments; invisible to members
    open = "open"        # reviews are writable; competency set is frozen
    closed = "closed"    # reviews are locked read-only, still visible


class ReviewKind(str, enum.Enum):
    self_review = "self"
    reviewer = "reviewer"


class ReviewStatus(str, enum.Enum):
    draft = "draft"          # private to its author
    submitted = "submitted"  # visible to the subject student and all staff


# The rated dimensions a brand-new install starts with. Admins edit/extend this list at
# /admin/competencies; it is the only seed data in the app.
DEFAULT_COMPETENCIES: list[tuple[str, str]] = [
    ("Attendance & Reliability", "Shows up consistently and follows through on commitments."),
    ("Technical Growth", "Builds skill in their subteam's craft and takes on harder work over time."),
    ("Teamwork & Communication", "Works well with others, shares context, asks for and offers help."),
    ("Initiative", "Finds work that needs doing without being told, and owns it to completion."),
    ("Leadership", "Helps others get better; sets the tone for how the team works."),
]


class AppSetting(Base):
    """Small key/value store for runtime-configurable app settings."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Subteam(Base):
    """Mirror of Legion's subteams (`GET /api/subteams`). Read-only, like `Member` —
    Virtus never writes roster data back. Gives team goals a real owner and powers the
    "assign everyone on subteam X to reviewer Y" bulk action."""
    __tablename__ = "subteams"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )


class Member(Base):
    """Unified roster mirror of Legion — students and mentors alike. Read-only: synced
    from Legion by `member_code` (see services/legion_sync.py); Virtus never writes
    roster data back."""
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Legion's stable sync key. Nullable only so a legacy row can exist transiently until
    # the first sync back-links it by slack_user_id/name.
    member_code: Mapped[Optional[str]] = mapped_column(String(8), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[MemberKind] = mapped_column(
        SAEnum(MemberKind), nullable=False, default=MemberKind.student
    )
    team_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slack_user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Denormalized from Legion's per-member `subteam` object rather than an FK to
    # `subteams`: the roster sync sees a member's subteam before it has necessarily seen
    # that subteam in /api/subteams, and a stale slug should degrade to "show the slug"
    # rather than fail an insert.
    subteam_slug: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    subteam_label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    grade: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    graduation_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Legion group slugs this member holds, comma-joined (e.g. "virtus-admin"). Not used
    # for authorization decisions (those read the live mw_sso cookie instead), but kept
    # for parity with the sibling apps and for admin-side display.
    group_slugs: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    goals: Mapped[List["StudentGoal"]] = relationship(
        "StudentGoal", back_populates="member", cascade="all, delete-orphan"
    )

    def has_group(self, slug: str) -> bool:
        if not self.group_slugs:
            return False
        return slug in {s.strip() for s in self.group_slugs.split(",") if s.strip()}

    @property
    def subteam_display(self) -> str:
        return self.subteam_label or self.subteam_slug or "—"


class TeamGoal(Base):
    """A high-level season objective owned by leadership. Deliberately has no link to
    `StudentGoal` — the two lists are independent (see CLAUDE.md "Goals are two separate
    lists")."""
    __tablename__ = "team_goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    season: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # One of three fixed buckets (GoalCategory). Team goals are grouped by category, not
    # by roster subteam — see CLAUDE.md "Team goals are categorised and team-scoped".
    category: Mapped[GoalCategory] = mapped_column(
        SAEnum(GoalCategory), nullable=False,
        default=GoalCategory.robot_performance,
        server_default=GoalCategory.robot_performance.value,
    )
    # Which team the goal belongs to (4143 / 4423 / whole organisation). The board and the
    # admin list are filterable by it.
    team: Mapped[GoalTeam] = mapped_column(
        SAEnum(GoalTeam, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=GoalTeam.organization,
        server_default="organization",
    )
    target_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[GoalStatus] = mapped_column(
        SAEnum(GoalStatus), nullable=False, default=GoalStatus.not_started
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class StudentGoal(Base):
    """One student's own development goal. Owned by the student (they create and edit
    their own); staff can also add one while coaching."""
    __tablename__ = "student_goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    season: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    target_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[GoalStatus] = mapped_column(
        SAEnum(GoalStatus), nullable=False, default=GoalStatus.not_started
    )
    # Whoever typed it in — a student's own member_code for a self-authored goal, a
    # mentor's for one added during coaching. Display only; ownership is `member_id`.
    created_by_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    member: Mapped["Member"] = relationship("Member", back_populates="goals")


class Competency(Base):
    """The admin-editable master list of rated dimensions. Never read directly by a
    review — opening a cycle snapshots the active rows into `CycleCompetency` so later
    edits can't rewrite a finished cycle's form."""
    __tablename__ = "competencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )


class ReviewCycle(Base):
    """An admin-opened review round, e.g. "2026 Build Season Midpoint"."""
    __tablename__ = "review_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    season: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    opens_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closes_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[CycleStatus] = mapped_column(
        SAEnum(CycleStatus), nullable=False, default=CycleStatus.draft
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    competencies: Mapped[List["CycleCompetency"]] = relationship(
        "CycleCompetency", back_populates="cycle",
        cascade="all, delete-orphan", order_by="CycleCompetency.sort_order",
    )
    assignments: Mapped[List["ReviewAssignment"]] = relationship(
        "ReviewAssignment", back_populates="cycle", cascade="all, delete-orphan"
    )


class CycleCompetency(Base):
    """A competency as it existed when the cycle was opened. Frozen copy — renaming or
    retiring the master `Competency` afterward must not rewrite history, and self and
    reviewer are guaranteed to have answered the identical form."""
    __tablename__ = "cycle_competencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
        ForeignKey("review_cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Kept for provenance only; never joined against for display.
    competency_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    cycle: Mapped["ReviewCycle"] = relationship("ReviewCycle", back_populates="competencies")


class ReviewAssignment(Base):
    """One student's slot in a cycle, and who owes them their official review. This is the
    unit the completion dashboard counts, and the row that authorizes a reviewer — being
    named here is what lets a group-less student lead write a review."""
    __tablename__ = "review_assignments"
    __table_args__ = (UniqueConstraint("cycle_id", "member_id", name="uq_assignment_cycle_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
        ForeignKey("review_cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reviewer_member_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Snapshotted at cycle open so a mid-season subteam move doesn't reshuffle a finished
    # cycle's grouping.
    subteam_slug: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    subteam_label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    cycle: Mapped["ReviewCycle"] = relationship("ReviewCycle", back_populates="assignments")
    member: Mapped["Member"] = relationship("Member", foreign_keys=[member_id])
    reviewer: Mapped[Optional["Member"]] = relationship("Member", foreign_keys=[reviewer_member_id])
    reviews: Mapped[List["Review"]] = relationship(
        "Review", back_populates="assignment", cascade="all, delete-orphan"
    )

    def review_of(self, kind: ReviewKind) -> Optional["Review"]:
        for r in self.reviews:
            if r.kind == kind:
                return r
        return None


class Review(Base):
    """One filled-in form: either the student's self-review or the assigned reviewer's
    official review. At most one of each per assignment."""
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("assignment_id", "kind", name="uq_review_assignment_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("review_assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[ReviewKind] = mapped_column(SAEnum(ReviewKind), nullable=False)
    author_member_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[ReviewStatus] = mapped_column(
        SAEnum(ReviewStatus), nullable=False, default=ReviewStatus.draft
    )
    strengths: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    growth_areas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    overall_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Reviewer-only working notes. **Never** shown to the subject of the review — not once
    # it's submitted, not even if that subject holds a staff group. Visible to the author
    # and to staff standing above them. Only the `reviewer` kind ever sets it. Gate:
    # services/reviews.can_read_private_notes().
    private_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    assignment: Mapped["ReviewAssignment"] = relationship("ReviewAssignment", back_populates="reviews")
    author: Mapped[Optional["Member"]] = relationship("Member")
    ratings: Mapped[List["ReviewRating"]] = relationship(
        "ReviewRating", back_populates="review", cascade="all, delete-orphan"
    )

    @property
    def is_submitted(self) -> bool:
        return self.status == ReviewStatus.submitted


class ReviewRating(Base):
    """One competency's score on one review. `score` stays NULL while drafting — a
    submitted review requires every rating filled (enforced in services/reviews.py)."""
    __tablename__ = "review_ratings"
    __table_args__ = (
        UniqueConstraint("review_id", "cycle_competency_id", name="uq_rating_review_competency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle_competency_id: Mapped[int] = mapped_column(
        ForeignKey("cycle_competencies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    review: Mapped["Review"] = relationship("Review", back_populates="ratings")
    competency: Mapped["CycleCompetency"] = relationship("CycleCompetency")


class AuditLog(Base):
    """Append-only record of admin/manager mutations."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="admin")
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "cycle.open"
    entity_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON


# The 1-5 rating scale, shared by the form and the side-by-side view. Rendered as a
# red→blue rainbow (worst→best): 1 red, 2 orange, 3 yellow, 4 green, 5 blue — the colours
# live in the `.score-N` / `.score-choice-N` CSS in the two base templates, keyed on the
# number here.
SCORE_LABELS: dict[int, str] = {
    1: "Needs support",
    2: "Developing",
    3: "Solid",
    4: "Strong",
    5: "Exceptional",
}
