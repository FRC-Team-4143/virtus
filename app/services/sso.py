"""
SSO identity — the signed `mw_sso` browser cookie shared across MARS/WARS apps.

Legion mints `mw_sso` once a member approves a Slack push; every sibling app verifies it
locally with the shared `sso_secret` — no callback to Legion. Virtus is a *consumer*: it
only ever **verifies** the cookie (never mints one). Single sign-out is a redirect to
Legion's `/sso/logout`.

Claims carried by the cookie (see Legion's `make_sso_token`):
    member_code, username, name, role, team_number, groups (list of slugs), slack_user_id
`/admin` is gated on the `virtus-admin`/`virtus-manager` slugs being present in `groups`;
the portal (`/me`, `/store`, `/orders`) is open to any active roster member.
"""
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

SSO_COOKIE = "mw_sso"

ADMIN_GROUP = "virtus-admin"
MANAGER_GROUP = "virtus-manager"

_sso_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")


def read_sso_token(token: Optional[str]) -> Optional[dict]:
    """The verified claims for a raw cookie value, or None if absent/invalid/expired."""
    if not token:
        return None
    try:
        return _sso_signer.loads(token, max_age=settings.sso_session_ttl)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def sso_identity(request: Request) -> Optional[dict]:
    """The verified SSO claims for the current request, or None if absent/invalid."""
    return read_sso_token(request.cookies.get(SSO_COOKIE))


def identity_groups(identity: Optional[dict]) -> set[str]:
    return set(identity.get("groups") or []) if identity else set()


def is_admin(identity: Optional[dict]) -> bool:
    return ADMIN_GROUP in identity_groups(identity)


def is_link_identity(identity: Optional[dict]) -> bool:
    """True for the weaker magic-link identity (`via="link"`), which Legion mints with an
    empty `groups` list so a forwarded Slack link can never reach /admin. Callers step it
    up to a full sign-in rather than returning 403 — the person may well be an admin, they
    just arrived by a route that can't prove it."""
    return bool(identity) and identity.get("via") == "link"


def is_staff(identity: Optional[dict]) -> bool:
    """Admin OR manager — the tier allowed into /admin at all."""
    return bool(identity_groups(identity) & {ADMIN_GROUP, MANAGER_GROUP})


def make_authorize_url(request: Request, *, return_to: Optional[str] = None) -> str:
    """Where to send an unauthenticated caller to sign in: Legion's `/sso/authorize`.

    Defaults `return_to` to the current page (the right behaviour for "you hit a
    protected page cold"); pass it explicitly for the portal's `/enter` bootstrap, which
    wants to land somewhere other than `/enter` itself.

    An explicit `return_to` is always a bare Virtus-relative path, so it must be made
    absolute here before handing it to Legion — otherwise Legion's `/sso/complete` issues
    a plain relative redirect that resolves against *Legion's* own host (since that's
    where `/sso/complete` runs), not Virtus's.
    """
    if return_to is not None:
        target = return_to if urlparse(return_to).netloc else f"{settings.base_url}{return_to}"
    else:
        target = str(request.url)
    return f"{settings.legion_base_url}/sso/authorize?app=virtus&return_to={quote(target, safe='')}"


def logout_url(request: Request, *, return_to: str = "/me") -> str:
    """Legion's single-logout endpoint, returning to `return_to` (a Virtus path)."""
    base = f"{request.url.scheme}://{request.url.netloc}{return_to}"
    return f"{settings.legion_base_url}/sso/logout?return_to={quote(base, safe='')}"


def is_same_app_path(url: Optional[str]) -> bool:
    """True only for a safe same-app relative path (leading '/', not protocol-relative)."""
    if not url:
        return False
    parsed = urlparse(url)
    return (
        not parsed.netloc
        and url.startswith("/")
        and not url.startswith("//")
        and not url.startswith("/\\")
    )
