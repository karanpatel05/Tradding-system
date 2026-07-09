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
from app.strategies.triple_calendar.store import (close_position, list_open, load_open,
                                                  save_position, update_calendars, update_value)


def _event_dates() -> list[date]:
    s = get_settings()
    return [date.fromisoformat(d.strip()) for d in s.tc_event_dates.split(",") if d.strip()]


def _params(underlying: str, events: list[date]) -> TripleCalendarParams:
    return TripleCalendarParams(underlying=underlying, event_dates=events)


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
    # Prefetch the entry strikes every tick (not just when flat): a roll fires on a HOLDING
    # tick and reopens by calling decide_entry against this same snapshot, so the fresh
    # outer-strike quotes must already be present even while a position is open.
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

    async def _tickers(self) -> list[dict]:
        """Configured tickers [{ticker, contracts}]. Empty tc_tickers → single fallback
        (tc_underlying at the global contracts setting), preserving legacy single-ticker mode."""
        from app.db.base import session_scope
        from app.db.models import StrategyState
        async with session_scope() as s:
            st = await s.get(StrategyState, self.key)
        raw = (st.tc_tickers if st else None) or []
        cleaned = [{"ticker": str(t["ticker"]).upper(), "contracts": max(1, int(t.get("contracts", 1)))}
                   for t in raw if t.get("ticker")]
        if cleaned:
            return cleaned
        contracts = max(1, int(getattr(st, "contracts", 1) or 1)) if st else 1
        return [{"ticker": (get_settings().tc_underlying or "QQQ").upper(),
                 "contracts": contracts}]

    async def run_live(self) -> None:
        src = LivePriceSource()
        events = _event_dates()
        executor = _make_executor()
        # Seed per-ticker state from any open records (restart-while-holding).
        positions: dict = {}
        record_ids: dict = {}
        for rec in await list_open(self.key):
            positions[rec.underlying] = await load_open(self.key, rec.underlying)
            record_ids[rec.underlying] = rec.id
        await log_event("Triple Calendar live loop started", category="action",
                        strategy_key=self.key)
        while True:
            # Pause unless READY AND the regular session is open — no trading on stale quotes.
            if get_gateway().state is not ConnState.READY or not get_market_status()["is_open"]:
                await asyncio.sleep(get_settings().tc_manage_interval_seconds)
                continue
            vix = None
            tickers = await self._tickers()
            try:
                today = datetime.now().date()
                # Fetch VIX once per tick (market-wide gate shared by all tickers).
                vix = await src.avix()
                for tk in tickers:
                    u, contracts = tk["ticker"], tk["contracts"]
                    params = _params(u, events)
                    pos = positions.get(u)
                    rid = record_ids.get(u)
                    try:
                        spot = await src.aunderlying(u)
                        if spot is None:
                            continue
                        # Flat entry is fail-closed: no VIX reading -> don't enter this ticker.
                        if pos is None and vix is None:
                            await log_event(f"Triple Calendar {u}: skipping entry — VIX "
                                            "unavailable (fail-closed)", category="decision",
                                            strategy_key=self.key)
                            continue
                        strikes = await src.achain(u)
                        snapshot = await _resolve_snapshot(src, params, spot, strikes, today, pos)
                        out = await manage_step(today, snapshot, params, executor, pos,
                                                vix=vix, contracts=contracts)
                        pos, rid = await self._persist(out, rid)
                        positions[u], record_ids[u] = pos, rid
                        # Persist the live valuation the core computed (in-memory snapshot).
                        if pos is not None and rid is not None and pos.net_debit:
                            from app.strategies.triple_calendar.strategy import position_value
                            val = position_value(snapshot, pos, today)
                            if val is not None:
                                await update_value(rid, val,
                                                   (val - pos.net_debit) / pos.net_debit, spot)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 — one ticker's error must not stop the rest
                        await log_event(f"Triple Calendar {u} tick error", level="warning",
                                        category="decision", strategy_key=self.key)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await log_event("Triple Calendar tick error", level="warning",
                                category="decision", strategy_key=self.key)
            # Adaptive cadence: fast-poll while any ticker is flat AND VIX blocks entry.
            vmax = TripleCalendarParams().vix_entry_max
            any_flat_blocked = (vix is None or vix > vmax) and any(
                positions.get(tk["ticker"]) is None for tk in tickers)
            poll = (get_settings().vix_wait_poll_seconds if any_flat_blocked
                    else get_settings().tc_manage_interval_seconds)
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
