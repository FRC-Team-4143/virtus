"""
Admin / manager UI — team goals, competencies, and the review cycles everything else
hangs off.

Gated by Legion SSO: `virtus-admin` (full) or `virtus-manager` (team goals, cycles, and
the completion dashboard — not competencies, roster, settings, backup, or audit).

Note what is *not* here: writing a review. That lives in the portal
(`routers/portal.py`), authorized by the `ReviewAssignment` rather than by a group, so a
student subteam lead can review their members with no admin access at all.

Every mutation records an audit row (services/audit.py) in the same transaction as the
change it describes.
"""
import os
from datetime import date, datetime, time
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    AuditLog, Competency, CycleStatus, GoalCategory, GoalStatus, GoalTeam, Member,
    MemberKind, ReviewCycle, ReviewKind, ReviewStatus, SCORE_LABELS, TeamGoal,
)
from app.services import audit, cycles as cycle_service, goals as goal_service
from app.services import reviews as review_service
from app.services.backup import is_sqlite, list_backups, nightly_backup, stage_restore
from app.services.legion_sync import LegionSyncError, sync_roster
from app.services.notify import notify_cycle_opened, notify_outstanding
from app.services.sso import (
    is_admin, is_link_identity, is_staff, logout_url, make_authorize_url, sso_identity,
)
from app.templating import templates

router = APIRouter(prefix="/admin")

_ADMIN_GROUP = "virtus-admin"
_MANAGER_GROUP = "virtus-manager"


# ── Auth guards ──────────────────────────────────────────────────────────────────

def _manager_allowed(path: str) -> bool:
    """The sections a `virtus-manager` may reach: team goals, review cycles, the student
    profiles, and the dashboard.

    Excluded, and admin-only: **competencies** (editing the master list changes the shape
    of every future cycle's form — a one-way change to how the whole team gets assessed),
    plus roster/settings/backup/audit, which are admin-only in every sibling app.
    Also excluded is `/unsubmit`, which reopens a review a student may already have read.
    """
    p = path.rstrip("/")
    if p.endswith("/unsubmit"):
        return False
    return (
        p == "/admin"
        or p == "/admin/team-goals"
        or p.startswith("/admin/team-goals/")
        or p == "/admin/cycles"
        or p.startswith("/admin/cycles/")
        or p.startswith("/admin/students/")
    )


_SECTION_LABELS = [
    ("/admin/team-goals", "Team Goals"),
    ("/admin/competencies", "Competencies"),
    ("/admin/cycles", "Review Cycles"),
    ("/admin/students", "Students"),
    ("/admin/roster", "Roster"),
    ("/admin/audit", "Audit Log"),
    ("/admin/backup", "Backup"),
    ("/admin/settings", "Settings"),
    ("/admin", "Dashboard"),
]


def _section_label(path: str) -> str:
    """A human label for the section a denied request was aimed at, for the forbidden
    page's message. Order matters — most-specific prefix first, since "/admin" is itself
    a prefix of every other admin path."""
    for prefix, label in _SECTION_LABELS:
        if path.startswith(prefix):
            return label
    return "this page"


def _require_auth(request: Request):
    """Gate every admin route via Legion SSO. Returns None when allowed, otherwise the
    response to return instead.

    `virtus-admin` passes everywhere; `virtus-manager` only where `_manager_allowed`
    says. A magic-link identity carries no groups by construction, so it already fails
    every check below — but it's treated as "not signed in strongly enough" rather than
    "not allowed", since the person may well be an admin who just arrived from a Slack
    link. Send them to a real sign-in instead of stranding them on a 403.
    """
    identity = sso_identity(request)
    if identity is None or is_link_identity(identity):
        return RedirectResponse(make_authorize_url(request), status_code=303)
    groups = set(identity.get("groups") or [])
    if _ADMIN_GROUP in groups:
        return None
    if _MANAGER_GROUP in groups and _manager_allowed(request.url.path):
        return None
    return templates.TemplateResponse(
        "admin/forbidden.html",
        {
            "request": request,
            "name": identity.get("name", ""),
            "section": _section_label(request.url.path),
        },
        status_code=403,
    )


