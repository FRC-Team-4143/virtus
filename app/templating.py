"""
Shared Jinja2 environment — configured once and imported by every router so the same
filters and auth-aware globals are available in all templates.
"""
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services.sso import sso_identity, is_admin, is_staff, stepup_url
from app.utils import utc_to_local

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%b %d, %Y %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)
templates.env.filters["localdate"] = (
    lambda dt, fmt="%b %d, %Y": utc_to_local(dt).strftime(fmt) if dt else ""
)

# Exposed to templates for auth-aware nav (both admin sidebar and portal navbar read the
# same live mw_sso claims — no bridging route needed).
templates.env.globals["session_identity"] = sso_identity
templates.env.globals["is_admin"] = is_admin
templates.env.globals["is_staff"] = is_staff
templates.env.globals["stepup_url"] = stepup_url
templates.env.globals["legion_base_url"] = lambda: settings.legion_base_url
