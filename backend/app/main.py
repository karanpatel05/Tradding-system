"""FastAPI application entrypoint.

Boots with zero external infra (SQLite + no OAuth secret) so the auth panel can
always render. Routers are mounted incrementally per build slice.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.gate import require_auth
from app.auth.routes import router as auth_router
from app.config import get_settings
from app.db import init_db
from app.routers.builder import router as builder_router
from app.routers.dashboard import router as dashboard_router
from app.routers.journal import logs_router, router as journal_router
from app.routers.strategies import router as strategies_router
from app.routers.system import router as system_router
from app.routers.ws import router as ws_router
from app.strategies.registry import seed_strategy_state

logging.basicConfig(level=logging.INFO)
# The tastytrade SDK logs every DXLink event at DEBUG — with a persistent quote stream
# that's millions of lines (a 443MB err.log by 2026-07-15). Clamp SDK loggers hard.
for _noisy in ("tastytrade", "httpx", "httpx_ws", "websockets"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger("trading")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s (env=%s)", settings.app_name, settings.environment)
    await init_db()
    await seed_strategy_state()
    logger.info("Database initialized")
    logger.info(
        "[Tastytrade] broker = Tastytrade OAuth. Connect via the API tab "
        "or POST /api/auth/connect. App boots without a live connection."
    )
    from app.engine.recovery import run_recovery
    try:
        await run_recovery()
    except Exception:  # noqa: BLE001 — recovery is best-effort at boot
        logger.warning("startup recovery failed", exc_info=True)
    await _resume_active_workers()
    from app.options.monitor import run_builder_monitor
    asyncio.create_task(run_builder_monitor())
    from app.broker.status import refresher
    asyncio.create_task(refresher())
    from app.services.alpaca_status import alpaca_refresher
    asyncio.create_task(alpaca_refresher())
    yield
    logger.info("Shutting down")


async def _resume_active_workers() -> None:
    """Restart workers for strategies whose persisted status was active.

    ``engine_manager`` is in-memory, so without this a server restart leaves a
    strategy showing 'scanning'/'active' in the DB while nothing actually runs.
    """
    from app.db.base import session_scope
    from app.db.models import StrategyState
    from app.engine.manager import engine_manager
    from app.engine.worker import run_strategy
    from app.strategies.registry import list_strategies

    async with session_scope() as session:
        for strat in list_strategies():
            st = await session.get(StrategyState, strat.key)
            if (st and st.status in ("scanning", "active")
                    and strat.ready and not engine_manager.is_running(strat.key)):
                await engine_manager.start_worker(strat.key, run_strategy(strat))
                logger.info("Resumed worker for %s (persisted status=%s)", strat.key, st.status)


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,  # required so the browser sends the session cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth_router)
app.include_router(builder_router)
app.include_router(dashboard_router)
app.include_router(journal_router)
app.include_router(logs_router)
app.include_router(strategies_router)
app.include_router(system_router)
app.include_router(ws_router)


@app.get("/health", tags=["system"])
async def health() -> dict:
    """Ungated liveness probe."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "broker": "tastytrade",
        "live_orders_enabled": settings.enable_live_orders,
    }


@app.get("/api/me", tags=["system"], dependencies=[Depends(require_auth)])
async def me() -> dict:
    """Gated probe — verifies the auth gate works end to end."""
    return {"authenticated": True}


# Serve the built React app same-origin in the packaged desktop build (no-op in dev).
# Registered LAST so its SPA catch-all never shadows ANY API/WS/system route above.
from app.static import mount_frontend  # noqa: E402

mount_frontend(app, settings.frontend_dist)
