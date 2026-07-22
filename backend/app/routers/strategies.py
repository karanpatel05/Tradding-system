"""Strategy routes.

Server-side enforcement of "Coming Soon": activation, capital allocation, and
trade-selection are rejected with 409 when a strategy is not ready. Hiding the
buttons in React is cosmetic — these checks are the real gate.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth.gate import require_auth
from app.auth.manager import session_manager
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import StrategyState
from app.engine.manager import engine_manager
from app.services import backtest_store
from app.services.journal import log_event
from app.strategies.registry import get_strategy, list_strategies

# Read-only views (list, detail, backtest, selection-history) are open so the
# Strategies tab works before connecting a broker (the app lands on the Dashboard
# without a login gate). Only deploying a strategy (activate) requires auth.
router = APIRouter(prefix="/api/strategies", tags=["strategies"])


def _require_ready(key: str):
    """Return the strategy or raise 404/409. The 409 is the Coming-Soon gate."""
    strat = get_strategy(key)
    if strat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown strategy")
    if not strat.ready:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{strat.name} is Coming Soon — it cannot be activated, "
                   f"allocated capital, or selected to trade yet.",
        )
    return strat


async def _state(key: str) -> StrategyState | None:
    async with session_scope() as session:
        return await session.get(StrategyState, key)


def _conn_ready() -> bool:
    """True when the Tastytrade OAuth probe confirms readiness (trading-permitted)."""
    from app.broker.status import is_ready_cached
    return is_ready_cached()


def _market_open() -> tuple[dict, bool]:
    """(status dict, is_open) for the regular session; degrade to closed on error."""
    from app.services.market import get_market_status
    try:
        status = get_market_status()
        return status, bool(status.get("is_open"))
    except Exception:  # noqa: BLE001
        return {}, False


@router.get("")
async def list_all() -> dict:
    out = []
    async with session_scope() as session:
        for strat in list_strategies():
            st = await session.get(StrategyState, strat.key)
            out.append({
                "key": strat.key,
                "name": strat.name,
                "ready": strat.ready,
                "status": st.status if st else "idle",
                "selected_symbol": st.selected_symbol if st else None,
                "capital_allocation": st.capital_allocation if st else 0.0,
                "running": engine_manager.is_running(strat.key),
            })
    return {"strategies": out}


@router.get("/{key}")
async def detail(key: str) -> dict:
    strat = get_strategy(key)
    if strat is None:
        raise HTTPException(status_code=404, detail="Unknown strategy")
    st = await _state(key)
    last_signal, last_trade = await _last_event_times(key)
    position = await _open_position(key)
    trades_today = await _trades_today_count(key)
    running = engine_manager.is_running(key)
    # Live phase: a single strategy holds at most one position at a time. It is
    # "in_position" while that one trade is open, otherwise "scanning" for the
    # next setup while running. It can re-enter after an exit (many trades/day).
    if position is not None:
        phase = "in_position"
    elif running:
        phase = "scanning"
    else:
        phase = st.status if st else "idle"  # stopped / idle
    return {
        "overview": strat.overview(),
        "status": st.status if st else "idle",
        "phase": phase,
        "position": position,
        "trades_today": trades_today,
        "selected_symbol": st.selected_symbol if st else None,
        "direction": getattr(st, "direction", None) or "both",
        "capital_allocation": st.capital_allocation if st else 0.0,
        "contracts": st.contracts if st else 1,
        "tc_tickers": (st.tc_tickers if st else []) or [],
        "running": running,
        "broker_connected": session_manager.is_authenticated(),
        "live_orders_enabled": get_settings().enable_live_orders,
        "last_signal_time": last_signal,
        "last_trade_time": last_trade,
    }


async def _open_position(key: str) -> dict | None:
    """The strategy's currently-open trade (it holds at most one), or None."""
    from sqlalchemy import select

    from app.db.models import TradeRecord

    async with session_scope() as session:
        stmt = (select(TradeRecord)
                .where(TradeRecord.strategy_key == key, TradeRecord.status == "open")
                .order_by(TradeRecord.entry_time.desc()).limit(1))
        t = (await session.execute(stmt)).scalars().first()
    if t is None:
        return None
    return {
        "symbol": t.symbol,
        "side": t.side,
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "stop": t.stop,
        "target": t.target,
    }


