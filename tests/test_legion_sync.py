"""
Roster + subteam mirroring against Legion's `/api/members` and `/api/subteams` payload
shapes (see legion/app/services/members.py's serializers).
"""
from sqlalchemy import select

from app.models import Member, MemberKind, Subteam
from app.services.legion_sync import _upsert_members, _upsert_subteams


def _member(code, name, **over):
    payload = {
        "member_code": code, "name": name, "role": "student",
        "team_number": 4143, "team_name": "MARS", "subteam": None,
        "groups": [], "slack_user_id": None, "is_active": True,
        "grade": None, "graduation_year": None, "years_on_team": 1,
        "updated_at": "2026-08-01T00:00:00",
    }
    payload.update(over)
    return payload


async def test_members_upsert_with_subteam_and_grade(db):
    await _upsert_members(db, [
        _member("aaa11111", "Sara Student",
                subteam={"slug": "design", "label": "Design"},
                grade="junior", graduation_year=2028, slack_user_id="U1"),
        _member("bbb22222", "Mo Mentor", role="mentor"),
    ])

    rows = {m.name: m for m in (await db.execute(select(Member))).scalars().all()}
    assert rows["Sara Student"].subteam_slug == "design"
    assert rows["Sara Student"].subteam_label == "Design"
    assert rows["Sara Student"].subteam_display == "Design"
    assert rows["Sara Student"].grade == "junior"
    assert rows["Sara Student"].graduation_year == 2028
    assert rows["Mo Mentor"].kind == MemberKind.mentor
    assert rows["Mo Mentor"].subteam_display == "—"


async def test_a_member_moving_subteam_is_mirrored(db):
    await _upsert_members(db, [
        _member("aaa11111", "Sara Student", subteam={"slug": "design", "label": "Design"})
    ])
    await _upsert_members(db, [
        _member("aaa11111", "Sara Student", subteam={"slug": "software", "label": "Software"})
    ])

    rows = (await db.execute(select(Member))).scalars().all()
    assert len(rows) == 1  # upserted, not duplicated
    assert rows[0].subteam_slug == "software"


async def test_group_slugs_flatten_from_either_payload_shape(db):
    await _upsert_members(db, [
        _member("aaa11111", "Dict Form", groups=[{"slug": "virtus-admin", "label": "X"}]),
        _member("bbb22222", "Slug Form", groups=["virtus-manager"]),
    ])
    rows = {m.name: m for m in (await db.execute(select(Member))).scalars().all()}
    assert rows["Dict Form"].has_group("virtus-admin")
    assert rows["Slug Form"].has_group("virtus-manager")
    assert not rows["Slug Form"].has_group("virtus-admin")


async def test_a_legacy_row_is_backlinked_by_slack_id_then_name(db):
    db.add(Member(member_code=None, name="Sara Student", slack_user_id="U1"))
    db.add(Member(member_code=None, name="Nick Nameonly"))
    await db.commit()

    await _upsert_members(db, [
        _member("aaa11111", "Different Name Now", slack_user_id="U1"),
        _member("bbb22222", "nick nameonly"),  # case-insensitive name match
    ])

    rows = (await db.execute(select(Member))).scalars().all()
    assert len(rows) == 2  # both back-linked, neither duplicated
    assert {r.member_code for r in rows} == {"aaa11111", "bbb22222"}


async def test_an_inactive_member_gets_an_archived_at(db):
    await _upsert_members(db, [_member("aaa11111", "Gone Grad", is_active=False)])
    row = (await db.execute(select(Member))).scalars().first()
    assert row.is_active is False
    assert row.archived_at is not None


async def test_subteams_upsert_and_deactivate_when_they_vanish(db):
    count = await _upsert_subteams(db, [
        {"slug": "design", "label": "Design", "is_active": True},
        {"slug": "pit", "label": "Pit Crew", "is_active": True},
    ])
    await db.commit()
    assert count == 2

    # Legion drops "pit" from its list entirely.
    await _upsert_subteams(db, [{"slug": "design", "label": "Design Team", "is_active": True}])
    await db.commit()

    rows = {s.slug: s for s in (await db.execute(select(Subteam))).scalars().all()}
    assert rows["design"].label == "Design Team"   # relabelled
    assert rows["pit"].is_active is False          # deactivated...
    assert rows["pit"].label == "Pit Crew"         # ...but kept, so old goals still read


async def test_a_subteam_without_a_slug_is_skipped(db):
    await _upsert_subteams(db, [{"label": "Nameless"}, {"slug": "ok", "label": "Fine"}])
    await db.commit()
    rows = (await db.execute(select(Subteam))).scalars().all()
    assert [r.slug for r in rows] == ["ok"]
