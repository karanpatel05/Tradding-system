"""Per-strategy async worker.

Each active strategy runs one ``run_strategy`` coroutine, registered under
:data:`engine_manager` so it can be started/stopped/killed independently.

For a **rule-driven** strategy (one exposing ``live_decision``) the loop is a real
trading loop: scan → select the single best-fit symbol → on each freshly-closed
bar run the *shared* ``generate_signal`` → size from allocated capital → place the
(gated) order → record the trade. The signal logic is byte-for-byte the same code
the backtest replays. For any other ready strategy it falls back to the original
scan/select/record loop.

Cancellation (stop / kill switch) propagates as ``CancelledError`` — we let it
bubble so the engine manager records a clean cancel.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.broker.status import is_ready_cached as broker_ready
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import StrategyState, TradeRecord
from app.services.journal import log_event
from app.strategies.base import BaseStrategy

logger = logging.getLogger("trading.worker")
settings = get_settings()


def _trading_allowed() -> bool:
    """Workers act only when the broker probe confirms readiness."""
    return broker_ready()


def _order_failed(order: dict) -> bool:
    """True only for a GENUINE transmission failure (dry-run reject / broker
    unavailable / quantity-skip). ``transmitted: False`` alone is NOT failure — it is
    the normal paper-mode return, which also carries ``paper: True``. Entry paths must
    not open a position, consume risk budget, or record an open trade when this is True."""
    return not order.get("transmitted") and not order.get("paper")


async def _entry_blocked_by_gap(strategy, bars) -> bool:
    """True when the market is open and the symbol's series is missing its latest bar."""
    import time

    from app.services.backfill import interval_seconds, is_stale
    from app.services.market import get_market_status
    secs = interval_seconds(strategy.bar_interval())
    return is_stale(bars, secs, int(time.time() * 1000),
                    market_open=get_market_status()["is_open"])


async def run_strategy(strategy: BaseStrategy) -> None:
    # Self-scheduling strategies (the XSP iron fly) own their whole loop. Checked FIRST
    # so the generic fallback never calls their (empty) scan every minute.
    if hasattr(strategy, "run_live"):
        await strategy.run_live()
        return
    # Multi-position strategies (the EMA crossover) run a watchlist-wide loop and
    # MUST be dispatched before the single-position rule-driven check (both expose
    # ``live_decision``, but the crossover holds one position per symbol).
    if getattr(strategy, "is_multi_position", False):
        await _run_multi_position(strategy)
    elif hasattr(strategy, "live_decision"):
        await _run_rule_driven(strategy)
    else:
        await _run_generic(strategy)