@router.get("/{key}/position")
async def position_detail(key: str) -> dict:
    """Full detail + live metrics for the strategy's open position (it holds at
    most one). Includes fill price, TP/SL, a best-effort live quote and the derived
    unrealized P&L, R-multiple and progress toward target. ``has_position`` is
    False when flat."""
    _require_ready(key)
    pos = await _open_position(key)
    if pos is None:
        return {"has_position": False}

    symbol = pos["symbol"]
    side = "long" if pos["side"] == "buy" else "short"
    direction = 1 if side == "long" else -1
    entry = pos.get("entry_price") or 0.0
    stop = pos.get("stop")
    target = pos.get("target")
    qty = pos.get("quantity") or 0

    # Best-effort live price (one-shot quote); degrade to null when disconnected.
    live = None
    try:
        from app.marketdata.facade import stock_quote
        q = (await stock_quote(symbol)) or {}
        live = q.get("last")
        if live is None and q.get("bid") is not None and q.get("ask") is not None:
            live = round((q["bid"] + q["ask"]) / 2, 4)
        if live is None:
            live = q.get("price")
    except Exception:  # noqa: BLE001 — never fail the page on a quote hiccup
        logger.exception("position quote fetch failed for %s", symbol)

    out: dict = {
        "has_position": True,
        "strategy_key": key,
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "fill_price": entry,
        "stop": stop,
        "target": target,
        "entry_time": pos.get("entry_time"),
        "live_price": live,
        "notional": round(entry * qty, 2) if entry and qty else None,
    }
    if live is not None and entry:
        out["unrealized_pnl"] = round((live - entry) * qty * direction, 2)
        out["unrealized_pct"] = round((live / entry - 1) * direction, 6)
    if entry and stop is not None:
        out["risk_per_share"] = round(abs(entry - stop), 4)
        if live is not None and abs(entry - stop):
            out["r_multiple"] = round((live - entry) * direction / abs(entry - stop), 3)
    if entry and target is not None:
        out["reward_per_share"] = round(abs(target - entry), 4)
        if live is not None and abs(target - entry):
            # 0 at entry, 1 at target (can exceed 1 / go negative toward stop).
            out["progress_to_target"] = round((live - entry) * direction / abs(target - entry), 4)
    return out


async def _open_option_record(key: str):
    """The strategy's open multi-leg options position record (it holds at most one), or None."""
    async with session_scope() as session:
        from sqlalchemy import select
        from app.db.models import OptionPositionRecord
        return (await session.execute(
            select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == key,
                OptionPositionRecord.status == "open"))).scalars().first()


