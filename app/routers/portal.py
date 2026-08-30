"""
Member portal — the team goals board, a person's own goals, and every review they either
own or owe.

Identity is the shared `mw_sso` Legion cookie (see `services/sso.py`); any active roster
member gets in, no group required. A fresh browser gets onto the cookie via `/enter`, the
one-tap Slack bootstrap.

**The reviewer form lives here, not under `/admin`.** A student subteam lead has to be
able to review their members without any admin access, so the right to write a review
comes from being named on the `ReviewAssignment` (`services/reviews.can_write`), never
from a Legion group. This is the one place Virtus deliberately departs from the sibling
apps' "anything staff-ish is under /admin" shape.
"""
from datetime import date
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    CycleStatus, GoalStatus, GoalTeam, Member, ReviewKind, ReviewStatus, SCORE_LABELS,
)
from app.services import audit, cycles as cycle_service, goals as goal_service
from app.services import legion_auth, reviews as review_service
from app.services.legion_auth import safe_next
from app.services.sso import is_staff, logout_url, make_authorize_url, sso_identity
from app.templating import templates

router = APIRouter()


# ── Member identity ──────────────────────────────────────────────────────────────

async def _current_member(request: Request, db: AsyncSession) -> Optional[Member]:
    identity = sso_identity(request)
    if identity is None:
        return None
    member = (
        await db.execute(select(Member).where(Member.member_code == identity["member_code"]))
    ).scalars().first()
    if member is None or not member.is_active:
        return None
    return member


def _signin_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(f"/me?next={quote(next_path, safe='')}", status_code=303)


def _identify(request: Request, next_path: Optional[str] = None):
    """The "we don't know who you are" page, with a link into Legion's sign-in."""
    identity = sso_identity(request)
    context = {
        "request": request,
        "authorize_url": make_authorize_url(request, return_to=next_path),
    }
    if identity is not None:
        # Signed in to Legion, but not on Virtus's roster mirror yet.
        context["not_synced"] = True
        context["signed_in_name"] = identity.get("name") or "that account"
    return templates.TemplateResponse("portal/identify.html", context)


def _parse_date(raw: Optional[str]) -> Optional[date]:
    """A browser `<input type=date>` value, or None. A malformed value is treated as
    "no date" rather than an error — it's an optional field on every form that has it."""
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _reviewer_denied(assignment, member) -> RedirectResponse:
    """Why a reviewer form was refused.

    "Not assigned to you" is deliberately the same answer for "no such assignment" and
    "someone else's" — telling them apart would leak who is being reviewed in which
    cycle. But an assigned reviewer hitting a *closed* cycle is neither of those, and
    deserves the accurate reason rather than being told their own assignment isn't theirs.
    """
    if assignment is not None and member is not None \
            and assignment.reviewer_member_id == member.id:
        return _redirect("/me/reviews", "That cycle is no longer open.")
    return _redirect("/me/reviews", "That review isn't assigned to you.")


def _redirect(path: str, message: str) -> RedirectResponse:
    """Post/redirect/get with a flash message.

    The message is percent-encoded whole: a raw non-latin-1 character (an emoji, a
    curly quote pasted into a goal title) in a `Location` header is not encodable and
    would crash the response.
    """
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}message={quote(message, safe='')}", status_code=303)


# ── Landing ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def team_goals_board(
    request: Request, db: AsyncSession = Depends(get_db), season: str = "", team: str = "",
):
    """The season's team goals, grouped by category and filterable by team. Read-only for
    everyone — editing lives at /admin/team-goals."""
    member = await _current_member(request, db)
    if not member:
        return _identify(request, "/")

    season = season or goal_service.current_season()
    team_filter = goal_service.parse_team(team)
    return templates.TemplateResponse(
        "portal/goals_board.html",
        {
            "request": request, "active_page": "board", "member": member,
            "season": season, "seasons": await goal_service.seasons(db),
            "groups": await goal_service.group_team_goals_by_category(
                db, season=season, team=team_filter
            ),
            "teams": list(GoalTeam),
            "team_filter": team_filter,
            "statuses": list(GoalStatus),
            "message": request.query_params.get("message"),
        },
    )


