"""
End-to-end over HTTP: goals, a cycle, a self-review, and a reviewer review written by a
student subteam lead who holds no Legion group at all.
"""
from sqlalchemy import select

from app.models import (
    CycleStatus, MemberKind, Review, ReviewCycle, ReviewKind, ReviewStatus, TeamGoal,
)
from app.services import cycles as cycle_service
from tests.conftest import cookie_for


async def _open_cycle_via_http(client, admin_cookie, db, name="Midpoint"):
    client.cookies.set("mw_sso", admin_cookie)
    resp = await client.post(
        "/admin/cycles",
        data={"name": name, "season": "2026", "closes_at": "2026-12-31"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cycle = (await db.execute(select(ReviewCycle).where(ReviewCycle.name == name))).scalars().first()
    return cycle


# ── Team goals ───────────────────────────────────────────────────────────────────

async def test_admin_creates_a_team_goal_and_members_see_it(
    client, admin_cookie, db, make_member, competencies
):
    student = await make_member("Sara Student", subteam_slug="design", subteam_label="Design")

    client.cookies.set("mw_sso", admin_cookie)
    resp = await client.post(
        "/admin/team-goals",
        data={"title": "Win a blue banner", "season": "2026",
              "category": "award", "team": "4143",
              "status": "on_track", "description": "Any regional."},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    goal = (await db.execute(select(TeamGoal))).scalars().first()
    assert goal.title == "Win a blue banner"
    assert goal.category.label == "Award"
    assert goal.team.label == "4143"

    # A plain member sees it on the board, read-only, under its category heading.
    client.cookies.set("mw_sso", cookie_for(student))
    board = await client.get("/")
    assert board.status_code == 200
    assert "Win a blue banner" in board.text
    assert "Award" in board.text


# ── The full review flow ─────────────────────────────────────────────────────────

async def test_a_group_less_student_lead_reviews_their_teammate(
    client, admin_cookie, db, session_factory, make_member, competencies
):
    student = await make_member("Sara Student", subteam_slug="design", subteam_label="Design",
                                slack_user_id="USARA")
    lead = await make_member("Lena Lead", subteam_slug="design", subteam_label="Design",
                             slack_user_id="ULENA")
    assert lead.kind == MemberKind.student and lead.group_slugs is None

    cycle = await _open_cycle_via_http(client, admin_cookie, db)
    assert cycle.status == CycleStatus.draft

    # Assign the lead as Sara's reviewer, then open the cycle.
    assignment = await cycle_service.get_assignment_for_member(db, cycle.id, student.id)
    resp = await client.post(
        f"/admin/cycles/{cycle.id}/assign",
        data={"assignment_id": assignment.id, "reviewer_id": lead.id},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = await client.post(f"/admin/cycles/{cycle.id}/open", follow_redirects=False)
    assert resp.status_code == 303

    # ── The student writes their self-review ──
    client.cookies.set("mw_sso", cookie_for(student))
    form = await client.get(f"/me/review/{cycle.id}")
    assert form.status_code == 200
    assert "Your self-review" in form.text

    self_review = (await db.execute(
        select(Review).where(Review.kind == ReviewKind.self_review)
    )).scalars().first()
    from app.services import reviews as review_service
    ratings = await review_service.load_ratings(db, self_review)

    payload = {f"score_{r.cycle_competency_id}": "2" for r in ratings}
    payload["strengths"] = "I show up."
    payload["action"] = "submit"

    # Can't submit while short of the 2-goal minimum — the draft is kept, not lost.
    resp = await client.post(f"/me/review/{cycle.id}", data=payload, follow_redirects=False)
    assert resp.status_code == 400
    assert "personal goals" in resp.text
    await db.refresh(self_review)
    assert self_review.status == ReviewStatus.draft
    assert self_review.strengths == "I show up."          # work was saved

    # Set the two goals, then the same submit goes through.
    goal_note = "Run it solo before the first competition."
    for title, desc in (("Learn the CNC", goal_note), ("Speak up in design reviews", "")):
        assert (await client.post("/me/goals", data={"title": title, "description": desc},
                                  follow_redirects=False)).status_code == 303
    resp = await client.post(f"/me/review/{cycle.id}", data=payload, follow_redirects=False)
    assert resp.status_code == 303

    await db.refresh(self_review)
    assert self_review.status == ReviewStatus.submitted

    # ── The lead writes theirs — with no admin access whatsoever ──
    client.cookies.set("mw_sso", cookie_for(lead))
    assert (await client.get("/admin")).status_code == 403  # genuinely not staff

    owed = await client.get("/me/reviews")
    assert owed.status_code == 200
    assert "Sara Student" in owed.text

    form = await client.get(f"/me/reviews/{assignment.id}")
    assert form.status_code == 200
    assert "Review of Sara Student" in form.text
    # The subject's goals show as context — title AND the notes written with them — and the
    # self-review does NOT leak into the form.
    assert "Learn the CNC" in form.text
    assert goal_note in form.text
    assert "I show up." not in form.text

    reviewer_review = (await db.execute(
        select(Review).where(Review.kind == ReviewKind.reviewer)
    )).scalars().first()
    r_ratings = await review_service.load_ratings(db, reviewer_review)
    payload = {f"score_{r.cycle_competency_id}": "4" for r in r_ratings}
    payload["strengths"] = "Sara is underrating herself."
    payload["private_notes"] = "Flagging for a lead role — keep this off her feedback."
    payload["action"] = "submit"
    resp = await client.post(
        f"/me/reviews/{assignment.id}", data=payload, follow_redirects=False
    )
    assert resp.status_code == 303

    await db.refresh(reviewer_review)
    assert reviewer_review.status == ReviewStatus.submitted
    assert reviewer_review.private_notes == "Flagging for a lead role — keep this off her feedback."

    # ── The student sees the side-by-side, gap and all — but NOT the private notes ──
    client.cookies.set("mw_sso", cookie_for(student))
    result = await client.get(f"/me/review/{cycle.id}/result")
    assert result.status_code == 200
    assert "Sara is underrating herself." in result.text
    assert "+2" in result.text
    assert "keep this off her feedback" not in result.text

    # ── Staff see them on the student's profile ──
    client.cookies.set("mw_sso", admin_cookie)
    profile = await client.get(f"/admin/students/{student.member_code}")
    assert profile.status_code == 200
    assert "keep this off her feedback" in profile.text

    # ── The cycle dashboard now counts Sara as complete ──
    client.cookies.set("mw_sso", admin_cookie)
    detail = await client.get(f"/admin/cycles/{cycle.id}")
    assert detail.status_code == 200

    # Each HTTP request above used its own session; this long-lived test session still
    # holds the pre-review versions of those rows in its identity map. Read the counts
    # through a fresh session, the way a real request does.
    async with session_factory() as fresh:
        stats = cycle_service.completion(await cycle_service.load_assignments(fresh, cycle.id))
    # Lena is a student too, so the roster is two people — only Sara's pair is done.
    assert stats["total"] == 2
    assert stats["both_done"] == 1
    assert stats["self_done"] == 1 and stats["reviewer_done"] == 1
    assert stats["percent"] == 50


async def test_a_bystander_cannot_open_someone_elses_reviewer_form(
    client, admin_cookie, db, make_member, competencies
):
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    bystander = await make_member("Bo Bystander")

    cycle = await _open_cycle_via_http(client, admin_cookie, db)
    assignment = await cycle_service.get_assignment_for_member(db, cycle.id, student.id)
    await client.post(
        f"/admin/cycles/{cycle.id}/assign",
        data={"assignment_id": assignment.id, "reviewer_id": lead.id}, follow_redirects=False,
    )
    await client.post(f"/admin/cycles/{cycle.id}/open", follow_redirects=False)

    client.cookies.set("mw_sso", cookie_for(bystander))
    resp = await client.get(f"/me/reviews/{assignment.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert "/me/reviews" in resp.headers["location"]
    # No review row was created for the bystander.
    assert (await db.execute(select(Review))).scalars().all() == []


async def test_a_student_cannot_edit_another_students_goal(client, db, make_member):
    from app.services import goals as goal_service
    mine = await make_member("Sara Student")
    theirs = await make_member("Sam Student")
    goal = await goal_service.create_student_goal(db, mine, title="Learn the CNC")
    await db.commit()

    client.cookies.set("mw_sso", cookie_for(theirs))
    resp = await client.post(
        f"/me/goals/{goal.id}/edit",
        data={"title": "Hijacked", "status": "done"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(goal)
    assert goal.title == "Learn the CNC"

    resp = await client.post(f"/me/goals/{goal.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert await goal_service.get_student_goal_for(db, goal.id, mine.id) is not None


async def test_opening_a_cycle_dms_the_roster(
    client, admin_cookie, db, make_member, competencies, fake_slack
):
    student = await make_member("Sara Student", slack_user_id="USARA")
    lead = await make_member("Lena Lead", slack_user_id="ULENA")

    cycle = await _open_cycle_via_http(client, admin_cookie, db)
    assignment = await cycle_service.get_assignment_for_member(db, cycle.id, student.id)
    await client.post(
        f"/admin/cycles/{cycle.id}/assign",
        data={"assignment_id": assignment.id, "reviewer_id": lead.id}, follow_redirects=False,
    )
    await client.post(f"/admin/cycles/{cycle.id}/open", follow_redirects=False)

    texts = "\n".join(d["text"] for d in fake_slack.dms)
    # Both students are on the roster, so both get a self-review nudge...
    assert texts.count("Time to write your self-review") == 2
    # ...and Lena additionally gets her reviewer list.
    assert "You're the reviewer for 1 teammate(s): Sara Student" in texts


async def test_an_incomplete_submit_re_renders_the_form_and_keeps_the_draft(
    client, admin_cookie, db, session_factory, make_member, competencies
):
    """A refused submit must not 500, and must not discard what was typed."""
    student = await make_member("Sara Student")
    cycle = await _open_cycle_via_http(client, admin_cookie, db)
    await client.post(f"/admin/cycles/{cycle.id}/open", follow_redirects=False)

    client.cookies.set("mw_sso", cookie_for(student))
    await client.get(f"/me/review/{cycle.id}")  # creates the draft + rating rows

    from app.services import reviews as review_service
    assignment = await cycle_service.get_assignment_for_member(db, cycle.id, student.id)
    review = assignment.review_of(ReviewKind.self_review)
    ratings = await review_service.load_ratings(db, review)

    # Answer only the first competency, then hit Submit.
    resp = await client.post(
        f"/me/review/{cycle.id}",
        data={f"score_{ratings[0].cycle_competency_id}": "3",
              "strengths": "Half-finished thought.", "action": "submit"},
    )
    assert resp.status_code == 400
    assert "Rate every competency before submitting" in resp.text
    assert ratings[1].competency.name in resp.text

    # The draft survived: still a draft, but holding what was typed.
    async with session_factory() as fresh:
        saved = (await fresh.execute(select(Review))).scalars().first()
        assert saved.status == ReviewStatus.draft
        assert saved.strengths == "Half-finished thought."
        kept = await review_service.load_ratings(fresh, saved)
        assert kept[0].score == 3
        assert kept[1].score is None


async def test_a_closed_cycle_tells_the_reviewer_the_real_reason(
    client, admin_cookie, db, make_member, competencies
):
    """An assigned reviewer must not be told their own assignment "isn't assigned to you"
    just because the cycle closed."""
    student = await make_member("Sara Student")
    lead = await make_member("Lena Lead")
    bystander = await make_member("Bo Bystander")

    cycle = await _open_cycle_via_http(client, admin_cookie, db)
    assignment = await cycle_service.get_assignment_for_member(db, cycle.id, student.id)
    await client.post(
        f"/admin/cycles/{cycle.id}/assign",
        data={"assignment_id": assignment.id, "reviewer_id": lead.id}, follow_redirects=False,
    )
    await client.post(f"/admin/cycles/{cycle.id}/open", follow_redirects=False)
    await client.post(f"/admin/cycles/{cycle.id}/close", follow_redirects=False)

    client.cookies.set("mw_sso", cookie_for(lead))
    resp = await client.get(f"/me/reviews/{assignment.id}", follow_redirects=False)
    assert "no%20longer%20open" in resp.headers["location"]

    # A bystander still gets the non-committal answer, which leaks nothing.
    client.cookies.set("mw_sso", cookie_for(bystander))
    resp = await client.get(f"/me/reviews/{assignment.id}", follow_redirects=False)
    assert "isn%27t%20assigned%20to%20you" in resp.headers["location"]
