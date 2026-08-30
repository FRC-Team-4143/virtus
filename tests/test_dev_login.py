"""The dev/preview sign-in shim must be invisible unless explicitly switched on."""
from app.config import settings


async def test_dev_login_is_not_mounted_without_a_secret(client):
    """`app` is built at import time with `dev_login_secret` unset (its default), so the
    route isn't registered at all — a request 404s rather than minting anything."""
    assert not settings.dev_login_secret  # the isolated-settings default
    resp = await client.get("/dev-login?key=whatever&code=x&groups=virtus-admin")
    assert resp.status_code == 404


def test_dev_login_handler_checks_the_secret_and_signs_a_real_cookie(monkeypatch):
    """Mount the router by hand and exercise it: a wrong key mints nothing; the right key
    mints an `mw_sso` the app's own verifier accepts."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "dev_login_secret", "let-me-in")
    from app.routers import dev_login
    from app.services.sso import read_sso_token

    mini = FastAPI()
    mini.include_router(dev_login.router)
    tc = TestClient(mini)

    bad = tc.get("/dev-login?key=nope&code=abc", follow_redirects=False)
    assert bad.status_code == 303 and "mw_sso" not in bad.cookies

    ok = tc.get("/dev-login?key=let-me-in&code=abc123&groups=virtus-admin,virtus-manager",
                follow_redirects=False)
    assert ok.status_code == 303
    claims = read_sso_token(ok.cookies["mw_sso"])
    assert claims["member_code"] == "abc123"
    assert set(claims["groups"]) == {"virtus-admin", "virtus-manager"}
