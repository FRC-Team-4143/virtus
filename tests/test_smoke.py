"""Every page renders for a signed-in admin, and the seed runs exactly once."""
import pytest
from sqlalchemy import select

from app.database import seed_competencies
from app.models import Competency, DEFAULT_COMPETENCIES
from tests.conftest import cookie_for


async def test_seed_is_idempotent_and_does_not_resurrect_deletions(db):
    assert await seed_competencies(db) == len(DEFAULT_COMPETENCIES)
    assert await seed_competencies(db) == 0

    # An admin removing one they don't want must not have it reappear on the next boot.
    rows = (await db.execute(select(Competency))).scalars().all()
    await db.delete(rows[0])
    await db.commit()
    assert await seed_competencies(db) == 0
    assert len((await db.execute(select(Competency))).scalars().all()) == len(rows) - 1


@pytest.mark.parametrize("path", [
    "/admin", "/admin/team-goals", "/admin/cycles", "/admin/competencies",
    "/admin/roster", "/admin/audit", "/admin/backup", "/admin/settings",
])
async def test_admin_pages_render(client, admin_cookie, competencies, path):
    client.cookies.set("mw_sso", admin_cookie)
    resp = await client.get(path)
    assert resp.status_code == 200
    assert "Virtus" in resp.text


@pytest.mark.parametrize("path", ["/", "/me", "/me/reviews"])
async def test_portal_pages_render_for_a_member(client, db, make_member, competencies, path):
    member = await make_member("Sara Student")
    client.cookies.set("mw_sso", cookie_for(member))
    resp = await client.get(path)
    assert resp.status_code == 200


@pytest.mark.parametrize("path", ["/", "/me", "/me/reviews"])
async def test_portal_pages_ask_a_stranger_to_sign_in(client, path):
    """A Legion identity with no matching roster row still gets the identify page, not a
    crash — that's the "synced to Legion but not to Virtus yet" case."""
    from tests.conftest import make_sso_cookie
    client.cookies.set("mw_sso", make_sso_cookie(member_code="nobody01"))
    resp = await client.get(path, follow_redirects=False)
    assert resp.status_code in (200, 303)


async def test_the_cycle_detail_page_renders(client, admin_cookie, db, make_member, make_cycle, competencies):
    await make_member("Sara Student", subteam_slug="design", subteam_label="Design")
    cycle = await make_cycle("Midpoint")
    client.cookies.set("mw_sso", admin_cookie)
    resp = await client.get(f"/admin/cycles/{cycle.id}")
    assert resp.status_code == 200
    assert "Sara Student" in resp.text
    assert "Midpoint" in resp.text


async def test_the_student_profile_page_renders(client, admin_cookie, db, make_member, make_cycle, competencies):
    member = await make_member("Sara Student")
    await make_cycle("Midpoint")
    client.cookies.set("mw_sso", admin_cookie)
    resp = await client.get(f"/admin/students/{member.member_code}")
    assert resp.status_code == 200
    assert "Sara Student" in resp.text
    assert "Midpoint" in resp.text


def test_backup_validation_expects_virtus_tables():
    """A restore is validated against Virtus's own schema, so a sibling app's backup
    (or any unrelated SQLite file) can't be swapped in over the live database."""
    import sqlite3
    import tempfile
    from pathlib import Path
    from app.services.backup import REQUIRED_TABLES, validate_sqlite_file
    from app.database import Base

    # Every required table must actually exist in the models, or the check is unfalsifiable.
    assert REQUIRED_TABLES <= set(Base.metadata.tables)

    with tempfile.TemporaryDirectory() as d:
        ours = Path(d) / "ours.db"
        conn = sqlite3.connect(ours)
        for t in REQUIRED_TABLES:
            conn.execute(f"CREATE TABLE {t} (id INTEGER)")
        conn.commit(); conn.close()
        assert validate_sqlite_file(str(ours))

        # A Merces backup has none of these.
        theirs = Path(d) / "theirs.db"
        conn = sqlite3.connect(theirs)
        for t in ("members", "transactions", "store_items", "redemptions"):
            conn.execute(f"CREATE TABLE {t} (id INTEGER)")
        conn.commit(); conn.close()
        assert not validate_sqlite_file(str(theirs))
