from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables and seed the starting competency list."""
    from app import models  # noqa: F401 — imported for side-effect (table registration)

    # Apply a staged database restore (if any) before the engine touches the file.
    from app.services.backup import apply_pending_restore
    apply_pending_restore()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # No Alembic. Additive column changes run here as an inspect-guarded ALTER
        # (a no-op on a fresh schema, which already has the column from create_all()),
        # mirroring the sibling apps.
        await conn.run_sync(_migrate_team_goal_category)
        await conn.run_sync(_migrate_review_private_notes)

    async with AsyncSessionLocal() as session:
        await seed_competencies(session)


def _migrate_review_private_notes(conn) -> None:
    """Additive `reviews.private_notes` column — the reviewer's private working notes,
    never surfaced to the student. A no-op on a fresh schema, which already has it from
    `create_all()`."""
    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(conn).get_columns("reviews")}
    if "private_notes" not in cols:
        conn.execute(text("ALTER TABLE reviews ADD COLUMN private_notes TEXT"))


def _migrate_team_goal_category(conn) -> None:
    """`team_goals.subteam_slug` (group by owning subteam) → `team_goals.category` (fixed
    three-value list) + `team_goals.team` (4143 / 4423 / organisation). Runs once: adds
    each column with a constant default for existing rows, then drops the old slug column.
    A no-op on a fresh schema, which already has both columns and no `subteam_slug` from
    `create_all()`."""
    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(conn).get_columns("team_goals")}
    if "category" not in cols:
        conn.execute(text(
            "ALTER TABLE team_goals ADD COLUMN category VARCHAR(20) "
            "NOT NULL DEFAULT 'robot_performance'"
        ))
    if "team" not in cols:
        conn.execute(text(
            "ALTER TABLE team_goals ADD COLUMN team VARCHAR(20) "
            "NOT NULL DEFAULT 'organization'"
        ))
    if "subteam_slug" in cols:
        # The old column was `index=True`; SQLite won't drop a column an index still
        # references, so the index goes first.
        conn.execute(text("DROP INDEX IF EXISTS ix_team_goals_subteam_slug"))
        conn.execute(text("ALTER TABLE team_goals DROP COLUMN subteam_slug"))


async def seed_competencies(db: AsyncSession) -> int:
    """Insert the default competency list on a brand-new install.

    The *only* seed data in the app, and deliberately all-or-nothing: it runs only when
    the table is completely empty, so an admin who deletes a competency they don't want
    never has it silently reappear on the next boot.
    """
    from app.models import Competency, DEFAULT_COMPETENCIES

    existing = (await db.execute(select(Competency.id).limit(1))).scalars().first()
    if existing is not None:
        return 0
    for i, (name, description) in enumerate(DEFAULT_COMPETENCIES):
        db.add(Competency(name=name, description=description, sort_order=i * 10))
    await db.commit()
    return len(DEFAULT_COMPETENCIES)
