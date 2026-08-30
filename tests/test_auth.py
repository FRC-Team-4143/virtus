"""
HTTP-level gating: who reaches /admin, the manager path allowlist, and the magic-link
step-up.
"""
import pytest

from app.routers.admin import _manager_allowed
from tests.conftest import make_sso_cookie


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
    admin who arrived from Slack, so send them to a real sign-in rather than a 403."""
    client.cookies.set("mw_sso", make_sso_cookie(groups=[], via="link"))
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert "/sso/authorize" in resp.headers["location"]


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
