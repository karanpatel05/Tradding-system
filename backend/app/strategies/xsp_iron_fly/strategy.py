"""XSP 0DTE iron fly: the registry-facing strategy. The trading logic lives in
``runner.IronFlyRunner``; this class wires it to the live feed, the Alpaca paper executor
the strategy's own equity (starting equity + realized P&L), safe mode and alerts."""
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
        "each short leg. Trades the Alpaca paper account with real orders and fills "
        "(no simulated fills); quotes come from Tastytrade, which never receives orders."
    )
    rules = [
        "Once per trading day at 09:45 ET, on the 0DTE PM-settled XSP expiry (skip the day "
        "if none is listed).",
        "Body = XSP spot rounded to the nearest listed strike (x.50 rounds up). Sell the "
        "body call and put, buy the call 10 points above and the put 10 points below "
        "(nearest listed strike if missing; wing width recomputed).",
        "One 4-leg net-credit combo, limit from the mid and conceded 0.05 every 5 s, at most "
        "0.30 from the mid, sent to Alpaca paper as one mleg DAY limit (negative limit = "
        "credit); each attempt is cancelled after a few seconds if unfilled.",
        "Each short leg has its own stop at 1.5x its entry fill, on the buy-to-close price "
        "(the ask). A stop closes only that leg; the other short keeps its stop; wings are "
        "never stopped; no profit target, no adjustment.",
        "Everything else is held to the 4:00 pm cash settlement at intrinsic (no assignment "
        "risk). Optional: flatten at 15:58 instead.",
        "Size: 1 fly per $5,000 of equity (starting equity $5,000 plus the strategy's "
        "realized P&L), never below 1 while equity covers one fly's max loss. No "
        "contract cap; the daily-loss limit is $1,000 per fly sized that day.",
        "Skip entry on a stale XSP quote, a leg spread over max_leg_spread, too little "
        "time to an early close, holidays, SAFE_MODE or a daily-loss halt.",
    ]
    entry_logic = "09:45 ET: sell the ATM iron fly with 10-point wings as one net-credit combo."
    exit_logic = ("Per short leg: buy to close at 1.5x its entry fill. Otherwise hold to the "
                  "4:00 pm ET cash settlement (optional flatten at 15:58).")
    selection_criteria = "Fixed underlying: XSP 0DTE (PM-settled)."
    backtest_note = ("No in-app backtest: the app's backtester is equity-only and there is "
                     "no historical XSP option quote data here. Validate with the "
                     "Alpaca paper sessions (GET /api/strategies/xsp_iron_fly/sessions).")

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
        return "Alpaca paper"

    # The session row (one per day, 4 leg TradeRecords) is the source of truth for
    # the Strategies screen, not a count of ledger rows.
    async def research_record(self) -> dict | None:
        from app.services.strategy_summary import session_record
        from app.strategies.xsp_iron_fly import store
        return session_record(await store.recent_sessions(self.key, 1000))

    async def session_rows(self) -> list[dict] | None:
        from app.strategies.xsp_iron_fly import store
        return await store.recent_sessions(self.key, 5000)

    async def session_pnl_today(self, today) -> float | None:
        """Today's session realized_pnl (exact: includes unwind P&L and fees that no
        TradeRecord carries). While the fly is still open it is not set yet, and the
        router falls back to the legs already closed today."""
        from app.strategies.xsp_iron_fly import store
        sess = await store.get_session(self.key, today)
        if sess and sess.get("realized_pnl") is not None:
            return round(sess["realized_pnl"], 2)
        return None

    def activation_block_reason(self) -> str | None:
        p = self.params
        errs = p.validate()
        if errs:
            return f"Invalid {p.ticker} fly settings: {'; '.join(errs)}."
        if p.mode == "alpaca_paper":
            from app.strategies.xsp_iron_fly.alpaca_executor import AlpacaPort
            why = AlpacaPort(self.key).routing_block()
            return (f"{p.ticker} trades on Alpaca paper and can't place orders: {why}."
                    if why else None)
        if p.mode != "alpaca_paper":
            return (f"Live mode is refused: {LiveExecutorUnavailable.REASON}. "
                    "Set XSP_FLY_MODE=alpaca_paper.")
        return None

    def resume_block_reason(self) -> str | None:
        """An XSP worker that was on always resumes at boot unless its settings are
        invalid: a refusal (routing off, keys missing) must never strand an open fly.
        Its orders still go through the gated path, so nothing is sent while routing
        is off; a rejected stop close alerts."""
        errs = self.params.validate()
        return f"Invalid {self.params.ticker} fly settings: {'; '.join(errs)}." if errs else None

    def build_runner(self):
        from app.engine.safe_mode import is_safe_mode
        from app.services.alerts import send_alert
        from app.strategies.xsp_iron_fly.feed import TastytradeFeed
        from app.strategies.xsp_iron_fly.runner import IronFlyRunner, RealClock

        p = self.params
        feed = TastytradeFeed()
        clock = RealClock()
        if p.mode == "alpaca_paper":
            from app.strategies.xsp_iron_fly.alpaca_executor import (AlpacaFlyExecutor,
                                                                     AlpacaPort)
            executor = AlpacaFlyExecutor(AlpacaPort(self.key), feed, clock)
        else:
            executor = LiveExecutorUnavailable()
        return IronFlyRunner(self.key, p, feed, executor, clock,
                             equity_fn=self._equity, safe_mode_fn=is_safe_mode,
                             alert_fn=send_alert,
                             entries_paused_fn=self._entries_paused if p.ticker == "SPY" else None)

    async def run_live(self) -> None:
        """Engine entry point (dispatched first by ``engine.worker.run_strategy``)."""
        await self.build_runner().run_forever()
        if self.params.ticker == "SPY":
            await self._finish_pause()

    async def _entries_paused(self) -> bool:
        from app.db.base import session_scope
        from app.db.models import StrategyState
        async with session_scope() as session:
            state = await session.get(StrategyState, self.key)
            return state is None or state.status in ("stopped", "exiting")

    async def manage_existing(self) -> None:
        """Finish previously opened legs, then leave the strategy stopped."""
        await self.build_runner().run_forever(manage_only=True)
        await self._finish_pause()

    async def _finish_pause(self) -> None:
        from sqlalchemy import update
        from app.db.base import session_scope
        from app.db.models import StrategyState

        async with session_scope() as session:
            await session.execute(update(StrategyState).where(
                StrategyState.key == self.key, StrategyState.status == "exiting"
            ).values(status="stopped"))

    async def _equity(self) -> float:
        """Sizing equity: starting equity + every finished session's realized P&L
        (net of fees). Sessions with an unknown result (kill switch) count as 0."""
        from app.strategies.xsp_iron_fly import store
        return self.params.starting_equity + await store.realized_total(self.key)

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

        return {"mode": self.params.mode, "halt": await store.halt_reason(key=self.key),
                "equity": round(await self._equity(), 2),
                "starting_equity": self.params.starting_equity,
                "sessions": [view(s) for s in recent]}


