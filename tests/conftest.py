"""
Test fixtures — a fresh in-memory SQLite database per test (a real DB, never mocked), an
httpx client wired to it, an mw_sso cookie minter, and a fake Slack recorder so no
outbound Slack call ever leaves the process.
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db, seed_competencies
from app.main import app
from app.models import Member, MemberKind


# ── Settings isolation ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv():
    """The suite must not be sensitive to whatever's in the developer's `.env` (a real
    LEGION_API_KEY, a production cookie domain, a different CURRENT_SEASON). Reset every
    setting to its class default before each test and restore afterwards; `sso_secret`
    gets a fixed test value since a blank signing key would make every cookie
    indistinguishable. Copied from Legion's conftest, which hit this first."""
    from app.config import Settings

    defaults = Settings(_env_file=None, sso_secret="test-sso-secret")
    original = {name: getattr(settings, name) for name in Settings.model_fields}
    for name in Settings.model_fields:
        setattr(settings, name, getattr(defaults, name))
    _rebuild_signers()
    yield
    for name, value in original.items():
        setattr(settings, name, value)
    _rebuild_signers()


def _rebuild_signers() -> None:
    """`sso.py` and `legion_auth.py` build their `URLSafeTimedSerializer`s at import time
    from `settings.sso_secret` (matching the sibling apps). Swapping the setting alone
    would leave them signing with the old key, so every cookie this suite mints would
    fail verification — rebuild them whenever the fixture changes the secret."""
    from itsdangerous import URLSafeTimedSerializer
    from app.services import legion_auth, sso as sso_service

    sso_service._sso_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")
    legion_auth._link_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso-link")


# ── Database ───────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def competencies(db):
    """The default competency list, as a fresh install would have it."""
    from sqlalchemy import select
    from app.models import Competency
    await seed_competencies(db)
    return (await db.execute(select(Competency).order_by(Competency.sort_order))).scalars().all()


# ── Fake Slack ───────────────────────────────────────────────────────────────────

class FakeSlack:
    """Records every DM / channel post instead of hitting Slack."""
    def __init__(self):
        self.dms: list[dict] = []
        self.channel_posts: list[dict] = []

    async def conversations_open(self, users):
        return {"channel": {"id": f"D{users}"}}

    async def chat_postMessage(self, channel, text, blocks=None):
        record = {"channel": channel, "text": text, "blocks": blocks}
        (self.dms if channel.startswith("D") else self.channel_posts).append(record)
        return {"ts": "1.0"}

    async def chat_update(self, channel, ts, text, blocks=None):
        return {"ts": ts}


@pytest.fixture(autouse=True)
def fake_slack():
    """Install a fake Slack client for every test; updates stay enabled so sends are
    actually attempted (and recorded)."""
    from app.services import slack_client
    fake = FakeSlack()
    slack_client._client = fake
    settings.updates_enabled = True
    yield fake
    slack_client._client = None


# ── HTTP client ────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(session_factory, db):
    async def _get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── SSO cookie ─────────────────────────────────────────────────────────────────────

def make_sso_cookie(
    *, member_code="m0000001", name="Test Member", role="student",
    groups=None, slack_user_id=None, team_number=4143, via=None,
):
    signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")
    claims = {
        "member_code": member_code,
        "username": name.lower().replace(" ", "."),
        "name": name,
        "role": role,
        "team_number": team_number,
        "groups": groups or [],
        "slack_user_id": slack_user_id,
    }
    if via:
        claims["via"] = via
    return signer.dumps(claims)


def cookie_for(member: Member, *, groups=None, via=None) -> str:
    """An mw_sso cookie matching a `Member` row the test already created — the pairing
    every portal route needs (the cookie names the member, the row must exist)."""
    return make_sso_cookie(
        member_code=member.member_code, name=member.name,
        role=member.kind.value, groups=groups, slack_user_id=member.slack_user_id,
        via=via,
    )


@pytest.fixture
def admin_cookie():
    return make_sso_cookie(
        member_code="admin001", name="Ada Admin", role="mentor",
        groups=["virtus-admin"], slack_user_id="UADMIN",
    )


@pytest.fixture
def manager_cookie():
    return make_sso_cookie(
        member_code="mgr00001", name="Mel Manager", role="mentor",
        groups=["virtus-manager"], slack_user_id="UMGR",
    )


# ── Convenience factories ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def make_member(db):
    counter = {"n": 0}

    async def _make(name="Member", *, kind=MemberKind.student, slack_user_id=None,
                    member_code=None, groups=None, team_number=4143,
                    subteam_slug=None, subteam_label=None, is_active=True):
        counter["n"] += 1
        m = Member(
            member_code=member_code or f"code{counter['n']:04d}",
            name=name, kind=kind, slack_user_id=slack_user_id,
            team_number=team_number, is_active=is_active,
            subteam_slug=subteam_slug, subteam_label=subteam_label,
            group_slugs=",".join(groups) if groups else None,
        )
        db.add(m)
        await db.commit()
        await db.refresh(m)
        return m

    return _make


@pytest_asyncio.fixture
async def make_cycle(db, competencies):
    """A cycle with a roster, optionally already opened (which freezes the competency
    snapshot). Returns the cycle."""
    from app.services import cycles as cycle_service

    async def _make(name="Test Cycle", *, season="2026", opened=True, closes_at=None):
        cycle = await cycle_service.create_cycle(
            db, name=name, season=season, closes_at=closes_at
        )
        await cycle_service.populate_roster(db, cycle)
        if opened:
            await cycle_service.open_cycle(db, cycle)
        await db.commit()
        return cycle

    return _make
