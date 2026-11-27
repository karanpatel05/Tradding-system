"""Session history and operator halt controls for the two independent iron-fly books."""
from fastapi import APIRouter, Depends

from app.auth.gate import require_auth
from app.services.journal import log_event
from app.strategies.registry import get_strategy
from app.strategies.xsp_iron_fly import store


def fly_routes(key: str) -> APIRouter:
    routes = APIRouter(prefix=f"/api/strategies/{key}", tags=["strategies"])

    @routes.get("/sessions", name=f"{key}_sessions")
    async def sessions() -> dict:
        return await get_strategy(key).status_view()

    @routes.post("/clear-halt", name=f"{key}_clear_halt", dependencies=[Depends(require_auth)])
    async def clear_halt() -> dict:
        cleared = await store.clear_halt(key=key)
        if cleared:
            await log_event("Iron fly daily-loss halt cleared by operator",
                            category="action", strategy_key=key)
        return {"cleared": cleared}

    return routes


router = APIRouter()
router.include_router(fly_routes("xsp_iron_fly"))
router.include_router(fly_routes("spy_iron_fly"))
