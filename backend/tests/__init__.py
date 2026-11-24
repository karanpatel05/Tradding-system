"""Test-package guard: REFUSE to run against the live trading.db.

A standalone module-runner invoked without DATABASE_URL falls through to the
production default and pollutes real records (it happened on 2026-07-15 —
fixture rows landed in trading.db and force-closed a live paper position).
Every test entrypoint imports this package, so the guard covers pytest and
`python -m tests.<module>` alike."""
import os
from pathlib import Path

from tests._env_guard import env_problem

_url = os.environ.get("DATABASE_URL", "")
if not _url or "trading.db" in _url:
    raise RuntimeError(
        "Refusing to run tests against the live trading.db — set DATABASE_URL to a "
        "throwaway sqlite file, e.g. DATABASE_URL=sqlite+aiosqlite:///./_t_x.db")

# REFUSE to run next to the real backend/.env (a symlink to it, or the main checkout's).
_problem = env_problem(Path(__file__).resolve().parents[1])
if _problem:
    raise RuntimeError(f"Refusing to run tests: {_problem}")

# Tests never see real credentials and never route orders unless a test opts in
# (environment variables override backend/.env in pydantic-settings).
for _key in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TT_PROVIDER_SECRET",
             "TT_REFRESH_TOKEN"):
    os.environ[_key] = ""
os.environ["ENABLE_LIVE_ORDERS"] = "false"