def _require_admin(request: Request):
    """Same as `_require_auth`, but full-admin only regardless of the path allowlist."""
    if redirect := _require_auth(request):
        return redirect
    if not is_admin(sso_identity(request)):
        return templates.TemplateResponse(
            "admin/forbidden.html",
            {"request": request, "section": _section_label(request.url.path)},
            status_code=403,
        )
    return None


def _manager_locked(request: Request) -> bool:
    """True when the viewer is a manager without full admin — drives the padlock the
    sidebar shows next to sections `_manager_allowed` excludes them from."""
    identity = sso_identity(request)
    if identity is None:
        return False
    groups = set(identity.get("groups") or [])
    return _MANAGER_GROUP in groups and _ADMIN_GROUP not in groups


templates.env.globals["manager_allowed"] = _manager_allowed
templates.env.globals["manager_locked"] = _manager_locked


# ── Shared helpers ───────────────────────────────────────────────────────────────

def _redirect(path: str, message: str) -> RedirectResponse:
    """Post/redirect/get with a flash message, percent-encoded whole — a raw emoji or
    curly quote in a `Location` header isn't latin-1 encodable and would crash the
    response."""
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}message={quote(message, safe='')}", status_code=303)


def _parse_date(raw: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _parse_datetime(raw: Optional[str]) -> Optional[datetime]:
    """A `<input type=date>` value as an end-of-day naive UTC datetime.

    Cycle windows are day-granular in the UI, and "closes on the 14th" has to mean the
    end of the 14th, not its first instant — otherwise a cycle nominally open all day
    would already be past its close date the moment it began.
    """
    d = _parse_date(raw)
    return datetime.combine(d, time(23, 59)) if d else None


def _actor_code(request: Request) -> Optional[str]:
    identity = sso_identity(request)
    return identity.get("member_code") if identity else None


async def _get_member(db: AsyncSession, member_code: str) -> Optional[Member]:
    return (await db.execute(
        select(Member).where(Member.member_code == member_code)
    )).scalars().first()


# ── Dashboard ────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    season = goal_service.current_season()
    open_cycles = []
    for cycle in await cycle_service.open_cycles(db):
        assignments = await cycle_service.load_assignments(db, cycle.id)
        open_cycles.append({"cycle": cycle, "stats": cycle_service.completion(assignments)})

    team_goals = await goal_service.list_team_goals(db, season=season)
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request, "active_page": "dashboard", "season": season,
            "open_cycles": open_cycles,
            "team_goals": team_goals,
            "at_risk": [g for g in team_goals if g.status == GoalStatus.at_risk],
            "done_count": sum(1 for g in team_goals if g.status == GoalStatus.done),
            "message": request.query_params.get("message"),
        },
    )


@router.get("/logout")
async def admin_logout(request: Request):
    return RedirectResponse(logout_url(request, return_to="/admin"), status_code=303)


# ── Team goals ───────────────────────────────────────────────────────────────────

@router.get("/team-goals", response_class=HTMLResponse)
async def team_goals(
    request: Request, db: AsyncSession = Depends(get_db), season: str = "", team: str = "",
):
    if redirect := _require_auth(request):
        return redirect
    season = season or goal_service.current_season()
    team_filter = goal_service.parse_team(team)
    return templates.TemplateResponse(
        "admin/team_goals.html",
        {
            "request": request, "active_page": "team-goals", "season": season,
            "seasons": await goal_service.seasons(db),
            "goals": await goal_service.list_team_goals(db, season=season, team=team_filter),
            "categories": list(GoalCategory),
            "teams": list(GoalTeam),
            "team_filter": team_filter,
            "default_team": GoalTeam.organization,
            "statuses": list(GoalStatus),
            "message": request.query_params.get("message"),
        },
    )


