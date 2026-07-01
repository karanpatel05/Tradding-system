"""Triple Calendar as an app strategy (ready, options, live loop). Reuses the Phase-1
core; live quotes resolved per-tick into a sync snapshot the core can read."""
from __future__ import annotations

import asyncio
from datetime import date, datetime

from app.config import get_settings
from app.services.journal import log_event
from app.strategies.base import BaseStrategy
from app.strategies.triple_calendar.legs import (atm_strike, expected_move, outer_strikes,
                                                 snap, target_expiry)
from app.strategies.triple_calendar.live import manage_step
from app.strategies.triple_calendar.live_source import LivePriceSource
from app.strategies.triple_calendar.params import TripleCalendarParams
from app.strategies.triple_calendar.store import (close_position, load_open, save_position,
                                                  update_calendars)


def _params() -> TripleCalendarParams:
    s = get_settings()
    events = [date.fromisoformat(d.strip()) for d in s.tc_event_dates.split(",") if d.strip()]
    return TripleCalendarParams(underlying=s.tc_underlying, event_dates=events)


class _Snapshot:
    """Sync price-source view for the core, filled from live IBKR values per tick."""

    def __init__(self, spot, strikes, opt):
        self._spot, self._strikes, self._opt = spot, strikes, opt

    def underlying_close(self, sym, day):
        return self._spot

    def chain_strikes(self, u, e):
        return self._strikes

    def option_close(self, u, expiry, strike, right, day):
        return self._opt.get((expiry, strike, right))


def _make_executor():
    from app.engine.execution import submit_combo_order

    async def _ex(underlying, legs, **kw):
        return await submit_combo_order(underlying, legs, **kw)

    return _ex


async def _resolve_snapshot(src: LivePriceSource, params, spot, strikes, today, position):
    """Pre-fetch the option mids the core will need this tick, into a _Snapshot."""
    sell_e = target_expiry(today, params.sell_dte)
    buy_e = target_expiry(sell_e, 7)
    atm = snap(atm_strike(spot, params.strike_round), strikes)
    wanted = {(sell_e, atm, "C"), (sell_e, atm, "P"), (buy_e, atm, "C")}
    for cal in (position.calendars if position else []):
        wanted |= {(cal.sell_expiry, cal.strike, "C"), (cal.buy_expiry, cal.strike, "C")}
    if position is None:
        c = await src.aoption_mid(params.underlying, sell_e, atm, "C")
        pmid = await src.aoption_mid(params.underlying, sell_e, atm, "P")
        if c and pmid:
            lo, up = outer_strikes(atm, expected_move(c, pmid), params.em_buffer,
                                   params.strike_round, params.manual_nudge)
            for sk in (snap(lo, strikes), snap(up, strikes)):
                wanted |= {(sell_e, sk, "C"), (buy_e, sk, "C")}
    opt = {}
    for (e, k, r) in wanted:
        opt[(e, k, r)] = await src.aoption_mid(params.underlying, e, k, r)
    return _Snapshot(spot, strikes, opt)


class TripleCalendarStrategy(BaseStrategy):
    key = "strategy_2"
    name = "Triple Calendar"
    ready = True
    is_options = True
    description = "Delta-neutral, +vega three-calendar spread on a configured underlying."
    rules = ["Sell 21DTE / buy 28DTE calendars at lower/ATM/upper strikes",
             "Outer strikes from the ATM straddle expected move + buffer",
             "Close at +10% of net debit or -25% stop-loss; add calendars on a strike breach"]
    entry_logic = "Three calendars around ATM; widths from the expected move."
    exit_logic = "10% debit target / -25% stop-loss / breach adjustments."
    selection_criteria = "Single configured underlying (default QQQ)."

    async def scan(self, universe):  # not a stock-picker
        return []

    async def run_live(self) -> None:
        params = _params()
        src = LivePriceSource()
        position = await load_open(self.key)
        record_id = None
        executor = _make_executor()
        await log_event("Triple Calendar live loop started", category="action",
                        strategy_key=self.key)
        while True:
            try:
                today = datetime.now().date()
                spot = await src.aunderlying(params.underlying)
                if spot is not None:
                    strikes = await src.achain(params.underlying)
                    snapshot = await _resolve_snapshot(src, params, spot, strikes, today, position)
                    out = await manage_step(today, snapshot, params, executor, position)
                    position, record_id = await self._persist(out, record_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await log_event("Triple Calendar tick error", level="warning",
                                category="decision", strategy_key=self.key)
            await asyncio.sleep(get_settings().tc_manage_interval_seconds)

    async def _persist(self, out, record_id):
        action, position = out["action"], out["position"]
        if action == "enter":
            rid = await save_position(self.key, position, order_ids=[],
                                      paper=not get_settings().enable_live_orders)
            await log_event(f"ENTER triple calendar {position.underlying} debit "
                            f"{position.net_debit:.2f}", category="trade", strategy_key=self.key)
            return position, rid
        if action == "adjust" and record_id is not None:
            await update_calendars(record_id, position)
            await log_event(f"ADJUST triple calendar {out['info'].get('side')}",
                            category="trade", strategy_key=self.key)
            return position, record_id
        if action == "exit" and record_id is not None:
            await close_position(record_id, pnl=out["info"].get("pnl", 0.0))
            await log_event(f"EXIT triple calendar ({out['info'].get('reason')})",
                            category="trade", strategy_key=self.key)
            return None, None
        return position, record_id