@router.get("/me", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db), next: str = ""):
    member = await _current_member(request, db)
    if not member:
        return _identify(request, safe_next(next) if next else None)

    season = goal_service.current_season()
    my_goals = await goal_service.list_student_goals(db, member.id, season=season)
    goal_shortfall = await goal_service.personal_goal_shortfall(db, member.id, season=season)

    # Cycles I'm *in* (my self-review), and cycles I owe someone else a review on. Both
    # lists are drawn from the same assignment rows, so they can't disagree.
    my_assignments = []
    for cycle in await cycle_service.open_cycles(db):
        assignment = await cycle_service.get_assignment_for_member(db, cycle.id, member.id)
        if assignment:
            my_assignments.append(assignment)
    owed = await cycle_service.assignments_for_reviewer(db, member.id)

    return templates.TemplateResponse(
        "portal/home.html",
        {
            "request": request, "active_page": "home", "member": member,
            "season": season, "goals": my_goals, "statuses": list(GoalStatus),
            "goal_shortfall": goal_shortfall,
            "required_goals": goal_service.required_personal_goals(),
            "my_assignments": my_assignments, "owed": owed,
            "ReviewKind": ReviewKind, "ReviewStatus": ReviewStatus,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/enter")
async def enter(
    request: Request, member: str = "", next: str = "/me", db: AsyncSession = Depends(get_db)
):
    """One-tap sign-in bootstrap. If the browser already holds a live `mw_sso` cookie,
    skip Legion entirely; otherwise start a Legion SSO challenge for the known member and
    send the browser to the "check Slack" pending page. Passes an **absolute** return_to
    so the fresh-sign-in path lands back on Virtus's host."""
    next_path = safe_next(next)
    if sso_identity(request) is not None:
        return RedirectResponse(next_path, status_code=303)

    row = None
    if member:
        row = (
            await db.execute(
                select(Member).where(Member.member_code == member, Member.is_active.is_(True))
            )
        ).scalars().first()
    if row is None:
        return RedirectResponse(make_authorize_url(request, return_to=next_path), status_code=303)

    pending_url = await legion_auth.start_challenge(
        row.member_code, return_to=f"{settings.base_url}{next_path}"
    )
    if pending_url is None:
        return templates.TemplateResponse(
            "portal/sso_unavailable.html", {"request": request}, status_code=503
        )
    return RedirectResponse(pending_url, status_code=303)


@router.get("/me/logout")
async def logout(request: Request):
    return RedirectResponse(logout_url(request, return_to="/me"), status_code=303)


# ── My goals ─────────────────────────────────────────────────────────────────────

@router.post("/me/goals")
async def create_goal(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    target_date: str = Form(""),
    status: str = Form("not_started"),
    db: AsyncSession = Depends(get_db),
):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect("/me")
    try:
        goal = await goal_service.create_student_goal(
            db, member,
            title=title, description=description,
            target_date=_parse_date(target_date),
            status=goal_service.parse_status(status),
            created_by_code=member.member_code,
        )
    except goal_service.GoalError as e:
        return _redirect("/me", str(e))
    await audit.record(
        db, request, "goal.create", f"{member.name} added goal “{goal.title}”",
        entity_type="student_goal", entity_id=goal.id,
    )
    await db.commit()
    return _redirect("/me", "Goal added.")


@router.post("/me/goals/{goal_id}/edit")
async def edit_goal(
    goal_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    target_date: str = Form(""),
    status: str = Form("not_started"),
    db: AsyncSession = Depends(get_db),
):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect("/me")
    goal = await goal_service.get_student_goal_for(db, goal_id, member.id)
    if goal is None:
        return _redirect("/me", "That goal doesn't exist.")
    try:
        await goal_service.update_student_goal(
            db, goal, title=title, description=description,
            target_date=_parse_date(target_date),
            status=goal_service.parse_status(status),
        )
    except goal_service.GoalError as e:
        return _redirect("/me", str(e))
    await audit.record(
        db, request, "goal.update", f"{member.name} edited goal “{goal.title}”",
        entity_type="student_goal", entity_id=goal.id,
    )
    await db.commit()
    return _redirect("/me", "Goal updated.")


@router.post("/me/goals/{goal_id}/status")
async def set_goal_status(
    goal_id: int, request: Request, status: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """One-click status change from the goal list — the action students take most."""
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect("/me")
    goal = await goal_service.get_student_goal_for(db, goal_id, member.id)
    if goal is None:
        return _redirect("/me", "That goal doesn't exist.")
    goal.status = goal_service.parse_status(status)
    await audit.record(
        db, request, "goal.status",
        f"{member.name} set goal “{goal.title}” to {goal.status.value}",
        entity_type="student_goal", entity_id=goal.id,
    )
    await db.commit()
    return _redirect("/me", "Goal updated.")


@router.post("/me/goals/{goal_id}/delete")
async def delete_goal(goal_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect("/me")
    goal = await goal_service.get_student_goal_for(db, goal_id, member.id)
    if goal is None:
        return _redirect("/me", "That goal doesn't exist.")
    title = goal.title
    await db.delete(goal)
    await audit.record(
        db, request, "goal.delete", f"{member.name} deleted goal “{title}”",
        entity_type="student_goal", entity_id=goal_id,
    )
    await db.commit()
    return _redirect("/me", "Goal deleted.")


# ── Writing a review ─────────────────────────────────────────────────────────────

def _form_ratings(form) -> tuple[dict[int, str], dict[int, str]]:
    """Pull `score_{id}` / `comment_{id}` pairs out of the posted form.

    Field names carry the `cycle_competency_id` because the frozen snapshot is what the
    form was rendered from — posting master `Competency` ids would break the moment an
    admin edited the master list mid-cycle.
    """
    scores: dict[int, str] = {}
    comments: dict[int, str] = {}
    for key, value in form.multi_items():
        for prefix, target in (("score_", scores), ("comment_", comments)):
            if key.startswith(prefix):
                try:
                    target[int(key[len(prefix):])] = value
                except ValueError:
                    pass
    return scores, comments


async def _render_review_form(
    request: Request, db: AsyncSession, member: Member, assignment, kind: ReviewKind,
    *, error: Optional[str] = None,
):
    review = await review_service.get_or_create(db, assignment, kind, member)
    await db.commit()
    return templates.TemplateResponse(
        "portal/review_form.html",
        {
            "request": request, "active_page": "home", "member": member,
            "assignment": assignment, "cycle": assignment.cycle, "kind": kind,
            "review": review, "ratings": await review_service.load_ratings(db, review),
            "score_labels": SCORE_LABELS,
            "subject": assignment.member,
            "goals": await goal_service.list_student_goals(
                db, assignment.member_id, season=assignment.cycle.season
            ),
            "required_goals": goal_service.required_personal_goals(),
            "error": error,
            "message": request.query_params.get("message"),
        },
        status_code=400 if error else 200,
    )


@router.get("/me/review/{cycle_id}", response_class=HTMLResponse)
async def self_review_form(cycle_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect(f"/me/review/{cycle_id}")
    assignment = await cycle_service.get_assignment_for_member(db, cycle_id, member.id)
    if assignment is None:
        return _redirect("/me", "You're not part of that review cycle.")
    if not review_service.can_write(assignment, ReviewKind.self_review, member):
        return _redirect(f"/me/review/{cycle_id}/result", "That cycle is no longer open.")
    return await _render_review_form(request, db, member, assignment, ReviewKind.self_review)


@router.post("/me/review/{cycle_id}")
async def self_review_save(cycle_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect(f"/me/review/{cycle_id}")
    assignment = await cycle_service.get_assignment_for_member(db, cycle_id, member.id)
    if assignment is None or not review_service.can_write(
        assignment, ReviewKind.self_review, member
    ):
        return _redirect("/me", "That review can't be edited.")
    return await _save_review(request, db, member, assignment, ReviewKind.self_review)


@router.get("/me/reviews", response_class=HTMLResponse)
async def reviews_i_owe(request: Request, db: AsyncSession = Depends(get_db)):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect("/me/reviews")
    return templates.TemplateResponse(
        "portal/reviews_owed.html",
        {
            "request": request, "active_page": "owed", "member": member,
            "assignments": await cycle_service.assignments_for_reviewer(db, member.id),
            "ReviewKind": ReviewKind, "ReviewStatus": ReviewStatus,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/me/reviews/{assignment_id}", response_class=HTMLResponse)
async def reviewer_form(
    assignment_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect(f"/me/reviews/{assignment_id}")
    assignment = await cycle_service.get_assignment(db, assignment_id)
    if assignment is None or not review_service.can_write(
        assignment, ReviewKind.reviewer, member
    ):
        return _reviewer_denied(assignment, member)
    return await _render_review_form(request, db, member, assignment, ReviewKind.reviewer)


@router.post("/me/reviews/{assignment_id}")
async def reviewer_save(
    assignment_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect(f"/me/reviews/{assignment_id}")
    assignment = await cycle_service.get_assignment(db, assignment_id)
    if assignment is None or not review_service.can_write(
        assignment, ReviewKind.reviewer, member
    ):
        return _reviewer_denied(assignment, member)
    return await _save_review(request, db, member, assignment, ReviewKind.reviewer)


async def _save_review(
    request: Request, db: AsyncSession, member: Member, assignment, kind: ReviewKind
):
    """Shared save/submit handler for both review kinds — they answer the identical form,
    so they take the identical path in and differ only in where they land afterwards."""
    form = await request.form()
    scores, comments = _form_ratings(form)
    submit = form.get("action") == "submit"

    # A student can't submit their self-review while short of the personal-goal minimum —
    # the review is meant to be read next to that list. `save()` enforces it alongside the
    # "every competency rated" check, so the draft is still kept and the form re-renders.
    goal_shortfall = 0
    if kind == ReviewKind.self_review:
        goal_shortfall = await goal_service.personal_goal_shortfall(
            db, assignment.member_id, season=assignment.cycle.season
        )

    review = await review_service.get_or_create(db, assignment, kind, member)
    try:
        await review_service.save(
            db, review,
            strengths=form.get("strengths"),
            growth_areas=form.get("growth_areas"),
            overall_comment=form.get("overall_comment"),
            # Private notes are a reviewer-only field; never accept them onto a self-review.
            private_notes=form.get("private_notes") if kind == ReviewKind.reviewer else None,
            scores=scores, comments=comments,
            self_goal_shortfall=goal_shortfall, submit=submit,
        )
    except review_service.ReviewError as e:
        # Commit rather than roll back. `save()` writes the answers *before* it checks
        # completeness, so by the time it raises, everything the person typed is staged —
        # rolling back would throw their work away and hand them a blank form to redo.
        # Only the submit itself was refused; the draft is perfectly good.
        await db.commit()
        return await _render_review_form(request, db, member, assignment, kind, error=str(e))

    if submit:
        await audit.record(
            db, request, f"review.submit.{kind.value}",
            f"{member.name} submitted the {kind.value} review for {assignment.member.name}",
            entity_type="review", entity_id=review.id,
        )
    await db.commit()

    if not submit:
        target = (
            f"/me/review/{assignment.cycle_id}" if kind == ReviewKind.self_review
            else f"/me/reviews/{assignment.id}"
        )
        return _redirect(target, "Draft saved.")
    if kind == ReviewKind.self_review:
        return _redirect(f"/me/review/{assignment.cycle_id}/result", "Self-review submitted.")
    return _redirect("/me/reviews", f"Review of {assignment.member.name} submitted.")


@router.get("/me/review/{cycle_id}/result", response_class=HTMLResponse)
async def review_result(cycle_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """My self-review next to my reviewer's, once each is submitted. Either side that
    isn't submitted yet simply renders as pending — the page is useful before both are in."""
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect(f"/me/review/{cycle_id}/result")
    assignment = await cycle_service.get_assignment_for_member(db, cycle_id, member.id)
    if assignment is None:
        return _redirect("/me", "You're not part of that review cycle.")

    staff = is_staff(sso_identity(request))
    self_review = assignment.review_of(ReviewKind.self_review)
    reviewer_review = assignment.review_of(ReviewKind.reviewer)
    return templates.TemplateResponse(
        "portal/review_result.html",
        {
            "request": request, "active_page": "home", "member": member,
            "assignment": assignment, "cycle": assignment.cycle,
            "subject": assignment.member,
            "rows": await review_service.comparison(db, assignment),
            "self_review": self_review if review_service.can_read(
                assignment, self_review, member, is_staff=staff) else None,
            "reviewer_review": reviewer_review if review_service.can_read(
                assignment, reviewer_review, member, is_staff=staff) else None,
            "score_labels": SCORE_LABELS,
            "can_edit_self": review_service.can_write(
                assignment, ReviewKind.self_review, member),
            "message": request.query_params.get("message"),
        },
    )