def _serialize_option_position(rec, contracts: int) -> dict:
    """Position fields for one open OptionPositionRecord (Triple Calendar), P&L scaled by
    ``contracts``. No IBKR calls — reads the persisted record + the loop's live valuation."""
    from datetime import date, datetime, timezone

    from app.strategies.triple_calendar.params import TripleCalendarParams

    p = TripleCalendarParams()
    today = date.today()
    net = rec.net_debit or 0.0
    calendars = []
    for c in rec.calendars or []:
        se = date.fromisoformat(c["sell"])
        be = date.fromisoformat(c["buy"])
        calendars.append({
            "strike": c["strike"],
            "sell_expiry": c["sell"], "buy_expiry": c["buy"],
            "sell_dte": (se - today).days, "buy_dte": (be - today).days,
        })
    strikes = [c["strike"] for c in rec.calendars or []]
    lower_strike = min(strikes) if strikes else None
    upper_strike = max(strikes) if strikes else None

    value = rec.last_value
    pnl_pct = rec.last_pnl_pct
    spot = rec.last_spot
    valued_at = rec.valued_at
    # Stale if the loop hasn't revalued within ~2 manage intervals (market closed / down).
    stale = False
    if valued_at is not None:
        va = valued_at if valued_at.tzinfo else valued_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - va).total_seconds()
        stale = age > 2 * get_settings().tc_manage_interval_seconds

    out = {
        "has_position": True,
        "ticker": rec.underlying,
        "underlying": rec.underlying,
        "status": rec.status,
        "paper": rec.paper,
        "entry_time": rec.entry_time.isoformat() if rec.entry_time else None,
        # entry_time is stored UTC-aware, so measure the hold in UTC too (comparing to the
        # local date could read one day off across the UTC/local boundary). Never negative.
        "days_held": max(0, (datetime.now(timezone.utc).date() - rec.entry_time.date()).days)
                     if rec.entry_time else None,
        "net_debit": round(net, 4),
        "contracts": contracts,
        "calendars": calendars,
        "target_value": round(net * (1 + p.profit_target), 4),
        "stop_value": round(net * (1 - p.stop_loss_pct), 4),
        "profit_target_pct": p.profit_target,
        "stop_loss_pct": p.stop_loss_pct,
        "vix_threshold": p.vix_threshold,
        "value": round(value, 4) if value is not None else None,
        "unrealized_pct": round(pnl_pct, 6) if pnl_pct is not None else None,
        # Per-share calendar debit → dollars, scaled by the number of contracts held.
        "unrealized_pnl": round((value - net) * 100 * contracts, 2) if value is not None else None,
        "spot": round(spot, 4) if spot is not None else None,
        "valued_at": valued_at.isoformat() if valued_at else None,
        "stale": stale,
    }
    if pnl_pct is not None and p.profit_target:
        out["progress_to_target"] = round(pnl_pct / p.profit_target, 4)
    if pnl_pct is not None and p.stop_loss_pct:
        out["distance_to_stop"] = round(max(0.0, -pnl_pct) / p.stop_loss_pct, 4)
    if spot and lower_strike is not None and upper_strike is not None:
        out["breach"] = {
            "lower_strike": lower_strike, "upper_strike": upper_strike,
            "dist_lower_pct": round((lower_strike - spot) / spot, 6),
            "dist_upper_pct": round((upper_strike - spot) / spot, 6),
        }
    else:
        out["breach"] = None
    return out


@router.get("/{key}/options-position")
async def options_position(key: str) -> dict:
    """Triple Calendar detail — one entry per configured ticker (open position or flat/scanning).
    No IBKR calls; reads persisted records + the loop's live valuation. Empty tc_tickers → the
    single fallback ticker (tc_underlying)."""
    _require_ready(key)
    base = {
        "running": engine_manager.is_running(key),
        "broker_connected": session_manager.is_authenticated(),
        "conn_ready": _conn_ready(),
        "market_open": _market_open()[1],
        "live_orders_enabled": get_settings().enable_live_orders,
    }
    last_signal, last_trade = await _last_event_times(key)
    base["last_signal_time"], base["last_trade_time"] = last_signal, last_trade

    st = await _state(key)
    raw = (st.tc_tickers if st else None) or []
    tickers = [{"ticker": str(t["ticker"]).upper(), "contracts": max(1, int(t.get("contracts", 1)))}
               for t in raw if t.get("ticker")]
    if not tickers:
        contracts = max(1, int(getattr(st, "contracts", 1) or 1)) if st else 1
        tickers = [{"ticker": (get_settings().tc_underlying or "QQQ").upper(), "contracts": contracts}]

    from sqlalchemy import select

    from app.db.models import OptionPositionRecord
    out_tickers = []
    async with session_scope() as session:
        for tk in tickers:
            rec = (await session.execute(select(OptionPositionRecord).where(
                OptionPositionRecord.strategy_key == key,
                OptionPositionRecord.underlying == tk["ticker"],
                OptionPositionRecord.status == "open"))).scalars().first()
            if rec is None:
                out_tickers.append({"ticker": tk["ticker"], "contracts": tk["contracts"],
                                    "has_position": False})
            else:
                out_tickers.append(_serialize_option_position(rec, tk["contracts"]))
    return {**base, "tickers": out_tickers}


