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
