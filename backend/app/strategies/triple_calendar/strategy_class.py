"""Triple Calendar as an app strategy (ready, options, live loop). Reuses the Phase-1
core; live quotes resolved per-tick into a sync snapshot the core can read."""
from __future__ import annotations

import asyncio
from datetime import date, datetime

from app.config import get_settings
from app.ibkr.gateway import ConnState, get_gateway
from app.services.journal import log_event
from app.services.market import get_market_status
from app.strategies.base import BaseStrategy
from app.strategies.triple_calendar.legs import (atm_strike, expected_move, outer_strikes,
                                                 snap, target_expiry)
from app.strategies.triple_calendar.live import manage_step
from app.strategies.triple_calendar.live_source import LivePriceSource
from app.strategies.triple_calendar.params import TripleCalendarParams
from app.strategies.triple_calendar.store import (close_position, load_open, load_open_id,
                                                  save_position, update_calendars, update_value)


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
    rules = ["Only enter when VIX ≤ 16 (wait for it to fall otherwise)",
             "Sell 21DTE / buy 28DTE calendars at lower/ATM/upper strikes",
             "Outer strikes from the ATM straddle expected move + buffer",
             "At +10% target or -25% stop: roll — close and reopen at the current level (VIX ≤ 16)",
             "Add calendars on a strike breach"]
    entry_logic = "Three calendars around ATM; widths from the expected move. Only when VIX ≤ 16."
    exit_logic = "+10% target / -25% stop both roll into a fresh calendar at the current level (VIX ≤ 16); breach adds a calendar."
    selection_criteria = "Single configured underlying (default QQQ)."

    async def scan(self, universe):  # not a stock-picker
        return []

    async def run_live(self) -> None:
        params = _params()
        src = LivePriceSource()
        position = await load_open(self.key)
        # Recover the record id too, so a restart while holding keeps updating the SAME
        # record (adjust / exit / live valuation) instead of no-op'ing until re-entry.
        record_id = await load_open_id(self.key) if position is not None else None
        executor = _make_executor()
        await log_event("Triple Calendar live loop started", category="action",
                        strategy_key=self.key)
        while True:
            # Pause unless READY AND the regular session is open — no trading on stale quotes.
            if get_gateway().state is not ConnState.READY or not get_market_status()["is_open"]:
                await asyncio.sleep(get_settings().tc_manage_interval_seconds)
                continue
            vix = None
            try:
                today = datetime.now().date()
                spot = await src.aunderlying(params.underlying)
                if spot is not None:
                    # Fetch VIX every tick: a holding tick may roll and must gate its re-entry.
                    vix = await src.avix()
                    # Flat entry is fail-closed: no VIX reading -> don't enter this tick.
                    if position is None and vix is None:
                        await log_event("Triple Calendar: skipping entry — VIX unavailable "
                                        "(fail-closed)", category="decision", strategy_key=self.key)
                    else:
                        strikes = await src.achain(params.underlying)
                        snapshot = await _resolve_snapshot(src, params, spot, strikes, today, position)
                        out = await manage_step(today, snapshot, params, executor, position, vix=vix)
                        position, record_id = await self._persist(out, record_id)
                        # Persist the live valuation the core computed (in-memory snapshot).
                        if position is not None and record_id is not None and position.net_debit:
                            from app.strategies.triple_calendar.strategy import position_value
                            val = position_value(snapshot, position, today)
                            if val is not None:
                                await update_value(record_id, val,
                                                   (val - position.net_debit) / position.net_debit, spot)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await log_event("Triple Calendar tick error", level="warning",
                                category="decision", strategy_key=self.key)
            # Adaptive cadence: fast-poll VIX while flat + above the entry ceiling (or unreadable),
            # otherwise the normal multi-day manage interval.
            poll = get_settings().tc_manage_interval_seconds
            if position is None and (vix is None or vix > params.vix_entry_max):
                poll = get_settings().vix_wait_poll_seconds
            await asyncio.sleep(poll)

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
        if action == "roll" and record_id is not None:
            await close_position(record_id, pnl=out["info"].get("closed_pnl", 0.0))
            reason = out["info"].get("reason", "roll")
            await log_event(f"ROLL close ({reason}) pnl {out['info'].get('closed_pnl', 0.0):.2f}",
                            category="trade", strategy_key=self.key)
            new_pos = out["position"]
            if new_pos is not None:
                rid = await save_position(self.key, new_pos, order_ids=[],
                                          paper=not get_settings().enable_live_orders)
                await log_event(f"ROLL reopen {new_pos.underlying} debit {new_pos.net_debit:.2f}",
                                category="trade", strategy_key=self.key)
                return new_pos, rid
            return None, None
        if action == "exit" and record_id is not None:
            await close_position(record_id, pnl=out["info"].get("pnl", 0.0))
            await log_event(f"EXIT triple calendar ({out['info'].get('reason')})",
                            category="trade", strategy_key=self.key)
            return None, None
        return position, record_id
