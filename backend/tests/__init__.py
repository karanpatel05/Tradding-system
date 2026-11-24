"""Test-package guard: REFUSE to run against the live trading.db.

A standalone module-runner invoked without DATABASE_URL falls through to the
production default and pollutes real records (it happened on 2026-07-15 —
fixture rows landed in trading.db and force-closed a live paper position).
Every test entrypoint imports this package, so the guard covers pytest and
`python -m tests.<module>` alike."""
import os

_url = os.environ.get("DATABASE_URL", "")
if not _url or "trading.db" in _url:
    raise RuntimeError(
        "Refusing to run tests against the live trading.db — set DATABASE_URL to a "
        "throwaway sqlite file, e.g. DATABASE_URL=sqlite+aiosqlite:///./_t_x.db")

# Same protection for the local price-history file: never touch the real
# backend/market_history.db. Default to a per-process temp file.
if not os.environ.get("MARKET_HISTORY_PATH"):
    import tempfile
    os.environ["MARKET_HISTORY_PATH"] = os.path.join(
        tempfile.gettempdir(), f"_t_market_history_{os.getpid()}.db")
if os.environ["MARKET_HISTORY_PATH"].replace("\\", "/").endswith("backend/market_history.db"):
    raise RuntimeError(
        "Refusing to run tests against the live market_history.db — set "
        "MARKET_HISTORY_PATH to a throwaway sqlite file.")