# ============================ rule-driven loop ============================
async def _run_rule_driven(strategy: BaseStrategy) -> None:
    from app.engine.execution import submit_equity_order
    from app.engine.sizing import size_position
    from app.engine.universe import get_universe
    from app.strategies.ema_scalper.signals import State, apply_action

    rebuilt = await _rebuild_rule_state(strategy.key)
    if rebuilt is not None:
        state, selected, open_qty, open_trade_id, last_bar_ms = rebuilt
        await log_event(f"Resumed state: {selected} pos={state.position is not None}",
                        category="action", strategy_key=strategy.key)
    else:
        state, selected, open_qty, open_trade_id, last_bar_ms = State(), None, 0, None, None

    await log_event("Worker started (rule-driven)", category="action", strategy_key=strategy.key)
    try:
        while True:
            if not _trading_allowed():
                await _set_status(strategy.key, "paused")
                await asyncio.sleep(settings.scan_interval_seconds)
                continue
            await _set_status(strategy.key, "scanning")
            try:
                # Only pick a new symbol while flat; hold the symbol once in a trade.
                if state.position is None:
                    best = await strategy.select_best(await get_universe())
                    selected = best.symbol if best else None
                    if best is not None:
                        await _set_selected(strategy.key, selected)
                        await log_event(
                            f"Watchlist top: {selected} ({best.reason})",
                            category="decision", strategy_key=strategy.key, symbol=selected,
                            data={"score": best.score, "metrics": best.metrics},
                        )

                if selected:
                    action, bars = await strategy.live_decision(selected, state)
                    cur_ms = bars[-1].get("time") if bars else None
                    # Gate ENTRIES on a stale series (exits/cancels are never gated).
                    blocked = (action is not None and action.kind in ("arm", "enter")
                               and await _entry_blocked_by_gap(strategy, bars))
                    if blocked:
                        logger.debug("entry gated (awaiting backfill) %s", selected)
                    # Act once per freshly-closed bar (avoid re-acting same bar).
                    if (action is not None and action.kind != "none"
                            and cur_ms != last_bar_ms and not blocked):
                        last_bar_ms = cur_ms
                        open_qty, open_trade_id = await _handle_action(
                            strategy, selected, action, state, bars,
                            open_qty, open_trade_id,
                            size_position, submit_equity_order, apply_action,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad cycle shouldn't kill the worker
                logger.exception("Cycle failed for %s", strategy.key)
                await log_event(f"Cycle error: {exc!r}", level="error",
                                category="decision", strategy_key=strategy.key)

            await _set_status(strategy.key, "active")
            await asyncio.sleep(settings.scan_interval_seconds)
    except asyncio.CancelledError:
        await log_event("Worker cancelled", category="action", strategy_key=strategy.key)
        raise


async def _handle_action(strategy, symbol, action, state, bars, open_qty, open_trade_id,
                         size_position, submit_equity_order, apply_action):
    """Apply one signal action: arm/cancel/enter/exit, with sizing + order + record."""
    idx = len(bars) - 1
    bar = bars[idx]

    if action.kind == "arm":
        apply_action(action, state, idx, bar)
        await log_event(f"Armed {action.side} entry @ {action.price:.2f}",
                        category="signal", strategy_key=strategy.key, symbol=symbol,
                        data={"stop": action.stop, "target": action.target})

    elif action.kind == "cancel":
        apply_action(action, state, idx, bar)
        await log_event("Entry trigger expired/invalidated", category="signal",
                        strategy_key=strategy.key, symbol=symbol)

    elif action.kind == "enter":
        alloc = await _allocation(strategy.key)
        sizing = size_position(alloc, action.price)
        if sizing.quantity <= 0:
            state.pending = None  # can't size it; stand down rather than loop
            await log_event(
                f"Reject {action.side} {symbol} ({action.entry_type}): {sizing.rejection or 'sizes 0 shares'}",
                level="warning", category="decision", strategy_key=strategy.key, symbol=symbol,
                data={"allocation": alloc, "entry": action.price, "reason": sizing.rejection},
            )
            await _persist_worker_state(strategy.key, symbol, state, None, bar.get("time"))
            return 0, None
        intent = "enter_long" if action.side == "long" else "enter_short"
        order = await submit_equity_order(symbol, intent, sizing.quantity, reason="entry")
        if _order_failed(order):
            # Genuine transmission failure: stay flat. Do NOT set a position or record a
            # trade — that would strand a phantom position the broker never opened.
            state.pending = None
            await log_event(
                f"Entry NOT transmitted {action.side} {symbol}: {order.get('detail')}",
                level="error", category="trade", strategy_key=strategy.key, symbol=symbol,
                data={"order": order, "entry": action.price},
            )
            await _persist_worker_state(strategy.key, symbol, state, None, bar.get("time"))
            return 0, None
        apply_action(action, state, idx, bar)  # sets state.position
        trade_id = await _open_trade(strategy.key, symbol, action.side,
                                     sizing.quantity, action.price, bar.get("time"),
                                     action.stop, action.target,
                                     transmitted=bool(order.get("transmitted")))
        await log_event(
            f"ENTER {action.side} {symbol} ({action.entry_type}) x{sizing.quantity} "
            f"@ {action.price:.2f} ({order['detail']})",
            category="trade", strategy_key=strategy.key, symbol=symbol,
            data={"qty": sizing.quantity, "entry_type": action.entry_type, "stop": action.stop,
                  "target": action.target, "capital_used": sizing.notional, "order": order},
        )
        await _persist_worker_state(strategy.key, symbol, state, trade_id, bar.get("time"))
        return sizing.quantity, trade_id

    elif action.kind == "exit" and state.position is not None:
        side = state.position.side
        intent = "exit_long" if side == "long" else "exit_short"
        order = await submit_equity_order(symbol, intent, open_qty, reason=action.reason)
        if settings.enable_live_orders and _order_failed(order):
            # Mirrors _exit_via_signal: the broker may still hold the shares, so book
            # nothing — the position and TradeRecord stay open for the next signal.
            await log_event(
                f"Exit NOT transmitted {side} {symbol}: {order.get('detail')} — position "
                f"left open", level="error", category="trade", strategy_key=strategy.key,
                symbol=symbol, data={"order": order})
            await _persist_worker_state(strategy.key, symbol, state, open_trade_id,
                                        bar.get("time"))
            return open_qty, open_trade_id
        trade = apply_action(action, state, idx, bar)
        pnl = (action.price - trade.entry_price) * open_qty * (1 if side == "long" else -1) if trade else None
        if open_trade_id is not None:
            await _close_trade(open_trade_id, action.price, bar.get("time"),
                               pnl, trade.return_pct if trade else None)
        await log_event(
            f"EXIT {side} {symbol} x{open_qty} @ {action.price:.2f} [{action.reason}] "
            f"pnl={pnl:.2f} ({order['detail']})" if pnl is not None else
            f"EXIT {side} {symbol} [{action.reason}]",
            category="trade", strategy_key=strategy.key, symbol=symbol,
            data={"reason": action.reason, "pnl": pnl, "order": order},
        )
        await _persist_worker_state(strategy.key, symbol, state, None, bar.get("time"))
        return 0, None

    await _persist_worker_state(strategy.key, symbol, state, open_trade_id, bar.get("time"))
    return open_qty, open_trade_id


async def _persist_worker_state(strategy_key, symbol, state, open_trade_id, last_bar_ms) -> None:
    """Persist the rule-driven worker's working memory so a restart resumes mid-trade."""
    from app.engine.worker_state import save_state
    p = state.pending
    armed = None if p is None else {"side": p.side, "trigger": p.trigger, "stop": p.stop,
                                    "target": p.target, "entry_type": p.entry_type}
    await save_state(strategy_key, managed_symbol=symbol, armed=armed,
                     open_trade_id=open_trade_id, last_bar_ms=last_bar_ms)


async def _load_worker_snapshot(strategy_key: str):
    from app.engine.worker_state import load_state
    return await load_state(strategy_key)


async def _rebuild_rule_state(strategy_key: str):
    """Rebuild (state, selected, open_qty, open_trade_id, last_bar_ms) from the durable
    snapshot + the open trade. Returns None when there's nothing to resume."""
    from app.db.base import session_scope
    from app.db.models import TradeRecord
    from app.strategies.ema_scalper.signals import Pending, Position, State

    snap = await _load_worker_snapshot(strategy_key)
    if snap is None:
        return None
    state = State()
    selected = snap["managed_symbol"]
    open_qty, open_trade_id = 0, snap["open_trade_id"]
    if snap["armed"]:
        a = snap["armed"]
        state.pending = Pending(side=a["side"], trigger=a["trigger"], stop=a["stop"],
                                target=a["target"], armed_index=0, entry_type=a.get("entry_type", ""))
    if open_trade_id is not None:
        async with session_scope() as s:
            tr = await s.get(TradeRecord, open_trade_id)
            if tr is not None and tr.status == "open":
                side = "long" if (tr.side or "").lower() in ("buy", "long") else "short"
                state.position = Position(side=side, entry=tr.entry_price or 0.0,
                                          stop=tr.stop or 0.0, target=tr.target or 0.0,
                                          entry_index=0)
                open_qty = int(tr.quantity)
    return state, selected, open_qty, open_trade_id, snap["last_bar_ms"]


# ========================= multi-position loop ===========================
async def _run_multi_position(strategy: BaseStrategy) -> None:
    """Watchlist-wide loop for the EMA crossover: one concurrent position per
    symbol, sized and admitted by the risk engine (shared buying-power pool +
    portfolio limits). Exits are unconditional; entries are risk-gated.

    On (re)start it reconstructs open positions and the day's risk counters from
    the DB so limits and exits survive a restart with up to ``max_positions`` open.
    ``CancelledError`` is allowed to bubble — multi-position is exactly where a
    swallowed cancel would strand real exposure."""
    from app.config import get_settings
    from app.strategies.ema_crossover.signals import State

    watchlist = get_settings().watchlist_symbols
    alloc = await _allocation(strategy.key)
    from app.engine.risk import RiskEngine, RiskLimits
    engine = RiskEngine(alloc, RiskLimits.from_settings())

    # Reconstruct open positions + today's counters so a restart resumes cleanly.
    states, track = await _reconstruct_positions(strategy.key, engine)
    last_entry_ms: dict[str, int | None] = {}

    await log_event(
        f"Worker started (multi-position bracket, watchlist={len(watchlist)}, "
        f"resumed {engine.open_count} open)",
        category="action", strategy_key=strategy.key,
    )
    try:
        while True:
            if not _trading_allowed():
                await _set_status(strategy.key, "paused")
                await asyncio.sleep(settings.scan_interval_seconds)
                continue
            await _set_status(strategy.key, "scanning")
            engine.equity = await _allocation(strategy.key)  # allocation can change live
            direction = await _direction(strategy.key)       # long/short/both, live-editable
            for symbol in watchlist:
                try:
                    state = states.setdefault(symbol, State())
                    action, bars = await strategy.live_decision(symbol, state, direction)
                    cur_ms = bars[-1].get("time") if bars else None

                    if state.position is not None:
                        await _manage_open(strategy, symbol, state, engine, track,
                                           action, bars, cur_ms)
                    else:
                        # Flat → bracketed entry, once per fresh 5m bar. Gate on a stale series.
                        if (action and action.kind == "enter"
                                and cur_ms != last_entry_ms.get(symbol)
                                and not await _entry_blocked_by_gap(strategy, bars)):
                            last_entry_ms[symbol] = cur_ms
                            t = await _open_bracket(strategy, symbol, action, state, bars, engine)
                            if t:
                                track[symbol] = t
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — one bad symbol shouldn't kill the worker
                    logger.exception("Cycle failed for %s/%s", strategy.key, symbol)
                    await log_event(f"Cycle error ({symbol}): {exc!r}", level="error",
                                    category="decision", strategy_key=strategy.key, symbol=symbol)

            await _set_status(strategy.key, "active")
            await asyncio.sleep(settings.scan_interval_seconds)
    except asyncio.CancelledError:
        await log_event("Worker cancelled", category="action", strategy_key=strategy.key)
        raise


async def _open_bracket(strategy, symbol, action, state, bars, engine) -> dict | None:
    """Risk-size + place a bracket (OTOCO) entry. Returns the per-symbol track dict
    (or None when rejected/unsized). The resting stop/target then fill intrabar."""
    from app.engine.execution import submit_bracket_order
    from app.strategies.ema_crossover.signals import apply_action

    idx = len(bars) - 1
    bar = bars[idx]
    sizing = engine.check_and_size(symbol, action.price, action.stop)
    if sizing.quantity <= 0:
        await log_event(
            f"Reject {action.side} {symbol}: {sizing.rejection}",
            level="warning", category="decision", strategy_key=strategy.key, symbol=symbol,
            data={"entry": action.price, "stop": action.stop, "risk": engine.snapshot()},
        )
        return None

    order = await submit_bracket_order(symbol, action.side, sizing.quantity, action.price,
                                       action.stop, action.target, reason="entry")
    if _order_failed(order):
        # Genuine transmission failure: stay flat. Do NOT set a position, consume risk
        # budget, or record a trade — that would strand a phantom (broker holds nothing).
        await log_event(
            f"Entry NOT transmitted {action.side} {symbol}: {order.get('detail')}",
            level="error", category="trade", strategy_key=strategy.key, symbol=symbol,
            data={"order": order, "entry": action.price, "stop": action.stop},
        )
        return None
    apply_action(action, state, idx, bar, symbol)  # sets state.position
    engine.register_open(symbol, sizing.quantity, action.price, action.stop)
    entry_ms = bar.get("time")
    trade_id = await _open_trade(strategy.key, symbol, action.side, sizing.quantity,
                                 action.price, entry_ms, action.stop, action.target,
                                 broker_order_id=order.get("parent_order_id"),
                                 transmitted=bool(order.get("transmitted")))
    await log_event(
        f"ENTER {action.side} {symbol} x{sizing.quantity} @ {action.price:.2f} "
        f"(risk ${sizing.risk_amount:.0f}; bracket stop {action.stop:.2f}/target "
        f"{action.target:.2f}; {order['detail']})",
        category="trade", strategy_key=strategy.key, symbol=symbol,
        data={"qty": sizing.quantity, "stop": action.stop, "target": action.target,
              "capital_used": sizing.notional, "risk": engine.snapshot(), "order": order},
    )
    return {"qty": sizing.quantity, "trade_id": trade_id,
            "broker_id": order.get("parent_order_id"), "entry_ms": entry_ms,
            "last_1m_ms": entry_ms or 0, "last_5m_ms": None}


async def _manage_open(strategy, symbol, state, engine, track, action, bars, cur_ms) -> None:
    """One cycle for a symbol holding a position.
    1) Resolve the resting bracket: broker fill (live) or 1m sim (paper).
    2) A held signal exit (``exit_pending``) is retried every cycle until it books.
    3) Opposite-crossover / EOD exit, once per fresh 5m bar."""
    tr = track.get(symbol, {})
    if await _reconcile_open(strategy, symbol, state, engine, tr):
        track.pop(symbol, None)
        return
    if tr.get("exit_pending"):
        held = tr["exit_action"]
        last_close = bars[-1].get("close") if bars else None
        retry = replace(held, price=last_close) if last_close is not None else held
        if await _exit_via_signal(strategy, symbol, state, engine, tr, retry, bars):
            track.pop(symbol, None)
        return
    if action and action.kind == "exit" and cur_ms != tr.get("last_5m_ms"):
        tr = track.setdefault(symbol, tr)
        tr["last_5m_ms"] = cur_ms
        if await _exit_via_signal(strategy, symbol, state, engine, tr, action, bars):
            track.pop(symbol, None)


async def _reconcile_open(strategy, symbol, state, engine, tr) -> bool:
    """Resolve a position's stop/target. Real: poll the broker bracket. Paper: walk
    the 1m bars since the last check via the shared ``resolve_intrabar_exit`` (so
    paper fills match the backtest). Returns True if the position was closed."""
    from app.config import get_settings
    from app.engine.execution import poll_bracket_fill
    from app.strategies.ema_crossover.signals import resolve_intrabar_exit

    if get_settings().enable_live_orders:
        fill = await poll_bracket_fill(symbol, tr.get("broker_id"), since_ms=tr.get("entry_ms"))
        if not fill:
            return False
        if fill.get("price") is None:
            # The real fill price is unknown. NEVER fabricate the stop as the fill — that
            # books a fake worst-case loss and can trip the daily-loss halt on a phantom.
            if tr.get("exit_pending") and fill.get("reason") == "bracket":
                # Our own held signal exit cancelled the bracket legs, so a DEAD bracket
                # is expected here: let the exit retry instead of tripping SAFE_MODE.
                # (A stop/target that filled without a price is a real exit: below.)
                logger.warning("bracket for %s is dead while its signal exit is held — "
                               "retrying the exit", symbol)
                return False
            if fill.get("reason") == "error":
                # Transient lookup error: leave the position open and retry next cycle.
                logger.warning("bracket fill lookup errored for %s — retrying next cycle", symbol)
                await log_event(f"Bracket fill lookup errored for {symbol} — retrying",
                                level="warning", category="trade",
                                strategy_key=strategy.key, symbol=symbol)
                return False
            # Broker shows flat but no matching fill was found: an unresolvable broker/local
            # mismatch. Escalate to SAFE_MODE for a human rather than guess a price.
            from app.engine.safe_mode import trip_safe_mode
            reason = f"undetermined bracket fill for {symbol} — manual reconcile required"
            await trip_safe_mode(reason)
            await log_event(f"SAFE MODE: {reason}", level="error", category="trade",
                            strategy_key=strategy.key, symbol=symbol)
            return False
        await _finalize_exit(strategy, symbol, state, engine, tr,
                             fill["price"], fill.get("ms"), fill["reason"],
                             detail="broker bracket fill")
        return True

    # Paper: simulate the bracket on 1m bars accumulated since the last check.
    one_min = await strategy._fetch_1m(symbol)
    cursor = tr.get("last_1m_ms") or 0
    recent = [b for b in one_min if (b.get("time") or 0) > cursor]
    if not recent:
        return False
    ex_action, ex_bar = resolve_intrabar_exit(state.position, recent)
    tr["last_1m_ms"] = (ex_bar.get("time") if ex_bar else recent[-1].get("time")) or cursor
    if ex_action is None:
        return False
    await _finalize_exit(strategy, symbol, state, engine, tr, ex_action.price,
                         ex_bar.get("time"), ex_action.reason, detail="paper bracket fill")
    return True


async def _exit_via_signal(strategy, symbol, state, engine, tr, action, bars) -> bool:
    """Opposite-crossover / EOD exit: cancel the resting bracket, market-close, book.
    Live: if the cancel is still pending/blocked or the close genuinely fails to transmit,
    nothing is booked (the broker may still hold the shares) and False is returned so the
    caller keeps the trade open and retries next cycle. Paper behaviour is unchanged."""
    from app.config import get_settings
    from app.engine.execution import cancel_symbol_orders, submit_equity_order

    live = get_settings().enable_live_orders
    side = state.position.side
    cancel = await cancel_symbol_orders(symbol)  # drop the resting OCO before closing
    if live and (cancel.get("pending") or (not cancel.get("paper") and cancel.get("detail"))):
        tr.update(exit_pending=True, exit_action=action)
        await log_event(
            f"Exit HELD {side} {symbol}: resting bracket not cleared "
            f"({cancel.get('detail') or 'cancel still pending'}) — retrying next cycle",
            level="error", category="trade", strategy_key=strategy.key, symbol=symbol,
            data={"cancel": cancel})
        return False
    qty = tr.get("qty", 0)
    if live:
        settled = await _settle_after_cancel(strategy, symbol, state, engine, tr, action, side)
        if settled is not None:
            return settled
        qty = tr.get("qty", 0)      # possibly capped at what the app actually holds
    intent = "exit_long" if side == "long" else "exit_short"
    order = await submit_equity_order(symbol, intent, qty, reason=action.reason,
                                      ref=tr.get("exit_ref"))
    if live and _order_failed(order):
        # Unknown state (the POST may have landed): retry with the SAME ref so Alpaca
        # refuses a duplicate. A definitive rejection gets a fresh order next time.
        tr.update(exit_pending=True, exit_action=action,
                  exit_ref=order.get("ref") if order.get("unknown_state") else None)
        await log_event(
            f"Exit NOT transmitted {side} {symbol}: {order.get('detail')} — position left "
            f"open, retrying next cycle",
            level="error", category="trade", strategy_key=strategy.key, symbol=symbol,
            data={"order": order})
        return False
    await _finalize_exit(strategy, symbol, state, engine, tr, action.price,
                         bars[-1].get("time"), action.reason, detail=order["detail"])
    return True


async def _settle_after_cancel(strategy, symbol, state, engine, tr, action, side) -> bool | None:
    """Live only, after the resting bracket cancel settled and before the market close.
    Returns True/False when the exit is decided here (finalized / held), or None to go
    ahead with the close (with ``tr["qty"]`` capped at the app's actual holding).

    - The stop/target may have filled between the poll and the cancel (the cancel then
      finds nothing and returns clean): re-poll; a priced exit fill is booked and NO
      close is sent; an unpriced one trips SAFE_MODE and holds (never sell twice).
    - The close is capped at the app ledger's net in the trade's direction (a partially
      filled then cancelled entry holds less than the intended qty). Nothing held ->
      close the record with unknown P&L and transmit nothing.
    - A poll or ledger read failure holds the exit (retried next cycle)."""
    import httpx

    from app.broker import ledger
    from app.broker.alpaca_client import AlpacaError, occ_unpadded
    from app.engine.execution import poll_bracket_fill

    def hold(why: str) -> bool:
        tr.update(exit_pending=True, exit_action=action)
        logger.warning("exit for %s held: %s", symbol, why)
        return False

    fill = await poll_bracket_fill(symbol, tr.get("broker_id"), since_ms=tr.get("entry_ms"))
    if fill and fill.get("reason") in ("stop", "target"):
        if fill.get("price") is None:
            from app.engine.safe_mode import trip_safe_mode
            reason = f"bracket {fill['reason']} filled for {symbol} without a fill price — " \
                     f"manual reconcile required"
            await trip_safe_mode(reason)
            await log_event(f"SAFE MODE: {reason}", level="error", category="trade",
                            strategy_key=strategy.key, symbol=symbol)
            return hold("exit child filled without a price")
        await _finalize_exit(strategy, symbol, state, engine, tr, fill["price"],
                             fill.get("ms"), fill["reason"],
                             detail="broker bracket fill (beat the signal exit)")
        return True
    if fill and fill.get("reason") == "error":
        await log_event(f"Exit HELD {side} {symbol}: bracket re-check failed — retrying",
                        level="warning", category="trade", strategy_key=strategy.key,
                        symbol=symbol)
        return hold("bracket re-check failed")
    try:
        net = await ledger.app_net_positions()
    except (AlpacaError, httpx.HTTPError) as exc:   # unknown holding: never guess a qty
        await log_event(f"Exit HELD {side} {symbol}: app position read failed ({exc!r})",
                        level="error", category="trade", strategy_key=strategy.key,
                        symbol=symbol)
        return hold("app ledger unreadable")
    signed = int(net.get(occ_unpadded(symbol), 0) or 0)
    held = signed if side == "long" else -signed
    cap = max(0, min(int(tr.get("qty", 0)), held))
    if cap == 0:
        await _finalize_unpriced(strategy, symbol, state, engine, tr,
                                 f"app ledger holds no {side} {symbol} (net {signed}); "
                                 f"nothing to close, no order sent")
        return True
    if cap < int(tr.get("qty", 0)):
        await log_event(f"Exit {side} {symbol}: closing {cap} (app holds {cap} of "
                        f"{tr.get('qty')})", level="warning", category="trade",
                        strategy_key=strategy.key, symbol=symbol)
        tr["qty"] = cap
    return None


async def _finalize_unpriced(strategy, symbol, state, engine, tr, why: str) -> None:
    """Close the trade without a fill price: state + risk cleared, TradeRecord closed with
    exit_price/pnl unknown (never a fabricated price)."""
    side = state.position.side
    state.position = None
    engine.register_close(symbol, None)
    if tr.get("trade_id") is not None:
        async with session_scope() as session:
            row = await session.get(TradeRecord, tr["trade_id"])
            if row:
                row.status = "closed"
                row.exit_time = datetime.now(timezone.utc)
    await log_event(f"EXIT {side} {symbol} closed without an order: {why}", level="error",
                    category="trade", strategy_key=strategy.key, symbol=symbol,
                    data={"risk": engine.snapshot()})


async def _finalize_exit(strategy, symbol, state, engine, tr, price, exit_ms, reason, *,
                         detail: str) -> None:
    """Shared exit bookkeeping: clear state, book risk P&L, close the TradeRecord, log."""
    from app.strategies.ema_crossover.signals import Action, apply_action

    side = state.position.side
    qty = tr.get("qty", 0)
    bar = {"time": exit_ms, "close": price}
    trade = apply_action(Action("exit", side, price, reason=reason), state, -1, bar, symbol)
    pnl = ((price - trade.entry_price) * qty * (1 if side == "long" else -1)) if trade else None
    engine.register_close(symbol, pnl)
    if tr.get("trade_id") is not None:
        await _close_trade(tr["trade_id"], price, exit_ms, pnl,
                           trade.return_pct if trade else None)
    await log_event(
        (f"EXIT {side} {symbol} x{qty} @ {price:.2f} [{reason}] pnl={pnl:.2f} ({detail})")
        if pnl is not None else f"EXIT {side} {symbol} [{reason}] ({detail})",
        category="trade", strategy_key=strategy.key, symbol=symbol,
        data={"reason": reason, "pnl": pnl, "risk": engine.snapshot()},
    )


# ============================ generic loop ===============================
async def _run_generic(strategy: BaseStrategy) -> None:
    from app.engine.universe import get_universe

    await log_event("Worker started", category="action", strategy_key=strategy.key)
    try:
        while True:
            if not _trading_allowed():
                await _set_status(strategy.key, "paused")
                await asyncio.sleep(settings.scan_interval_seconds)
                continue
            await _set_status(strategy.key, "scanning")
            try:
                best = await strategy.select_best(await get_universe())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Scan failed for %s", strategy.key)
                await log_event(f"Scan error: {exc!r}", level="error",
                                category="decision", strategy_key=strategy.key)
                best = None

            if best is not None:
                await _set_selected(strategy.key, best.symbol)
                await log_event(
                    f"Selected {best.symbol} (score={best.score:.3f})",
                    category="decision", strategy_key=strategy.key, symbol=best.symbol,
                    data={"score": best.score, "reason": best.reason, "metrics": best.metrics},
                )
            else:
                await log_event("No qualifying candidate this scan",
                                category="decision", strategy_key=strategy.key)

            await _set_status(strategy.key, "active")
            await asyncio.sleep(settings.scan_interval_seconds)
    except asyncio.CancelledError:
        await log_event("Worker cancelled", category="action", strategy_key=strategy.key)
        raise


# ============================ helpers ====================================
def _dt(ms: int | None):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _et_date(dt):
    """ET date of a (possibly naive) datetime. SQLite returns naive datetimes;
    treat those as UTC (how they were stored)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("America/New_York")).date()


async def load_open_state(strategy_key: str) -> tuple[list[dict], int, float]:
    """The DB→risk-state read shared by worker restart-reconstruction and the
    ``/risk`` endpoint, so the snapshot the endpoint reports is exactly what the
    worker's in-memory engine holds. Returns:

    * ``open_rows`` — one dict per OPEN trade (symbol/side/quantity/entry/stop/
      target/entry_ms/trade_id),
    * ``trades_today`` — count of trades whose entry is today (ET),
    * ``realized_today`` — summed P&L of trades closed today (ET)."""
    from sqlalchemy import select

    from app.db.models import TradeRecord

    today = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    open_rows: list[dict] = []
    trades_today = 0
    realized_today = 0.0

    async with session_scope() as session:
        rows = (await session.execute(
            select(TradeRecord).where(TradeRecord.strategy_key == strategy_key)
        )).scalars().all()
        for t in rows:
            if _et_date(t.entry_time) == today:
                trades_today += 1
            if t.status == "closed":
                if _et_date(t.exit_time) == today and t.pnl is not None:
                    realized_today += t.pnl
                continue
            etime = t.entry_time
            if etime is not None and etime.tzinfo is None:
                etime = etime.replace(tzinfo=timezone.utc)
            entry = t.entry_price or 0.0
            open_rows.append({
                "symbol": t.symbol,
                "side": "long" if t.side == "buy" else "short",
                "quantity": int(t.quantity or 0),
                "entry_price": entry,
                "stop": t.stop if t.stop is not None else entry,
                "target": t.target if t.target is not None else entry,
                "entry_ms": int(etime.timestamp() * 1000) if etime else None,
                "trade_id": t.id,
                "broker_order_id": t.broker_order_id,
            })
    return open_rows, trades_today, realized_today


async def risk_snapshot_from_db(strategy_key: str, equity: float, limits) -> dict:
    """Reconstruct the strategy's risk-engine snapshot from the DB — used by the
    ``/risk`` endpoint (the live engine is worker-local). Mirrors the worker's
    reconstruction so the numbers match what the running worker holds."""
    from app.engine.risk import RiskEngine

    rows, trades_today, realized_today = await load_open_state(strategy_key)
    engine = RiskEngine(equity, limits)
    for r in rows:
        engine.register_open(r["symbol"], r["quantity"], r["entry_price"], r["stop"],
                             count_trade=False)
    engine.seed_day(trades_today=trades_today, realized_today=realized_today)
    return engine.snapshot()


async def _reconstruct_positions(strategy_key: str, engine) -> tuple[dict, dict]:
    """Rebuild per-symbol crossover state from open trades + seed the risk engine's
    day counters from the DB, so a worker restart resumes (rather than abandons)
    open positions and respects the day's already-used trade/loss budget.

    Returns ``(states, track)`` keyed by symbol, where ``track[symbol]`` =
    ``{qty, trade_id, broker_id, entry_ms, last_1m_ms, last_5m_ms}`` — the live
    bookkeeping the loop needs (broker bracket id for real-mode reconciliation; the
    1m cursor for paper-mode fill simulation, seeded at the entry time)."""
    from app.strategies.ema_crossover.signals import Position, State

    open_rows, trades_today, realized_today = await load_open_state(strategy_key)
    states: dict[str, State] = {}
    track: dict[str, dict] = {}

    for r in open_rows:
        st = State()
        st.position = Position(side=r["side"], entry_price=r["entry_price"], stop=r["stop"],
                               target=r["target"], entry_index=-1, entry_time=r["entry_ms"])
        states[r["symbol"]] = st
        track[r["symbol"]] = {
            "qty": r["quantity"], "trade_id": r["trade_id"], "broker_id": r["broker_order_id"],
            "entry_ms": r["entry_ms"], "last_1m_ms": r["entry_ms"] or 0, "last_5m_ms": None,
        }
        engine.register_open(r["symbol"], r["quantity"], r["entry_price"], r["stop"],
                             count_trade=False)

    engine.seed_day(trades_today=trades_today, realized_today=realized_today)
    return states, track


async def _allocation(key: str) -> float:
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        return st.capital_allocation if st else 0.0


async def _direction(key: str) -> str:
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        return getattr(st, "direction", None) or "both"


async def _open_trade(strategy_key: str, symbol: str, side: str, qty: int,
                      entry_price: float, entry_ms: int | None,
                      stop: float | None = None, target: float | None = None,
                      broker_order_id: str | None = None,
                      transmitted: bool = False) -> int:
    async with session_scope() as session:
        tr = TradeRecord(
            strategy_key=strategy_key, symbol=symbol,
            side="buy" if side == "long" else "sell",
            quantity=qty, entry_price=entry_price, entry_time=_dt(entry_ms),
            stop=stop, target=target, status="open", broker_order_id=broker_order_id,
            transmitted=transmitted,
        )
        session.add(tr)
        await session.flush()
        return tr.id


async def _close_trade(trade_id: int, exit_price: float, exit_ms: int | None,
                       pnl: float | None, return_pct: float | None) -> None:
    async with session_scope() as session:
        tr = await session.get(TradeRecord, trade_id)
        if tr:
            tr.exit_price = exit_price
            tr.exit_time = _dt(exit_ms)
            tr.pnl = pnl
            tr.return_pct = return_pct
            tr.status = "closed"


async def close_open_trades(reason: str = "kill_switch", *,
                            keep_index_symbols: set[str] | None = frozenset()) -> int:
    """Mark every open TradeRecord closed and return how many were closed.

    Used by the kill switch. The dashboard's Open Positions view reads open
    TradeRecords (see ``_strategy_positions_snapshot``), so cancelling workers and
    flattening the broker is not enough — without this the ledger stays "open" and
    the dashboard keeps showing stale positions (guaranteed in paper mode, where
    there is no broker position to clear at all).

    A force-flatten captures no real fill price, so ``exit_price``/``pnl``/
    ``return_pct`` are left None (honest "unknown") rather than booking a
    fabricated $0 — the calendar P&L aggregation skips pnl-None trades. Only
    ``exit_time``/``status`` are stamped so the trade leaves the open set.
    Idempotent: returns 0 when the ledger is already flat.

    ``keep_index_symbols``: transmitted SPY and cash-settled index-option legs whose
    OCC symbol is in this set are left OPEN, because Alpaca still holds them (their
    marketable-limit close didn't fill); None = the broker couldn't be read, so every
    transmitted guarded option leg stays open.
    """
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        rows = (await session.execute(
            select(TradeRecord).where(TradeRecord.status == "open")
        )).scalars().all()
        from app.broker.orders import is_guarded_option
        kept = 0
        for tr in rows:
            if tr.transmitted and is_guarded_option(tr.symbol or "") and (
                    keep_index_symbols is None
                    or tr.symbol.replace(" ", "") in keep_index_symbols):
                kept += 1
                continue
            tr.status = "closed"
            tr.exit_time = now
    n = len(rows) - kept
    if n:
        logger.warning("Kill switch closed %d open trade record(s) [%s]", n, reason)
    if kept:
        logger.error("Kill switch left %d option leg record(s) OPEN: still held at "
                     "Alpaca or unreadable [%s]", kept, reason)
    return n


async def _set_status(key: str, status: str) -> None:
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        if st:
            st.status = status


async def _set_selected(key: str, symbol: str) -> None:
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        if st:
            st.selected_symbol = symbol
