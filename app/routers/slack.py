"""
Slack routes.

  POST /slack/command — the `/virtus` slash command (the caller's open goals, whatever
                        they still owe on an open cycle, and a one-tap link in)

Verified by the Slack signing secret. There are **no interactive components** — every
action is a form on the web — so Legion needs no `slack_dispatch.py` entry for Virtus.
"""
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import GoalStatus, Member, ReviewKind, ReviewStatus
from app.services import cycles as cycle_service, goals as goal_service
from app.services.legion_auth import make_link_url

router = APIRouter(prefix="/slack")


# ── Signature verification ─────────────────────────────────────────────────────

async def _verify_slack_signature(request: Request) -> bytes:
    """Read raw body and verify Slack request signature. Raises 403 on failure."""
    if not settings.slack_signing_secret:
        raise HTTPException(
            status_code=503, detail="Slack integration is not configured (no signing secret set)."
        )

    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    try:
        if abs(time.time() - float(timestamp)) > 300:
            raise HTTPException(status_code=403, detail="Request too old")
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid timestamp")

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        settings.slack_signing_secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid Slack signature")
    return body


# ── Slash command ──────────────────────────────────────────────────────────────

@router.post("/command")
async def slack_command(request: Request, db: AsyncSession = Depends(get_db)):
    await _verify_slack_signature(request)

    form = await request.form()
    user_id = form.get("user_id", "")

    member = (
        await db.execute(select(Member).where(Member.slack_user_id == user_id))
    ).scalars().first()
    if not member:
        return JSONResponse({
            "response_type": "ephemeral",
            "text": "❌ Your Slack account isn't linked to a roster record yet. Ask an admin.",
        })

    lines: list[str] = []

    goals = [
        g for g in await goal_service.list_student_goals(db, member.id)
        if g.status != GoalStatus.done
    ]
    if goals:
        lines.append("*Your open goals*")
        lines += [f"• {g.title} — _{g.status.value.replace('_', ' ')}_" for g in goals[:5]]
        if len(goals) > 5:
            lines.append(f"…and {len(goals) - 5} more.")
    else:
        lines.append("You have no open goals for this season yet.")

    # What they still owe: their own self-review, plus any reviews assigned to them.
    todo: list[str] = []
    for cycle in await cycle_service.open_cycles(db):
        assignment = await cycle_service.get_assignment_for_member(db, cycle.id, member.id)
        if assignment:
            review = assignment.review_of(ReviewKind.self_review)
            if not (review and review.status == ReviewStatus.submitted):
                todo.append(f"your self-review for *{cycle.name}*")
    for a in await cycle_service.assignments_for_reviewer(db, member.id):
        review = a.review_of(ReviewKind.reviewer)
        if not (review and review.status == ReviewStatus.submitted):
            todo.append(f"your review of *{a.member.name}*")
    if todo:
        lines.append("")
        lines.append("*Still to do*")
        lines += [f"• {item}" for item in todo]

    # A magic link, not a bare URL: an ephemeral slash-command reply is visible only to
    # the caller, and Slack's in-app browser drops cookies between opens.
    lines.append("")
    lines.append(f"<{make_link_url(member.member_code, '/me')}|Open Virtus>")

    text = "\n".join(lines)
    return JSONResponse({
        "response_type": "ephemeral",
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    })