@router.get("/{key}/options-position/payoff")
async def options_position_payoff(key: str, ticker: str) -> dict:
    """Payoff diagram + stats + net greeks for one open Triple Calendar position.
    Read-only; {"has_position": false} when flat (200, not 404)."""
    from sqlalchemy import select

    from app.db.models import OptionPositionRecord
    from app.options.position_payoff import build_position_payoff

    _require_ready(key)
    tk = ticker.upper()
    st = await _state(key)
    raw = (st.tc_tickers if st else None) or []
    cmap = {str(t["ticker"]).upper(): max(1, int(t.get("contracts", 1)))
            for t in raw if t.get("ticker")}
    contracts = cmap.get(tk, max(1, int(getattr(st, "contracts", 1) or 1)) if st else 1)
    async with session_scope() as session:
        rec = (await session.execute(select(OptionPositionRecord).where(
            OptionPositionRecord.strategy_key == key,
            OptionPositionRecord.underlying == tk,
            OptionPositionRecord.status == "open"))).scalars().first()
    if rec is None:
        return {"has_position": False, "ticker": tk}
    return build_position_payoff(rec, contracts)


async def _trades_today_count(key: str) -> int:
    """Trades this strategy has taken today (open + closed), by entry/exit date."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db.models import TradeRecord

    today = datetime.now(timezone.utc).date()
    async with session_scope() as session:
        rows = (await session.execute(
            select(TradeRecord).where(TradeRecord.strategy_key == key)
        )).scalars().all()
    return sum(1 for t in rows if (t.entry_time or t.exit_time)
               and (t.entry_time or t.exit_time).date() == today)


async def _last_event_times(key: str) -> tuple[str | None, str | None]:
    """Most-recent signal-category and trade-category log timestamps for a strategy."""
    from sqlalchemy import select

    from app.db.models import LogEntry

    async def latest(category: str) -> str | None:
        async with session_scope() as session:
            stmt = (select(LogEntry.ts)
                    .where(LogEntry.strategy_key == key, LogEntry.category == category)
                    .order_by(LogEntry.ts.desc()).limit(1))
            ts = (await session.execute(stmt)).scalars().first()
            return ts.isoformat() if ts else None

    return await latest("signal"), await latest("trade")


class AllocationBody(BaseModel):
    amount: float = Field(..., ge=0, description="Dollar capital allocated to this strategy")


@router.put("/{key}/allocation")
async def set_allocation(key: str, body: AllocationBody) -> dict:
    _require_ready(key)  # rejected for Coming-Soon strategies
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        st.capital_allocation = body.amount
    await log_event(f"Capital allocation set to ${body.amount:,.2f}",
                    category="action", strategy_key=key)
    return {"key": key, "capital_allocation": body.amount}


class ContractsBody(BaseModel):
    contracts: int = 1  # clamped to >= 1 in the handler (0/neg → 1)


@router.put("/{key}/contracts")
async def set_contracts(key: str, body: ContractsBody) -> dict:
    """How many of the whole spread to open per entry. Applies to the NEXT entry; an open
    position keeps the contracts it was opened with."""
    strat = _require_ready(key)  # rejected for Coming-Soon strategies
    n = max(1, int(body.contracts))
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        if st is None:
            st = StrategyState(key=key, name=strat.name, ready=True)
            session.add(st)
        st.contracts = n
    await log_event(f"Contracts set to {n}", category="action", strategy_key=key)
    return {"key": key, "contracts": n}


class TickerItem(BaseModel):
    ticker: str
    contracts: int = 1


class TickersBody(BaseModel):
    tickers: list[TickerItem]


def _normalize_tickers(raw: list) -> list[dict]:
    """Uppercase, clamp contracts >=1, drop blanks, de-dupe (first wins), cap at 10."""
    out: list[dict] = []
    seen: set[str] = set()
    for it in raw:
        t = (it.ticker or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append({"ticker": t, "contracts": max(1, int(it.contracts))})
        if len(out) >= 10:
            break
    return out


@router.get("/{key}/tickers")
async def get_tickers(key: str) -> dict:
    st = await _state(key)
    return {"key": key, "tickers": (st.tc_tickers if st else []) or []}


@router.put("/{key}/tickers")
async def set_tickers(key: str, body: TickersBody) -> dict:
    strat = _require_ready(key)
    tickers = _normalize_tickers(body.tickers)
    # A ticker with an open position cannot be dropped: the live loop only manages
    # configured tickers, so removal would orphan the position (no stop/target enforcement)
    # while its IBKR legs stay open. Empty list falls back to the single tc_underlying.
    from app.strategies.triple_calendar.store import list_open
    effective = ({t["ticker"] for t in tickers} if tickers
                 else {(get_settings().tc_underlying or "QQQ").upper()})
    orphaned = sorted({r.underlying.upper() for r in await list_open(key)} - effective)
    if orphaned:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot remove ticker(s) with an open position: {', '.join(orphaned)}. "
                   "Wait for the position to exit (or flatten) first.")
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        if st is None:
            st = StrategyState(key=key, name=strat.name, ready=True)
            session.add(st)
        st.tc_tickers = tickers
    await log_event(f"TC tickers set: {', '.join(t['ticker'] for t in tickers) or '(none)'}",
                    category="action", strategy_key=key)
    return {"key": key, "tickers": tickers}


class DirectionBody(BaseModel):
    direction: str = Field(..., description="Trade-direction mode: long | short | both")


@router.put("/{key}/direction")
async def set_direction(key: str, body: DirectionBody) -> dict:
    """Set which sides may OPEN new positions (long / short / both). Live-editable:
    the worker re-reads it each cycle. Changing it only gates NEW entries — open
    positions keep riding their own exit rules (it never force-flattens)."""
    _require_ready(key)  # rejected for Coming-Soon strategies
    direction = (body.direction or "").lower().strip()
    if direction not in ("long", "short", "both"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="direction must be one of: long, short, both")
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        st.direction = direction
    await log_event(f"Direction set to {direction}", category="action", strategy_key=key)
    return {"key": key, "direction": direction}


@router.get("/{key}/positions")
async def positions(key: str) -> dict:
    """All currently-open positions for this (multi-position) strategy, newest
    first. The crossover holds up to one position per watchlist symbol."""
    from sqlalchemy import select

    from app.db.models import TradeRecord

    _require_ready(key)
    async with session_scope() as session:
        rows = (await session.execute(
            select(TradeRecord)
            .where(TradeRecord.strategy_key == key, TradeRecord.status == "open")
            .order_by(TradeRecord.entry_time.desc())
        )).scalars().all()
    out = [{
        "symbol": t.symbol,
        "side": "long" if t.side == "buy" else "short",
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "stop": t.stop,
        "target": t.target,
        "notional": round((t.entry_price or 0) * (t.quantity or 0), 2),
    } for t in rows]
    return {"positions": out, "count": len(out)}


@router.get("/{key}/risk")
async def risk(key: str) -> dict:
    """The strategy's live risk-engine snapshot (open count, used cash/risk, the
    day's realized P&L and the daily-loss-halt state). The running engine is
    worker-local, so this is reconstructed from the DB the same way the worker
    rebuilds it on restart — the numbers match what the worker holds."""
    from app.engine.risk import RiskLimits
    from app.engine.worker import risk_snapshot_from_db

    _require_ready(key)
    st = await _state(key)
    equity = st.capital_allocation if st else 0.0
    snap = await risk_snapshot_from_db(key, equity, RiskLimits.from_settings())
    snap["direction"] = getattr(st, "direction", None) or "both"
    snap["running"] = engine_manager.is_running(key)
    return snap


@router.post("/{key}/activate", dependencies=[Depends(require_auth)])
async def activate(key: str) -> dict:
    strat = _require_ready(key)  # rejected for Coming-Soon strategies
    from app.engine.worker import run_strategy  # lazy: engine wiring

    await engine_manager.start_worker(key, run_strategy(strat))
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        st.status = "scanning"
    await log_event("Strategy activated", category="action", strategy_key=key)
    return {"key": key, "status": "scanning", "running": True}


@router.get("/{key}/backtest")
async def backtest(
    key: str,
    symbol: str | None = None,
    start: str | None = None,
    end: str | None = None,
    capital: float | None = None,
    rr: float | None = None,
    stop_buffer: float | None = None,
    slope_min: float | None = None,
    stop_atr_mult: float | None = None,
    vol_multiplier: float | None = None,
    direction: str | None = None,
) -> dict:
    """Backtest results. Blocked (409) for Coming-Soon strategies — matching the
    spec that backtests aren't shown until rules exist. Query params carry the
    strategy page's backtest-config controls (``direction`` = long/short/both for
    the multi-position crossover)."""
    strat = _require_ready(key)
    from app.backtest import run_backtest

    overrides = {"symbol": symbol, "start": start, "end": end, "capital": capital,
                 "rr": rr, "stop_buffer": stop_buffer, "slope_min": slope_min,
                 "stop_atr_mult": stop_atr_mult, "vol_multiplier": vol_multiplier,
                 "direction": direction}
    overrides = {k: v for k, v in overrides.items() if v is not None}
    result = await run_backtest(strat, symbol, overrides)
    if result.get("data_available"):
        backtest_store.save(key, result)
    return result


@router.get("/{key}/backtest/last")
async def backtest_last(key: str) -> dict:
    """The most recent backtest result for this strategy, persisted locally so it
    reloads after a page/server restart. Returns ``{}`` when none is stored yet."""
    _require_ready(key)
    return backtest_store.load(key) or {}


@router.get("/{key}/backtest/stream")
async def backtest_stream(
    key: str,
    symbol: str | None = None,
    start: str | None = None,
    end: str | None = None,
    capital: float | None = None,
    rr: float | None = None,
    stop_buffer: float | None = None,
    slope_min: float | None = None,
    stop_atr_mult: float | None = None,
    vol_multiplier: float | None = None,
    direction: str | None = None,
) -> StreamingResponse:
    """Same backtest as ``/backtest`` but streams NDJSON progress while the universe
    is scanned, then a final result line. Each line is one JSON object:
    ``{"type":"progress","scanned":i,"total":n,"symbol":...}`` during the scan,
    then ``{"type":"result","result":{...}}`` (or ``{"type":"error","detail":...}``).
    The page renders a determinate progress bar from the progress lines."""
    strat = _require_ready(key)
    from app.backtest import run_backtest

    overrides = {"symbol": symbol, "start": start, "end": end, "capital": capital,
                 "rr": rr, "stop_buffer": stop_buffer, "slope_min": slope_min,
                 "stop_atr_mult": stop_atr_mult, "vol_multiplier": vol_multiplier,
                 "direction": direction}
    overrides = {k: v for k, v in overrides.items() if v is not None}

    queue: asyncio.Queue = asyncio.Queue()

    def on_progress(scanned: int, total: int, sym: str) -> None:
        # Called from the scan loop (same event loop); never blocks.
        queue.put_nowait({"type": "progress", "scanned": scanned, "total": total, "symbol": sym})

    async def runner() -> None:
        try:
            result = await run_backtest(strat, symbol, overrides, on_progress=on_progress)
            if result.get("data_available"):
                backtest_store.save(key, result)  # persist so it reloads after restart
            await queue.put({"type": "result", "result": result})
        except Exception as exc:  # noqa: BLE001 — surface as a stream error, not a 500
            await queue.put({"type": "error", "detail": str(exc) or repr(exc)})
        finally:
            await queue.put(None)  # sentinel: stream complete

    async def generate():
        task = asyncio.create_task(runner())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield json.dumps(item) + "\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


class OptimizeBody(BaseModel):
    grid: dict[str, list[float]] | None = Field(
        default=None, description="param-field → list of values to sweep (None = default grid)")
    directions: list[str] | None = Field(default=None, description="subset of long/short/both")
    objective: str = Field(default="net_profit", description="metric to rank by (higher = better)")
    capital: float = Field(default=2000.0, ge=0)
    # Realistic per-side slippage so the ranking reflects keepable (net-of-cost) profit
    # rather than a cost-free mirage. 0 = gross.
    slippage_per_side_bps: float = Field(default=5.0, ge=0)
    start: str | None = None
    end: str | None = None


@router.post("/{key}/optimize/stream")
async def optimize_stream(key: str, body: OptimizeBody) -> StreamingResponse:
    """Grid-search the strategy's params over the watchlist, ranked by ``objective``.
    Streams NDJSON: ``{"type":"progress",...}`` while the watchlist history is
    fetched once, then ``{"type":"result","result":{...}}`` with the ranked combos
    (or ``{"type":"error",...}``). Multi-position (crossover) only."""
    strat = _require_ready(key)
    if not (getattr(strat, "is_multi_position", False) and hasattr(strat, "optimize")):
        raise HTTPException(status_code=400,
                            detail="Optimization is only available for the multi-position crossover.")
    directions = [d.lower() for d in (body.directions or ["long", "short", "both"])
                  if d.lower() in ("long", "short", "both")] or ["both"]
    symbols = get_settings().watchlist_symbols

    queue: asyncio.Queue = asyncio.Queue()

    def on_progress(scanned: int, total: int, sym: str) -> None:
        queue.put_nowait({"type": "progress", "scanned": scanned, "total": total, "symbol": sym})

    async def runner() -> None:
        try:
            result = await strat.optimize(
                symbols, grid=body.grid, directions=directions, objective=body.objective,
                capital=body.capital, start=body.start, end=body.end,
                slippage_per_side_bps=body.slippage_per_side_bps, on_progress=on_progress)
            await queue.put({"type": "result", "result": result})
        except Exception as exc:  # noqa: BLE001 — surface as a stream error, not a 500
            await queue.put({"type": "error", "detail": str(exc) or repr(exc)})
        finally:
            await queue.put(None)

    async def generate():
        task = asyncio.create_task(runner())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield json.dumps(item) + "\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.get("/{key}/selection-history")
async def selection_history(
    key: str,
    start: str | None = None,
    end: str | None = None,
    capital: float = 2000.0,
) -> dict:
    """Per-day daily-stock-selection history across the universe (Section 8).

    Heavy: scans every universe symbol's cached history. Degrades to an empty list
    with a note when no candle data is cached yet (e.g. before sign-in)."""
    strat = _require_ready(key)
    if not hasattr(strat, "selection_history"):
        return {"days": [], "universe_size": 0, "note": "Not supported for this strategy."}
    from app.engine.universe import get_universe

    universe = await get_universe()
    return await strat.selection_history(universe, start=start, end=end, capital=capital)


@router.post("/{key}/stop")
async def stop(key: str) -> dict:
    if get_strategy(key) is None:
        raise HTTPException(status_code=404, detail="Unknown strategy")
    stopped = await engine_manager.stop_worker(key)
    async with session_scope() as session:
        st = await session.get(StrategyState, key)
        if st:
            st.status = "stopped"
            st.selected_symbol = None
    await log_event("Strategy stopped", category="action", strategy_key=key)
    return {"key": key, "status": "stopped", "was_running": stopped}
