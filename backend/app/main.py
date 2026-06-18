"""FastAPI application entrypoint.

Boots with zero external infra (SQLite + no OAuth secret) so the auth panel can
always render. Routers are mounted incrementally per build slice.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.gate import require_auth
from app.auth.routes import router as auth_router
from app.config import get_settings
from app.db import init_db
from app.routers.dashboard import router as dashboard_router
from app.routers.journal import logs_router, router as journal_router
from app.routers.strategies import router as strategies_router
from app.routers.ws import router as ws_router
from app.strategies.registry import seed_strategy_state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s (env=%s)", settings.app_name, settings.environment)
    await init_db()
    await seed_strategy_state()
    logger.info("Database initialized")
    if not settings.oauth_configured:
        logger.warning(
            "TASTYTRADE_PROVIDER_SECRET not set — auth panel will report "
            "'OAuth not configured'. App still boots."
        )
    if not settings.massive_configured:
        logger.warning(
            "[Massive] MASSIVE_API_KEY not set — historical/backtest data is "
            "unavailable until it's configured. App still boots."
        )
    else:
        logger.info("[Massive] historical provider configured (%s)", settings.massive_base_url)
    await _restore_session()
    await _resume_active_workers()
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


async def _restore_session() -> None:
    """Rebuild a persisted session at boot so the user isn't re-prompted after a
    restart. Reuses the stored app_session_id so the existing browser cookie keeps
    validating. On any rebuild failure we stay disconnected but KEEP the stored
    credentials (so they remain editable in the API tab) — never leaving a dead
    'authenticated' state, but also never silently discarding the saved secret.
    """
    from app.auth import store
    from app.auth.manager import session_manager
    from app.tastytrade.client import build_session

    creds = store.load()
    if not creds:
        return
    try:
        tasty_session = await build_session(creds["refresh_token"], creds.get("provider_secret"))
    except Exception as exc:  # noqa: BLE001 — bad/expired token, network, etc.
        # Keep the stored credentials (don't clear) so they survive the restart and
        # remain editable in the API tab; we just stay disconnected until fixed.
        logger.warning("Persisted session could not be restored (%s); staying "
                       "disconnected, credentials kept for the API tab.", exc)
        return
    session_manager.establish(
        tasty_session,
        refresh_token=creds["refresh_token"],
        provider_secret=creds.get("provider_secret"),
        app_session_id=creds["app_session_id"],
        persist=False,  # already on disk
    )
    logger.info("Restored persisted Tastytrade session from store.")


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,  # required so the browser sends the session cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(journal_router)
app.include_router(logs_router)
app.include_router(strategies_router)
app.include_router(ws_router)


@app.get("/health", tags=["system"])
async def health() -> dict:
    """Ungated liveness probe."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "oauth_configured": settings.oauth_configured,
        "live_orders_enabled": settings.enable_live_orders,
    }


@app.get("/api/me", tags=["system"], dependencies=[Depends(require_auth)])
async def me() -> dict:
    """Gated probe — verifies the auth gate works end to end."""
    return {"authenticated": True}
