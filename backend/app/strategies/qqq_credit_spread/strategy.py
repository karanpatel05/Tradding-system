"""QQQ first-hour ATM credit spread, run IN-APP on the Alpaca PAPER account.

The trading logic is a port of the external runner (``~/live_system_maintenance/live``,
``com.karan.spreadlive``) checked for parity against its own functions; orders go only
to Alpaca paper through the app's tagged, paper-locked path, and Tastytrade supplies
market data only. Unlike the runner, there is no 15:30 model exit: every spread is
held to the 15:50 flat close. See ``runner.QqqSpreadRunner`` for the day's timeline.

The external daemon may keep trading real money on Tastytrade, but its own Alpaca paper
copy (``--alpaca-paper``) shares this account and closes by the positions Alpaca reports,
so activation is refused while that copy is on. Nothing here writes the daemon's
files or touches its LaunchAgent; its lock and process line are only READ.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.strategies.base import BaseStrategy, Candidate
from app.strategies.qqq_credit_spread.params import QqqSpreadParams


class QqqCreditSpreadStrategy(BaseStrategy):
    key = "qqq_credit_spread"
    name = "QQQ First-Hour Credit Spread"
    ready = True
    category = "Options · 0DTE credit spread"
    universe = ("QQQ",)

    description = (
        "0DTE QQQ $1-wide ATM credit spread sold on the direction of the first hour and "
        "closed flat at 15:50 (no model, no intraday exit). Orders go to the Alpaca PAPER "
        "account only; Tastytrade is used for market data."
    )
    rules = [
        "QQQ only, one $1-wide spread, same-day expiry.",
        "Green first hour (9:30-10:29 ET) sells a put spread; red sells a call spread; "
        "flat first-hour candles are skipped.",
        "Short strike = first-hour close rounded away from spot; long leg $1 further out.",
        "Limit credit at the live option mid, re-priced every 15 s, cancelled if unfilled "
        "10 minutes after 10:30. Minimum credit 0.02.",
        "An action fires only within 10 minutes of its scheduled time; a late start "
        "records it as missed.",
        "Size: one spread per $1,000 allocated (the first at $500), capped at "
        "max_contracts (1 while testing).",
        "Hold to the 15:50 close: no stop, no profit target, no model exit.",
    ]
    entry_logic = "10:30 ET: read the completed first hour and sell the spread against it."
    exit_logic = (
        "Flat exit only: at 15:50 ET (12:50 half days) the whole spread is closed, walking "
        "the limit from mid to marketable, aiming to be flat by 15:55 ET (12:55 half days). "
        "No model and no earlier exit."
    )
    selection_criteria = "Fixed underlying: QQQ."
    backtest_note = ("No in-app backtest: the app's backtester is equity-only. The strategy "
                     "was validated by the external runner's backtester; review the in-app "
                     "sessions (GET /api/strategies/qqq_credit_spread/sessions).")

    daemon_label = "com.karan.spreadlive"

    def __init__(self, live_dir: str | Path | None = None,
                 params: QqqSpreadParams | None = None) -> None:
        if live_dir is None:
            from app.config import get_settings
            live_dir = get_settings().qqq_spread_live_dir
        self.live_dir = Path(live_dir).expanduser()
        self._params = params

    @property
    def params(self) -> QqqSpreadParams:
        return self._params or QqqSpreadParams.from_settings()

    # --- engine hooks -------------------------------------------------------
    async def scan(self, universe: list[str]) -> list[Candidate]:
        return []   # fixed underlying; the runner keeps its own daily schedule

    def bar_interval(self) -> str:
        return "1m"

    def execution_label(self) -> str:
        from app.config import get_settings
        return "Alpaca paper" if get_settings().enable_live_orders else "Alpaca paper (routing off)"

    def activation_block_reason(self) -> str | None:
        from app.broker import alpaca_client as ac
        from app.config import get_settings
        p = self.params
        if (errs := p.validate()):
            return f"Invalid QQQ spread settings: {'; '.join(errs)}."
        if (why := self._daemon_paper_copy()) is not None:
            return why
        if not get_settings().enable_live_orders:
            return ("Order routing is off: turn on Alpaca paper order routing "
                    "(ENABLE_LIVE_ORDERS) first - this strategy never runs on simulated fills.")
        if not ac.configured():
            return "Alpaca paper keys are not configured (APCA_* in backend/.env)."
        return self._entry_guard()

    def resume_block_reason(self) -> str | None:
        """A QQQ worker that was on always resumes (unless its settings are invalid):
        a refusal here could strand an open spread past 15:55. The runner re-checks
        routing and the daemon's paper copy before each NEW entry, and its exits always
        run."""
        errs = self.params.validate()
        return f"Invalid QQQ spread settings: {'; '.join(errs)}." if errs else None

    def _entry_guard(self) -> str | None:
        """Extra checks before each new entry (the runner calls this at 10:30)."""
        return self._daemon_paper_copy()

    def build_runner(self):
        from app.engine.safe_mode import is_safe_mode
        from app.services.alerts import send_alert
        from app.services.journal import log_event
        from app.strategies.qqq_credit_spread import store
        from app.strategies.qqq_credit_spread.broker import AlpacaSpreadBroker
        from app.strategies.qqq_credit_spread.feed import TastytradeSpreadFeed
        from app.strategies.qqq_credit_spread.runner import QqqSpreadRunner, RealClock

        p = self.params
        return QqqSpreadRunner(
            self.key, p, TastytradeSpreadFeed(), AlpacaSpreadBroker(self.key), RealClock(),
            store=store, capital_fn=self._capital, safe_mode_fn=is_safe_mode, alert_fn=send_alert, log_fn=log_event,
            entry_guard_fn=self._entry_guard)

    async def run_live(self) -> None:
        """Engine entry point (dispatched first by ``engine.worker.run_strategy``)."""
        await self.build_runner().run_forever()

    async def _capital(self) -> float:
        from app.db.base import session_scope
        from app.db.models import StrategyState
        async with session_scope() as s:
            st = await s.get(StrategyState, self.key)
            return float(st.capital_allocation or 0.0) if st else 0.0

    # --- Strategies screen ----------------------------------------------------
    async def research_record(self) -> dict | None:
        from app.services.strategy_summary import session_record
        from app.strategies.qqq_credit_spread import store
        return session_record(await store.recent_sessions(self.key, 1000))

    async def session_pnl_today(self, today) -> float | None:
        from app.strategies.qqq_credit_spread import store
        sess = await store.get_session(self.key, today)
        if sess and sess.get("realized_pnl") is not None:
            return round(sess["realized_pnl"], 2)
        return None

    async def status_view(self) -> dict:
        from app.strategies.qqq_credit_spread import store
        sessions = []
        for s in await store.recent_sessions(self.key, 10):
            d = s["data"]
            sig = d.get("signal") or {}
            sessions.append({
                "session_date": s["session_date"].isoformat(),
                "status": s["status"], "reason": s["reason"],
                "realized_pnl": s["realized_pnl"],
                "spread": ({"right": sig.get("right"), "short_symbol": sig.get("short_symbol"),
                            "long_symbol": sig.get("long_symbol"),
                            "short_strike": sig.get("short_strike"),
                            "long_strike": sig.get("long_strike")} if sig else None),
                "quantity": d.get("open_qty", d.get("quantity")),
                "filled_credit": d.get("filled_credit"),
                "exit_debit": d.get("exit_debit"),
            })
        return {"execution": self.execution_label(), "daemon": self.daemon_status(),
                "sessions": sessions}

    # --- the external daemon (read-only) --------------------------------------
    def daemon_status(self) -> dict:
        running, pid = self._daemon_running()
        cmd = self._process_command(pid) if running and pid else None
        return {"label": self.daemon_label, "running": running, "pid": pid,
                "alpaca_paper_copy": (None if cmd is None else "--alpaca-paper" in cmd)}

    def _daemon_paper_copy(self) -> str | None:
        """Refusal reason while the daemon's own Alpaca paper copy is (or may be) on."""
        running, pid = self._daemon_running()
        if not running:
            return None
        cmd = self._process_command(pid) if pid else None
        if cmd is None:
            return (f"{self.daemon_label} is running and its command line cannot be read, "
                    "so its Alpaca paper copy may be on (same paper account). Refusing.")
        if "--alpaca-paper" in cmd:
            return (f"{self.daemon_label} (pid {pid}) still runs with --alpaca-paper: its paper "
                    "copy trades the same Alpaca paper account. Drop that flag first.")
        return None

    @staticmethod
    def _process_command(pid: int | None) -> str | None:
        if not pid:
            return None
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() or None

    def _lock_path(self) -> Path:
        return self.live_dir / "state" / ".live_strategy.lock"

    def _daemon_running(self) -> tuple[bool, int | None]:
        """(running, pid) from the runner's single-instance lock. No lock = not running.
        A lock we cannot read or parse counts as running (fail closed)."""
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
