"""QQQ first-hour ATM credit spread — registered here, executed elsewhere.

The live strategy runs outside this app, from ``~/live_system_maintenance/live`` under
the ``com.karan.spreadlive`` LaunchAgent, and places REAL Tastytrade orders (plus an
Alpaca paper copy). This app is still in development, so the strategy is listed with
its real rules and a read-only view of the daemon, but it is switched OFF here:

* activation is always refused (``activation_block_reason`` never returns None);
* it is never auto-resumed at boot, and a persisted active status is reset on seed;
* nothing here imports the runner, writes its state/lock/credentials, or touches the
  LaunchAgent. The daemon's state file and lock are only ever READ.

Turning it on in-app later means porting the two-leg spread execution into the engine
(which today only places equity orders) and dropping the "not built" block below.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.strategies.base import BaseStrategy, Candidate

_ET = ZoneInfo("America/New_York")


class QqqCreditSpreadStrategy(BaseStrategy):
    key = "qqq_credit_spread"
    name = "QQQ First-Hour Credit Spread"
    ready = True
    external_runner = True

    description = (
        "0DTE QQQ $1-wide ATM credit spread sold on the direction of the first hour. "
        "Runs outside this app via the com.karan.spreadlive LaunchAgent (real "
        "Tastytrade orders plus an Alpaca paper copy). Switched off here while this "
        "app is in development."
    )
    rules = [
        "QQQ only, one $1-wide spread, same-day expiry.",
        "Green first hour (9:30-10:29 ET) sells a put spread; red sells a call spread; "
        "flat first-hour candles are skipped.",
        "Short strike = first-hour close rounded away from spot; long leg $1 further out.",
        "Limit credit from live option mids, re-priced every 15 s toward the marketable "
        "price, cancelled at the entry deadline.",
        "An action fires only within 10 minutes of its scheduled time; a late start "
        "records it as missed.",
        "Size: one spread per capital step of net liquidating value, capped per ticker "
        "and in total.",
    ]
    entry_logic = "10:30 ET: read the completed first hour and sell the spread against it."
    exit_logic = (
        "15:30 ET (12:30 half days): an ML model predicts the chance the short strike "
        "finishes ITM; strictly above the model's threshold (currently 90%) it closes. "
        "Everything left starts closing at 15:50 ET, targeting broker-confirmed flat "
        "by 15:55 ET (12:50/12:55 half days)."
    )
    selection_criteria = "Fixed underlying: QQQ."

    daemon_label = "com.karan.spreadlive"

    def __init__(self, live_dir: str | Path | None = None) -> None:
        if live_dir is None:
            from app.config import get_settings
            live_dir = get_settings().qqq_spread_live_dir
        self.live_dir = Path(live_dir).expanduser()

    # --- engine hooks -------------------------------------------------------
    async def scan(self, universe: list[str]) -> list[Candidate]:
        # Never reached (activation is refused); empty rather than raising so a
        # stray call can't crash a loop.
        return []

    def activation_block_reason(self) -> str | None:
        running, pid = self._daemon_running()
        if running:
            who = f"pid {pid}" if pid else "lock held"
            return (f"Already running on this computer ({self.daemon_label}, {who}). "
                    "This app will not start a second copy.")
        return ("In-app execution is not built yet: this app is in development. "
                f"The strategy runs via the {self.daemon_label} LaunchAgent.")

    # --- read-only view of the external daemon ------------------------------
    def external_status(self) -> dict:
        running, pid = self._daemon_running()
        return {
            "runner": self.daemon_label,
            "live_dir": str(self.live_dir),
            "found": self.live_dir.is_dir(),
            "daemon_running": running,
            "pid": pid,
            "today": self._today_summary(),
        }

    def _lock_path(self) -> Path:
        return self.live_dir / "state" / ".live_strategy.lock"

    def _daemon_running(self) -> tuple[bool, int | None]:
        """(running, pid) from the runner's single-instance lock. No lock = not
        running. A lock we cannot read or parse counts as running (fail closed)."""
        lock = self._lock_path()
        if not lock.exists():
            return False, None
        try:
            pid = int(lock.read_text().strip())
        except (OSError, ValueError):
            return True, None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, pid
        except OSError:  # e.g. PermissionError: the pid exists
            return True, pid
        return True, pid

    def _today_summary(self) -> dict | None:
        path = self.live_dir / "state" / "live_state.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        today = datetime.now(_ET).date().isoformat()
        day = data.get(today) if isinstance(data, dict) else None
        if not isinstance(day, dict):
            return {"session_date": today, "entry": None, "checkpoint": None,
                    "final_close": None, "position": None}
        position = None
        for ticker, e in (day.get("entries") or {}).items():
            if not isinstance(e, dict):
                continue
            right = e.get("right")
            position = {
                "ticker": ticker,
                "spread": {"C": "call credit spread", "P": "put credit spread"}.get(
                    right, "credit spread"),
                "short_strike": e.get("short_strike"),
                "long_strike": e.get("long_strike"),
                "quantity": e.get("quantity"),
                "credit_per_share": e.get("filled_credit_per_share"),
                "status": e.get("status"),
            }
            break
        return {
            "session_date": today,
            "entry": day.get("entry"),
            "checkpoint": day.get("checkpoint"),
            "final_close": day.get("final_close"),
            "position": position,
        }
