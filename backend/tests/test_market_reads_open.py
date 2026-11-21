"""Public market-data reads stay open (the gate's policy: the app token guards the
dangerous mutating routes; reads land without a login). Gating them made every
browser or phone without the token fall back to the Chart screen's sample candles.
Account data stays gated."""
from app.auth.gate import require_auth
from app.routers.dashboard import router


def _gated(path: str) -> bool:
    route = next(r for r in router.routes if getattr(r, "path", None) == path)
    return any(d.call is require_auth for d in route.dependant.dependencies)


def test_market_data_reads_are_open():
    for path in ("/api/market/status", "/api/market/overview",
                 "/api/market/bars", "/api/market/quotes"):
        assert not _gated(path), path


def test_account_data_and_kill_stay_gated():
    for path in ("/api/account", "/api/positions", "/api/kill"):
        assert _gated(path), path
