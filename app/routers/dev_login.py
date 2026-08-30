"""
Dev / preview sign-in — NOT part of the real auth flow.

Legion mints the shared `mw_sso` cookie scoped to `.marswars.org`, so it can never reach
a Virtus deployed on a *different* domain (a preview host like `dev2.tuckers-workshop.xyz`).
This router lets such a deploy mint its own `mw_sso` for its own host, standing in for a
real Legion sign-in — the same job `devlogin.py` does locally, but as a route the deploy
can expose.

It is mounted **only** when `settings.dev_login_secret` is set (see `app/main.py`), and
every request must present that exact secret. Leave `DEV_LOGIN_SECRET` unset in
production and this module is never imported.
"""
import secrets

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer

from app.config import settings
from app.services.sso import SSO_COOKIE, is_same_app_path

router = APIRouter(prefix="/dev-login", tags=["dev"])

# Same construction Legion uses for the real cookie, and the same one `services/sso.py`
# verifies with — salt included.
_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")


@router.get("")
async def dev_login(
    key: str = "",
    code: str = "dev00001",
    name: str = "Dev User",
    role: str = "student",
    groups: str = "",
    next: str = "/me",
):
    """`/dev-login?key=<DEV_LOGIN_SECRET>&code=<member_code>&groups=virtus-admin` → sets a
    host-only `mw_sso` cookie and drops you into the app. A wrong/absent key just bounces
    to `/` with no cookie."""
    if not settings.dev_login_secret or not secrets.compare_digest(key, settings.dev_login_secret):
        return RedirectResponse("/", status_code=303)

    token = _signer.dumps({
        "member_code": code,
        "username": "dev",
        "name": name,
        "role": role,
        "team_number": 4143,
        "groups": [g.strip() for g in groups.split(",") if g.strip()],
        "slack_user_id": None,
    })
    resp = RedirectResponse(next if is_same_app_path(next) else "/me", status_code=303)
    # No `domain=` — a host-only cookie, which is the whole point: it must land on *this*
    # deploy's host, not `.marswars.org`.
    resp.set_cookie(
        SSO_COOKIE, token, httponly=True, samesite="lax",
        max_age=settings.sso_session_ttl,
    )
    return resp