@router.post("/team-goals")
async def team_goal_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    season: str = Form(""),
    category: str = Form("robot_performance"),
    team: str = Form("organization"),
    target_date: str = Form(""),
    status: str = Form("not_started"),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    season = season or goal_service.current_season()
    try:
        goal = await goal_service.create_team_goal(
            db, title=title, description=description, season=season,
            category=goal_service.parse_category(category),
            team=goal_service.parse_team(team) or GoalTeam.organization,
            target_date=_parse_date(target_date),
            status=goal_service.parse_status(status),
            created_by_code=_actor_code(request),
        )
    except goal_service.GoalError as e:
        return _redirect(f"/admin/team-goals?season={quote(season)}", str(e))
    await audit.record(
        db, request, "team_goal.create", f"Added team goal “{goal.title}”",
        entity_type="team_goal", entity_id=goal.id,
    )
    await db.commit()
    return _redirect(f"/admin/team-goals?season={quote(season)}", "Team goal added.")


@router.post("/team-goals/{goal_id}/edit")
async def team_goal_edit(
    goal_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form("robot_performance"),
    team: str = Form("organization"),
    target_date: str = Form(""),
    status: str = Form("not_started"),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    goal = await db.get(TeamGoal, goal_id)
    if goal is None:
        return _redirect("/admin/team-goals", "That goal doesn't exist.")
    try:
        await goal_service.update_team_goal(
            db, goal, title=title, description=description,
            category=goal_service.parse_category(category),
            team=goal_service.parse_team(team) or GoalTeam.organization,
            target_date=_parse_date(target_date), status=goal_service.parse_status(status),
        )
    except goal_service.GoalError as e:
        return _redirect(f"/admin/team-goals?season={quote(goal.season)}", str(e))
    await audit.record(
        db, request, "team_goal.update", f"Edited team goal “{goal.title}”",
        entity_type="team_goal", entity_id=goal.id,
    )
    await db.commit()
    return _redirect(f"/admin/team-goals?season={quote(goal.season)}", "Team goal updated.")


@router.post("/team-goals/{goal_id}/status")
async def team_goal_status(
    goal_id: int, request: Request, status: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    goal = await db.get(TeamGoal, goal_id)
    if goal is None:
        return _redirect("/admin/team-goals", "That goal doesn't exist.")
    goal.status = goal_service.parse_status(status)
    await audit.record(
        db, request, "team_goal.status",
        f"Set team goal “{goal.title}” to {goal.status.value}",
        entity_type="team_goal", entity_id=goal.id,
    )
    await db.commit()
    return _redirect(f"/admin/team-goals?season={quote(goal.season)}", "Team goal updated.")


@router.post("/team-goals/{goal_id}/delete")
async def team_goal_delete(goal_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    goal = await db.get(TeamGoal, goal_id)
    if goal is None:
        return _redirect("/admin/team-goals", "That goal doesn't exist.")
    title, season = goal.title, goal.season
    await db.delete(goal)
    await audit.record(
        db, request, "team_goal.delete", f"Deleted team goal “{title}”",
        entity_type="team_goal", entity_id=goal_id,
    )
    await db.commit()
    return _redirect(f"/admin/team-goals?season={quote(season)}", "Team goal deleted.")


# ── Competencies (admin only) ────────────────────────────────────────────────────

@router.get("/competencies", response_class=HTMLResponse)
async def competencies(request: Request, db: AsyncSession = Depends(get_db), show_archived: int = 0):
    if redirect := _require_admin(request):
        return redirect
    stmt = select(Competency).order_by(Competency.sort_order, Competency.name)
    if not show_archived:
        stmt = stmt.where(Competency.is_active.is_(True))
    return templates.TemplateResponse(
        "admin/competencies.html",
        {
            "request": request, "active_page": "competencies",
            "competencies": (await db.execute(stmt)).scalars().all(),
            "show_archived": bool(show_archived),
            "score_labels": SCORE_LABELS,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/competencies")
async def competency_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_admin(request):
        return redirect
    name = name.strip()
    if not name:
        return _redirect("/admin/competencies", "A competency needs a name.")
    rows = (await db.execute(select(Competency))).scalars().all()
    row = Competency(
        name=name[:120], description=description.strip() or None,
        sort_order=(max((c.sort_order for c in rows), default=0) + 10),
    )
    db.add(row)
    await db.flush()
    await audit.record(
        db, request, "competency.create", f"Added competency “{row.name}”",
        entity_type="competency", entity_id=row.id,
    )
    await db.commit()
    return _redirect("/admin/competencies", "Competency added.")


@router.post("/competencies/{competency_id}/edit")
async def competency_edit(
    competency_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    sort_order: int = Form(0),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_admin(request):
        return redirect
    row = await db.get(Competency, competency_id)
    if row is None:
        return _redirect("/admin/competencies", "That competency doesn't exist.")
    if not name.strip():
        return _redirect("/admin/competencies", "A competency needs a name.")
    row.name = name.strip()[:120]
    row.description = description.strip() or None
    row.sort_order = sort_order
    await audit.record(
        db, request, "competency.update", f"Edited competency “{row.name}”",
        entity_type="competency", entity_id=row.id,
    )
    await db.commit()
    return _redirect("/admin/competencies", "Competency updated.")


@router.post("/competencies/{competency_id}/archive")
async def competency_archive(
    competency_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """Archive/restore, mirroring the siblings' "hide, don't erase" story.

    There is no delete: an archived competency stays readable for any cycle whose frozen
    snapshot named it, and archiving is the only way `is_active` is ever set — so there
    is one unambiguous path rather than two that could drift.
    """
    if redirect := _require_admin(request):
        return redirect
    row = await db.get(Competency, competency_id)
    if row is None:
        return _redirect("/admin/competencies", "That competency doesn't exist.")
    row.is_active = not row.is_active
    verb = "Restored" if row.is_active else "Archived"
    await audit.record(
        db, request, "competency.archive", f"{verb} competency “{row.name}”",
        entity_type="competency", entity_id=row.id,
    )
    await db.commit()
    return _redirect("/admin/competencies", f"{verb} “{row.name}”.")


# ── Review cycles ────────────────────────────────────────────────────────────────

@router.get("/cycles", response_class=HTMLResponse)
async def cycles_list(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    rows = []
    for cycle in await cycle_service.list_cycles(db):
        assignments = await cycle_service.load_assignments(db, cycle.id)
        rows.append({"cycle": cycle, "stats": cycle_service.completion(assignments)})
    return templates.TemplateResponse(
        "admin/cycles.html",
        {
            "request": request, "active_page": "cycles", "rows": rows,
            "season": goal_service.current_season(),
            "message": request.query_params.get("message"),
        },
    )


@router.post("/cycles")
async def cycle_create(
    request: Request,
    name: str = Form(...),
    season: str = Form(""),
    opens_at: str = Form(""),
    closes_at: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    try:
        cycle = await cycle_service.create_cycle(
            db, name=name, season=season or goal_service.current_season(),
            opens_at=_parse_datetime(opens_at), closes_at=_parse_datetime(closes_at),
        )
        # A new cycle is immediately useful only with a roster, and every cycle so far
        # has wanted every active student — so populate up front rather than making the
        # admin click a second button before they can assign anyone.
        added = await cycle_service.populate_roster(db, cycle)
    except cycle_service.CycleError as e:
        return _redirect("/admin/cycles", str(e))
    await audit.record(
        db, request, "cycle.create", f"Created review cycle “{cycle.name}” with {added} student(s)",
        entity_type="cycle", entity_id=cycle.id,
    )
    await db.commit()
    return _redirect(f"/admin/cycles/{cycle.id}", f"Cycle created with {added} student(s).")


@router.get("/cycles/{cycle_id}", response_class=HTMLResponse)
async def cycle_detail(cycle_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    cycle = await cycle_service.get_cycle(db, cycle_id)
    if cycle is None:
        return _redirect("/admin/cycles", "That cycle doesn't exist.")
    assignments = await cycle_service.load_assignments(db, cycle.id)
    # Mentors first in the reviewer picker, but students stay selectable — subteam leads
    # are students, and they're the whole reason the reviewer role isn't a Legion group.
    reviewers = (await db.execute(
        select(Member).where(Member.is_active.is_(True)).order_by(
            (Member.kind == MemberKind.student).asc(), Member.name
        )
    )).scalars().all()
    return templates.TemplateResponse(
        "admin/cycle_detail.html",
        {
            "request": request, "active_page": "cycles", "cycle": cycle,
            "assignments": assignments, "reviewers": reviewers,
            "stats": cycle_service.completion(assignments),
            "subteams": await goal_service.list_subteams(db),
            "ReviewKind": ReviewKind, "ReviewStatus": ReviewStatus,
            "CycleStatus": CycleStatus,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/cycles/{cycle_id}/assign")
async def cycle_assign(
    cycle_id: int, request: Request,
    assignment_id: int = Form(...),
    reviewer_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    assignment = await cycle_service.get_assignment(db, assignment_id)
    if assignment is None or assignment.cycle_id != cycle_id:
        return _redirect(f"/admin/cycles/{cycle_id}", "That assignment doesn't exist.")
    try:
        await cycle_service.set_reviewer(
            db, assignment, int(reviewer_id) if reviewer_id else None
        )
    except (cycle_service.CycleError, ValueError) as e:
        return _redirect(f"/admin/cycles/{cycle_id}", str(e))
    who = assignment.reviewer.name if assignment.reviewer else "nobody"
    await audit.record(
        db, request, "cycle.assign",
        f"Assigned {assignment.member.name}'s review to {who}",
        entity_type="assignment", entity_id=assignment.id,
    )
    await db.commit()
    return _redirect(f"/admin/cycles/{cycle_id}", f"Reviewer set to {who}.")


@router.post("/cycles/{cycle_id}/assign-subteam")
async def cycle_assign_subteam(
    cycle_id: int, request: Request,
    subteam_slug: str = Form(""),
    reviewer_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    cycle = await cycle_service.get_cycle(db, cycle_id)
    if cycle is None:
        return _redirect("/admin/cycles", "That cycle doesn't exist.")
    count = await cycle_service.bulk_assign_subteam(
        db, cycle, subteam_slug=subteam_slug or None, reviewer_id=reviewer_id
    )
    reviewer = await db.get(Member, reviewer_id)
    await audit.record(
        db, request, "cycle.assign_bulk",
        f"Assigned {count} review(s) on “{cycle.name}” to {reviewer.name if reviewer else reviewer_id}",
        entity_type="cycle", entity_id=cycle.id,
    )
    await db.commit()
    return _redirect(
        f"/admin/cycles/{cycle_id}",
        f"Assigned {count} unassigned review(s)." if count
        else "Nothing to assign — those slots already have reviewers.",
    )


@router.post("/cycles/{cycle_id}/refresh-roster")
async def cycle_refresh_roster(
    cycle_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """Pull in students who joined the roster after the cycle was created."""
    if redirect := _require_auth(request):
        return redirect
    cycle = await cycle_service.get_cycle(db, cycle_id)
    if cycle is None:
        return _redirect("/admin/cycles", "That cycle doesn't exist.")
    try:
        added = await cycle_service.populate_roster(db, cycle)
    except cycle_service.CycleError as e:
        return _redirect(f"/admin/cycles/{cycle_id}", str(e))
    await audit.record(
        db, request, "cycle.roster", f"Added {added} student(s) to “{cycle.name}”",
        entity_type="cycle", entity_id=cycle.id,
    )
    await db.commit()
    return _redirect(f"/admin/cycles/{cycle_id}", f"Added {added} student(s).")


@router.post("/cycles/{cycle_id}/open")
async def cycle_open(
    cycle_id: int, request: Request, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    cycle = await cycle_service.get_cycle(db, cycle_id)
    if cycle is None:
        return _redirect("/admin/cycles", "That cycle doesn't exist.")
    try:
        await cycle_service.open_cycle(db, cycle)
    except cycle_service.CycleError as e:
        return _redirect(f"/admin/cycles/{cycle_id}", str(e))
    await audit.record(
        db, request, "cycle.open", f"Opened review cycle “{cycle.name}”",
        entity_type="cycle", entity_id=cycle.id,
    )
    await db.commit()

    # Announce after the commit, in the background: a Slack outage must not roll back the
    # open, and the admin shouldn't wait on a few hundred DMs.
    assignments = await cycle_service.load_assignments(db, cycle.id)
    background_tasks.add_task(notify_cycle_opened, cycle.name, cycle.id, list(assignments))
    return _redirect(f"/admin/cycles/{cycle_id}", "Cycle opened — everyone has been notified.")


@router.post("/cycles/{cycle_id}/close")
async def cycle_close(cycle_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    cycle = await cycle_service.get_cycle(db, cycle_id)
    if cycle is None:
        return _redirect("/admin/cycles", "That cycle doesn't exist.")
    try:
        await cycle_service.close_cycle(db, cycle)
    except cycle_service.CycleError as e:
        return _redirect(f"/admin/cycles/{cycle_id}", str(e))
    await audit.record(
        db, request, "cycle.close", f"Closed review cycle “{cycle.name}”",
        entity_type="cycle", entity_id=cycle.id,
    )
    await db.commit()
    return _redirect(f"/admin/cycles/{cycle_id}", "Cycle closed.")


@router.post("/cycles/{cycle_id}/reopen")
async def cycle_reopen(cycle_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    cycle = await cycle_service.get_cycle(db, cycle_id)
    if cycle is None:
        return _redirect("/admin/cycles", "That cycle doesn't exist.")
    try:
        await cycle_service.reopen_cycle(db, cycle)
    except cycle_service.CycleError as e:
        return _redirect(f"/admin/cycles/{cycle_id}", str(e))
    await audit.record(
        db, request, "cycle.reopen", f"Reopened review cycle “{cycle.name}”",
        entity_type="cycle", entity_id=cycle.id,
    )
    await db.commit()
    return _redirect(f"/admin/cycles/{cycle_id}", "Cycle reopened.")


@router.post("/cycles/{cycle_id}/nag")
async def cycle_nag(
    cycle_id: int, request: Request, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """DM everyone who still owes a review on this cycle."""
    if redirect := _require_auth(request):
        return redirect
    cycle = await cycle_service.get_cycle(db, cycle_id)
    if cycle is None:
        return _redirect("/admin/cycles", "That cycle doesn't exist.")
    outstanding = await cycle_service.outstanding_reviews(db, cycle)
    background_tasks.add_task(notify_outstanding, cycle.name, list(outstanding))
    return _redirect(
        f"/admin/cycles/{cycle_id}",
        f"Reminded {len(outstanding)} person/people." if outstanding
        else "Nothing outstanding — everyone is done.",
    )


@router.post("/cycles/{cycle_id}/reviews/{review_id}/unsubmit")
async def review_unsubmit(
    cycle_id: int, review_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """Reopen a submitted review for editing. Admin-only and audited — submission is what
    lets a student trust the review they read won't change underneath them."""
    if redirect := _require_admin(request):
        return redirect
    from app.models import Review
    review = await db.get(Review, review_id)
    if review is None or review.assignment.cycle_id != cycle_id:
        return _redirect(f"/admin/cycles/{cycle_id}", "That review doesn't exist.")
    await review_service.unsubmit(db, review)
    await audit.record(
        db, request, "review.unsubmit",
        f"Reopened the {review.kind.value} review for {review.assignment.member.name}",
        entity_type="review", entity_id=review.id,
    )
    await db.commit()
    return _redirect(f"/admin/cycles/{cycle_id}", "Review reopened for editing.")


# ── Student profile ──────────────────────────────────────────────────────────────

@router.get("/students/{member_code}", response_class=HTMLResponse)
async def student_profile(
    member_code: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """One student's goals and their whole review history — the page a mentor opens
    before sitting down with them."""
    if redirect := _require_auth(request):
        return redirect
    member = await _get_member(db, member_code)
    if member is None:
        return _redirect("/admin/roster", "No such member.")

    # A reviewer's private notes are shown to staff — but never to the subject themselves,
    # even a staff member reading their own profile.
    viewer_code = _actor_code(request)
    viewer_is_subject = bool(viewer_code) and viewer_code == member.member_code

    history = []
    for cycle in await cycle_service.list_cycles(db):
        assignment = await cycle_service.get_assignment_for_member(db, cycle.id, member.id)
        if assignment is None:
            continue
        history.append({
            "cycle": cycle,
            "assignment": assignment,
            "rows": await review_service.comparison(db, assignment),
            "self_review": assignment.review_of(ReviewKind.self_review),
            "reviewer_review": assignment.review_of(ReviewKind.reviewer),
            "show_private_notes": not viewer_is_subject,
        })

    return templates.TemplateResponse(
        "admin/student.html",
        {
            "request": request, "active_page": "roster", "member": member,
            "goals": await goal_service.list_student_goals(db, member.id),
            "history": history, "score_labels": SCORE_LABELS,
            "ReviewStatus": ReviewStatus,
            "message": request.query_params.get("message"),
        },
    )


# ── Roster (admin only) ──────────────────────────────────────────────────────────

@router.get("/roster", response_class=HTMLResponse)
async def roster(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_admin(request):
        return redirect
    members = (
        await db.execute(select(Member).order_by(Member.is_active.desc(), Member.name))
    ).scalars().all()

    from app.services.app_settings import LEGION_LAST_SYNCED_KEY, get_setting
    last_synced = await get_setting(db, LEGION_LAST_SYNCED_KEY)

    return templates.TemplateResponse(
        "admin/roster.html",
        {"request": request, "active_page": "roster",
         "students": [m for m in members if m.kind == MemberKind.student],
         "mentors": [m for m in members if m.kind == MemberKind.mentor],
         "last_synced": last_synced,
         "legion_configured": bool(settings.legion_base_url and settings.legion_api_key),
         "message": request.query_params.get("message")},
    )


@router.post("/roster/sync")
async def roster_sync(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_admin(request):
        return redirect
    try:
        msg = f"Synced {await sync_roster(db, full=True)}"
    except LegionSyncError as e:
        msg = f"Sync failed: {e}"
    return _redirect("/admin/roster", msg)


# ── Audit log (admin only) ───────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
async def audit_log(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_admin(request):
        return redirect
    rows = (
        await db.execute(select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(200))
    ).scalars().all()
    return templates.TemplateResponse(
        "admin/audit.html", {"request": request, "active_page": "audit", "rows": rows}
    )


# ── Backup (admin only) ──────────────────────────────────────────────────────────

@router.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    if redirect := _require_admin(request):
        return redirect
    return templates.TemplateResponse(
        "admin/backup.html",
        {"request": request, "active_page": "backup", "backups": list_backups(),
         "is_sqlite": is_sqlite(), "message": request.query_params.get("message")},
    )


@router.post("/backup/snapshot")
async def backup_snapshot(request: Request):
    if redirect := _require_admin(request):
        return redirect
    try:
        nightly_backup()
        msg = "Snapshot created"
    except Exception:
        msg = "Snapshot failed"
    return _redirect("/admin/backup", msg)


@router.post("/backup/restore")
async def backup_restore(request: Request, file: UploadFile = File(...)):
    if redirect := _require_admin(request):
        return redirect
    ok, message = stage_restore(await file.read())
    return _redirect("/admin/backup", message)


@router.get("/backup/download/{name}")
async def backup_download(name: str, request: Request):
    if redirect := _require_admin(request):
        return redirect
    # Guard against path traversal — only a bare filename in the backup dir.
    safe = os.path.basename(name)
    path = os.path.join(settings.backup_dir, safe)
    if safe != name or not os.path.isfile(path):
        return _redirect("/admin/backup", "Not found")
    return FileResponse(path, filename=safe, media_type="application/octet-stream")


# ── Settings (admin only, read-only view) ────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if redirect := _require_admin(request):
        return redirect
    return templates.TemplateResponse(
        "admin/settings.html",
        {"request": request, "active_page": "settings", "settings": settings},
    )
