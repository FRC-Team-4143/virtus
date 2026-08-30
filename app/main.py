from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db, init_db
from app.routers import portal, admin, slack
from app.services.scheduler import create_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler = create_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown()


app = FastAPI(title="Virtus", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(portal.router)
app.include_router(admin.router)
app.include_router(slack.router)

# Preview deploys only — see routers/dev_login.py. Unset in production ⇒ never mounted.
if settings.dev_login_secret:
    from app.routers import dev_login
    app.include_router(dev_login.router)
    print("⚠️  Virtus /dev-login is ENABLED — this must not be a production config.")


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    """Unauthenticated liveness probe — Legion's admin dashboard polls this to show
    Virtus in its System Status panel."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — any DB error means "not healthy"
        return JSONResponse({"status": "error", "app": "virtus"}, status_code=503)
    return {"status": "ok", "app": "virtus"}
