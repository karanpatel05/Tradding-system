"""XSP 0DTE iron fly: the registry-facing strategy. The trading logic lives in
``runner.IronFlyRunner``; this class wires it to the live feed, the simulated executor
and the app's capital allocation, safe mode and alerts."""
from __future__ import annotations

from app.strategies.base import BaseStrategy, Candidate
from app.strategies.xsp_iron_fly.executor import LiveExecutorUnavailable
from app.strategies.xsp_iron_fly.params import IronFlyParams


class XspIronFlyStrategy(BaseStrategy):
    key = "xsp_iron_fly"
    name = "XSP 0DTE Iron Fly"
    ready = True
    category = "Options · 0DTE iron fly"
    universe = ("XSP",)

    description = (
        "Sells a 10-point-wide XSP (Mini-SPX, cash-settled, European) 0DTE iron fly at "
        "09:45 ET and holds it to the PM cash settlement, with an independent 1.5x stop on "
        "each short leg. Paper only: fills are simulated against live Tastytrade NBBO; "
        "no orders are sent anywhere."
    )
    rules = [
        "Once per trading day at 09:45 ET, on the 0DTE PM-settled XSP expiry (skip the day "
        "if none is listed).",
        "Body = XSP spot rounded to the nearest listed strike (x.50 rounds up). Sell the "
        "body call and put, buy the call 10 points above and the put 10 points below "
        "(nearest listed strike if missing; wing width recomputed).",
        "One 4-leg net-credit combo, limit from the mid and conceded 0.05 every 5 s, at most "
        "0.30 from the mid. Without combo support: buy both wings first, then the body.",
        "Each short leg has its own stop at 1.5x its entry fill, on the buy-to-close price "
        "(the ask). A stop closes only that leg; the other short keeps its stop; wings are "
        "never stopped; no profit target, no adjustment.",
        "Everything else is held to the 4:00 pm cash settlement at intrinsic (no assignment "
        "risk). Optional: flatten at 15:58 instead.",
        "Size: 1 fly per $5,000 allocated, capped by max loss vs allocation, by "
        "max_daily_loss and by max_contracts (default 1).",
        "Skip entry on a stale XSP quote, a leg spread over max_leg_spread, too little "
        "time to an early close, holidays, SAFE_MODE or a daily-loss halt.",
    ]
    entry_logic = "09:45 ET: sell the ATM iron fly with 10-point wings as one net-credit combo."
    exit_logic = ("Per short leg: buy to close at 1.5x its entry fill. Otherwise hold to the "
                  "4:00 pm ET cash settlement (optional flatten at 15:58).")
    selection_criteria = "Fixed underlying: XSP 0DTE (PM-settled)."
    backtest_note = ("No in-app backtest: the app's backtester is equity-only and there is "
                     "no historical XSP option quote data here. Validate with the "
                     "simulated paper sessions (GET /api/strategies/xsp_iron_fly/sessions).")

    def __init__(self, params: IronFlyParams | None = None) -> None:
        self._params = params

    @property
    def params(self) -> IronFlyParams:
        return self._params or IronFlyParams.from_settings()

    async def scan(self, universe: list[str]) -> list[Candidate]:
        return []   # fixed underlying; the runner does its own daily schedule

    def bar_interval(self) -> str:
        return "1m"

    def execution_label(self) -> str:
        return "Simulated fills"  # sim mode only; live mode is refused below

    # The session row (one per day, 4 leg TradeRecords) is the source of truth for
    # the Strategies screen, not a count of ledger rows.
    async def research_record(self) -> dict | None:
        from app.services.strategy_summary import session_record
        from app.strategies.xsp_iron_fly import store
        return session_record(await store.recent_sessions(self.key, 1000))

    async def session_pnl_today(self, today) -> float | None:
        from app.strategies.xsp_iron_fly import store
        sess = await store.get_session(self.key, today)
        if sess and sess.get("realized_pnl") is not None:
            return round(sess["realized_pnl"], 2)
        return None  # not finished yet: the router falls back to closed legs today

    def activation_block_reason(self) -> str | None:
        p = self.params
        errs = p.validate()
        if errs:
            return f"Invalid XSP fly settings: {'; '.join(errs)}."
        if p.mode != "sim":
            return (f"Live mode is refused: {LiveExecutorUnavailable.REASON}. "
                    "Set XSP_FLY_MODE=sim to run it on simulated fills.")
        return None

    def build_runner(self):
        from app.engine.safe_mode import is_safe_mode
        from app.services.alerts import send_alert
        from app.strategies.xsp_iron_fly.executor import SimExecutor
        from app.strategies.xsp_iron_fly.feed import TastytradeFeed
        from app.strategies.xsp_iron_fly.runner import IronFlyRunner, RealClock

        p = self.params
        feed = TastytradeFeed()
        clock = RealClock()
        executor = SimExecutor(feed, clock) if p.mode == "sim" else LiveExecutorUnavailable()
        return IronFlyRunner(self.key, p, feed, executor, clock,
                             capital_fn=self._capital, safe_mode_fn=is_safe_mode,
                             alert_fn=send_alert)

    async def run_live(self) -> None:
        """Engine entry point (dispatched first by ``engine.worker.run_strategy``)."""
        await self.build_runner().run_forever()

    async def _capital(self) -> float:
        from app.db.base import session_scope
        from app.db.models import StrategyState
        async with session_scope() as s:
            st = await s.get(StrategyState, self.key)
            return float(st.capital_allocation or 0.0) if st else 0.0

    async def status_view(self) -> dict:
        from app.strategies.xsp_iron_fly import store
        recent = await store.recent_sessions(self.key, 10)

        def live_ids(s: dict) -> list[int]:
            return [l["trade_id"] for l in s.get("legs") or []
                    if l.get("trade_id") and l.get("status") in ("open", "stopping")]

        # The kill switch closes the leg TradeRecords but the runner only marks the
        # session killed on its next start; show it as killed meanwhile.
        states = await store.trade_states(
            [i for s in recent if s.get("status") == "open" for i in live_ids(s)])

        def view(s: dict) -> dict:
            out = {k: v for k, v in s.items() if k != "fills"}
            for k, v in out.items():
                if hasattr(v, "isoformat"):
                    out[k] = v.isoformat()
            out["fill_count"] = len(s.get("fills") or [])
            ids = live_ids(s) if s.get("status") == "open" else []
            if ids and all(states.get(i, {}).get("status") == "closed" for i in ids):
                out["status"] = "killed"
                out["reason"] = "legs closed by the kill switch; exit prices unknown"
            return out

        return {"mode": self.params.mode, "halt": await store.halt_reason(),
                "sessions": [view(s) for s in recent]}