class SpyIronFlyStrategy(XspIronFlyStrategy):
    key = "spy_iron_fly"
    name = "SPY 0DTE Iron Fly"
    universe = ("SPY",)
    description = ("Sells a same-day iron fly on SPY at 09:45 ET with a separate stop on each "
                   "short option. Closes remaining contracts before expiry on Alpaca paper.")
    rules = [
        "Once per trading day, select the standard 100-share SPY options expiring that day.",
        "Sell the nearest listed at-the-money call and put, and buy wings nearest 10 points away.",
        "Enter all four legs as one net-credit limit order. Skip when prices, spread or risk checks fail.",
        "Buy back a short leg when its ask triggers the 1.5× entry-fill stop and passes the quote checks.",
        "Begin closing 30 minutes before the regular equity session ends. Cover shorts before "
        "selling protective wings. An unresolved close requires broker reconciliation.",
        "Size one fly per $5,000 of strategy equity, subject to buying power and the per-fly loss limit.",
    ]
    entry_logic = "09:45 ET: sell the at-the-money SPY iron fly with 10-point wings."
    exit_logic = ("15:30 ET on regular sessions: close all remaining legs. Intraday, each short "
                  "has its own 1.5× stop. The close moves earlier on a shortened session.")
    selection_criteria = "SPY ETF options expiring today, standard 100-share deliverable."
    backtest_note = "SPY option replays use historical SPY prices and pre-expiry closing fills."

    @property
    def params(self) -> IronFlyParams:
        return self._params or IronFlyParams.for_spy()


class ArchivedXspIronFlyStrategy(XspIronFlyStrategy):
    """Keep the old key for its history and to finish positions from before the switch."""
    archived = True
    name = "XSP 0DTE Iron Fly (archived)"
    description = "Historical XSP iron fly sessions and replays. New iron fly entries use SPY."

    def activation_block_reason(self) -> str:
        return "XSP is archived. New iron fly entries use SPY."

    async def run_live(self) -> None:
        await self.build_runner().run_forever(manage_only=True)
