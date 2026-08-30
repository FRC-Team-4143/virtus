"""The `/virtus` slash command."""
import hashlib
import hmac
import time

from app.config import settings
from app.services import goals as goal_service


def _signed_headers(body: str) -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(
        settings.slack_signing_secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def _post(client, body: str):
    return await client.post("/slack/command", content=body, headers=_signed_headers(body))


async def test_an_unsigned_request_is_rejected(client):
    settings.slack_signing_secret = "test-signing-secret"
    resp = await client.post(
        "/slack/command", content="user_id=U1",
        headers={"X-Slack-Request-Timestamp": str(int(time.time())),
                 "X-Slack-Signature": "v0=deadbeef",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 403


async def test_an_unlinked_slack_user_is_told_to_ask_an_admin(client, make_member):
    settings.slack_signing_secret = "test-signing-secret"
    resp = await _post(client, "user_id=UNKNOWN")
    assert resp.status_code == 200
    assert "isn't linked to a roster record" in resp.json()["text"]


async def test_the_command_lists_open_goals_and_outstanding_reviews(
    client, db, make_member, competencies, make_cycle
):
    from app.models import GoalStatus
    settings.slack_signing_secret = "test-signing-secret"
    student = await make_member("Sara Student", slack_user_id="USARA")
    await goal_service.create_student_goal(db, student, title="Learn the CNC")
    await goal_service.create_student_goal(
        db, student, title="Already finished", status=GoalStatus.done
    )
    await db.commit()
    cycle = await make_cycle("Midpoint")

    resp = await _post(client, "user_id=USARA")
    text = resp.json()["text"]

    assert "Learn the CNC" in text
    assert "Already finished" not in text          # done goals are not "open"
    assert "your self-review for *Midpoint*" in text
    # The link is a Legion magic link, not a bare URL — Slack's browser drops cookies.
    assert "/sso/link?token=" in text


async def test_a_student_with_nothing_outstanding_gets_a_clean_reply(
    client, db, make_member, competencies
):
    settings.slack_signing_secret = "test-signing-secret"
    await make_member("Sara Student", slack_user_id="USARA")
    resp = await _post(client, "user_id=USARA")
    text = resp.json()["text"]
    assert "no open goals" in text
    assert "Still to do" not in text
