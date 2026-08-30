"""
Slack notifications — the announcements and reminders Virtus sends out.

Every link into Virtus is a **magic link** (`legion_auth.make_link_url`), not a bare URL:
Slack's in-app browser throws cookies away between opens, so a plain link would face the
recipient with a fresh Approve/Deny push every single time. A magic link is a bearer
credential, which is exactly why these only ever go into DMs — never the announce channel,
where anyone could redeem someone else's.

Called from `BackgroundTasks` and the scheduler, always *after* the request's commit: a
Slack outage must never roll back the change that triggered the message.
"""
import logging
from typing import Iterable, Optional, Sequence

from app.config import settings
from app.models import Member, ReviewAssignment, ReviewKind, ReviewStatus
from app.services.legion_auth import make_link_url
from app.services.slack_client import post_to_channel, send_dm

log = logging.getLogger(__name__)


def _link(member: Member, next_path: str) -> str:
    return make_link_url(member.member_code, next_path) if member.member_code else \
        f"{settings.base_url}{next_path}"


async def _dm(member: Optional[Member], text: str) -> bool:
    """DM a member, skipping anyone with no Slack account linked. Returns whether it went.

    Failures are logged, never raised — one member with a stale Slack id must not abort a
    whole cycle's announcements.
    """
    if member is None or not member.slack_user_id:
        return False
    try:
        await send_dm(member.slack_user_id, text, automated=True)
        return True
    except Exception:  # noqa: BLE001 — a Slack error is never worth failing the caller
        log.exception("Failed to DM %s", member.name)
        return False


async def notify_cycle_opened(
    cycle_name: str, cycle_id: int, assignments: Sequence[ReviewAssignment]
) -> int:
    """Tell everyone a cycle just opened: students that their self-review is due, and
    reviewers what they owe. Someone who is both gets both messages — they're two
    genuinely different pieces of work."""
    if not settings.updates_enabled:
        return 0

    sent = 0
    owed: dict[int, tuple[Member, list[str]]] = {}
    for a in assignments:
        sent += await _dm(a.member, (
            f"📋 *{cycle_name}* is open. Time to write your self-review.\n"
            f"<{_link(a.member, f'/me/review/{cycle_id}')}|Write your self-review>"
        ))
        if a.reviewer is not None:
            member, names = owed.setdefault(a.reviewer.id, (a.reviewer, []))
            names.append(a.member.name)

    for reviewer, names in owed.values():
        people = ", ".join(sorted(names))
        sent += await _dm(reviewer, (
            f"📝 *{cycle_name}* is open. You're the reviewer for {len(names)} "
            f"teammate(s): {people}.\n"
            f"<{_link(reviewer, '/me/reviews')}|Write your reviews>"
        ))

    if settings.slack_announce_channel:
        try:
            # No magic link here — a channel post reaches everyone, and a magic link is a
            # bearer credential for one person.
            await post_to_channel(settings.slack_announce_channel, (
                f"📋 Review cycle *{cycle_name}* is now open. "
                f"Everyone has been DM'd their part. {settings.base_url}/me"
            ))
        except Exception:  # noqa: BLE001
            log.exception("Failed to announce cycle %s", cycle_name)
    return sent


async def notify_outstanding(
    cycle_name: str, outstanding: Iterable[tuple[Member, str]]
) -> int:
    """Nudge everyone who still owes something.

    Groups by person first so someone behind on three reviews gets one message listing
    all three, not three separate pings.
    """
    if not settings.updates_enabled:
        return 0

    grouped: dict[int, tuple[Member, list[str]]] = {}
    for member, what in outstanding:
        _, items = grouped.setdefault(member.id, (member, []))
        items.append(what)

    sent = 0
    for member, items in grouped.values():
        bullets = "\n".join(f"• {item}" for item in sorted(items))
        sent += await _dm(member, (
            f"⏰ Still outstanding for *{cycle_name}*:\n{bullets}\n"
            f"<{_link(member, '/me')}|Finish up>"
        ))
    return sent
