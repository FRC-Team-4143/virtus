"""
HTTP-level gating: who reaches /admin, the manager path allowlist, and the magic-link
step-up.
"""
import pytest

from app.routers.admin import _manager_allowed
from tests.conftest import cookie_for, make_sso_cookie


ADMIN_PAGES = [
    "/admin", "/admin/team-goals", "/admin/cycles", "/admin/competencies",
    "/admin/roster", "/admin/audit", "/admin/backup", "/admin/settings",
]

# Sections a manager may reach, and the admin-only ones they may not.
MANAGER_OK = ["/admin", "/admin/team-goals", "/admin/cycles", "/admin/cycles/1", "/admin/students/x1"]
MANAGER_DENIED = ["/admin/competencies", "/admin/roster", "/admin/audit", "/admin/backup", "/admin/settings"]


@pytest.mark.parametrize("path", ADMIN_PAGES)
async def test_no_cookie_redirects_to_legion(client, path):
    resp = await client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert "/sso/authorize?app=virtus" in resp.headers["location"]


@pytest.mark.parametrize("path", ADMIN_PAGES)
async def test_a_member_without_groups_is_forbidden(client, path):
    client.cookies.set("mw_sso", make_sso_cookie(groups=[]))
    resp = await client.get(path)
    assert resp.status_code == 403


@pytest.mark.parametrize("path", MANAGER_DENIED)
def test_manager_allowlist_excludes_admin_only_sections(path):
    assert not _manager_allowed(path)


@pytest.mark.parametrize("path", MANAGER_OK)
def test_manager_allowlist_includes_the_manager_sections(path):
    assert _manager_allowed(path)


def test_manager_allowlist_excludes_unsubmit():
    """Reopening a submitted review is admin-only even though it sits under /admin/cycles,
    which managers otherwise reach."""
    assert _manager_allowed("/admin/cycles/1")
    assert not _manager_allowed("/admin/cycles/1/reviews/2/unsubmit")


async def test_a_manager_is_denied_an_admin_only_page(client, manager_cookie):
    client.cookies.set("mw_sso", manager_cookie)
    assert (await client.get("/admin/competencies")).status_code == 403
    assert (await client.get("/admin/settings")).status_code == 403


async def test_a_manager_reaches_team_goals_and_cycles(client, manager_cookie, competencies):
    client.cookies.set("mw_sso", manager_cookie)
    assert (await client.get("/admin/team-goals")).status_code == 200
    assert (await client.get("/admin/cycles")).status_code == 200


async def test_an_admin_reaches_everything(client, admin_cookie, competencies):
    client.cookies.set("mw_sso", admin_cookie)
    for path in ADMIN_PAGES:
        assert (await client.get(path)).status_code == 200, path


async def test_a_magic_link_identity_is_stepped_up_not_forbidden(client):
    """A magic-link cookie carries no groups by construction. The person may well be an
    admin who arrived from Slack, so send them through `/sso/stepup` (fresh Approve/Deny,
    re-mint with groups) rather than a 403 — and not `/sso/authorize`, which would just
    bounce the link cookie back and loop."""
    client.cookies.set("mw_sso", make_sso_cookie(groups=[], via="link"))
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert "/sso/stepup?app=virtus" in resp.headers["location"]


async def test_magic_link_session_sees_the_stepup_banner(client, db, make_member, competencies, monkeypatch):
    """A `via="link"` session (Slack quick link) gets a visible offer to step up to a
    full sign-in from the portal, without signing out first."""
    from app.config import settings
    monkeypatch.setattr(settings, "legion_base_url", "https://legion.example.org")
    member = await make_member("Sara Student")
    client.cookies.set("mw_sso", cookie_for(member, via="link"))

    resp = await client.get("/me")

    assert resp.status_code == 200
    assert "https://legion.example.org/sso/stepup?app=virtus" in resp.text
    assert "quick link" in resp.text


async def test_normal_session_has_no_stepup_banner(client, db, make_member, competencies, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "legion_base_url", "https://legion.example.org")
    member = await make_member("Sara Student")
    client.cookies.set("mw_sso", cookie_for(member))

    resp = await client.get("/me")

    assert "/sso/stepup" not in resp.text


async def test_a_garbage_cookie_is_treated_as_signed_out(client):
    client.cookies.set("mw_sso", "not-a-real-token")
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert "/sso/authorize" in resp.headers["location"]


async def test_health_needs_no_auth(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "app": "virtus"}


async def test_authorize_url_sends_an_absolute_return_to(client):
    """Legion's /sso/complete redirects to return_to as-is; a bare path would resolve
    against Legion's own host instead of Virtus's."""
    from urllib.parse import parse_qs, urlparse
    from app.config import settings
    settings.base_url = "https://virtus.example.org"

    resp = await client.get("/enter?next=/me/reviews", follow_redirects=False)
    return_to = parse_qs(urlparse(resp.headers["location"]).query)["return_to"][0]
    assert return_to == "https://virtus.example.org/me/reviews"
