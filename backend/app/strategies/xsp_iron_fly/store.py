"""Persistence for the XSP fly: the per-day session row, per-leg TradeRecords, and the
persisted daily-loss halt flag. The runner holds the session as a plain dict and saves
the whole thing after every state change."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.base import session_scope
from app.db.models import SystemFlag, TradeRecord, XspFlySession

HALT_FLAG = "xsp_fly_halt"

_FIELDS = ("status", "reason", "mode", "ticker", "expiry", "close_time", "spot_at_entry",
           "body_strike", "contracts", "mid_credit", "net_credit", "max_loss", "legs",
           "fills", "extra_pnl", "fees", "realized_pnl", "settlement_price",
           "daily_loss_breached", "reconciliation", "entry_time", "closed_time")


def _db(v):
    """Datetimes are written as UTC: SQLite drops tzinfo, so an ET wall time stored as-is
    would read back as that wall time in UTC (16:00 ET -> 11:00 ET)."""
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(timezone.utc)
    return v


def _aware(dt):
    """SQLite drops tzinfo on read; stored values are UTC."""
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_dict(row: XspFlySession) -> dict:
    d = {"id": row.id, "strategy_key": row.strategy_key, "session_date": row.session_date}
    for f in _FIELDS:
        v = getattr(row, f)
        if isinstance(v, (list, dict)):
            import copy
            v = copy.deepcopy(v)
        d[f] = _aware(v)
    d["legs"] = d["legs"] or []
    d["fills"] = d["fills"] or []
    return d


async def get_session(key: str, d: date) -> dict | None:
    async with session_scope() as s:
        row = (await s.execute(select(XspFlySession).where(
            XspFlySession.strategy_key == key,
            XspFlySession.session_date == d))).scalars().first()
        return _to_dict(row) if row else None


async def create_session(key: str, d: date, **fields) -> dict | None:
    """Insert the day's row. None if one already exists (the idempotency guard)."""
    try:
        async with session_scope() as s:
            row = XspFlySession(strategy_key=key, session_date=d,
                                **{k: _db(v) for k, v in fields.items() if k in _FIELDS})
            s.add(row)
            await s.flush()
            return _to_dict(row)
    except IntegrityError:
        return None


async def save(sess: dict) -> None:
    import copy
    async with session_scope() as s:
        row = await s.get(XspFlySession, sess["id"])
        for f in _FIELDS:
            if f in sess:
                v = sess[f]
                setattr(row, f, copy.deepcopy(v) if isinstance(v, (list, dict)) else _db(v))


async def unfinished_before(key: str, d: date) -> list[dict]:
    """Earlier sessions still to resolve: entering/open, and "error" (a mid-day
    SAFE_MODE stop) so their settlement is booked once Alpaca agrees."""
    async with session_scope() as s:
        rows = (await s.execute(select(XspFlySession).where(
            XspFlySession.strategy_key == key, XspFlySession.session_date < d,
            XspFlySession.status.in_(("entering", "open", "error"))))).scalars().all()
        return [_to_dict(r) for r in rows]


def expected_broker(sess: dict) -> dict[str, int]:
    """Net qty per OCC symbol the session's own ORDER fills imply. Settlement and
    no-bid abandon bookings are not orders, so the broker still holds those legs."""
    to_occ = {l["symbol"]: (l.get("occ") or l["symbol"]).replace(" ", "")
              for l in sess.get("legs") or []}
    out = {sym: 0 for sym in to_occ.values()}
    for f in sess.get("fills") or []:
        if f.get("kind") in ("settle", "abandon"):
            continue
        sym = to_occ.get(f["symbol"], f["symbol"])
        out[sym] = out.get(sym, 0) + (1 if f["side"] == "BUY" else -1) * int(f["qty"])
    return out


async def broker_expected_positions(key: str) -> dict[str, int]:
    """What the app should hold at Alpaca for XSP right now: the order-fill net of every
    alpaca_paper session not yet settled (entering/open/error). Used by engine startup
    recovery instead of XSP's TradeRecords, which don't exist mid-entry and don't track
    a partial stop."""
    out: dict[str, int] = {}
    async with session_scope() as s:
        rows = (await s.execute(select(XspFlySession).where(
            XspFlySession.strategy_key == key, XspFlySession.mode == "alpaca_paper",
            XspFlySession.status.in_(("entering", "open", "error"))))).scalars().all()
        sessions = [_to_dict(r) for r in rows]
    for sess in sessions:
        for sym, q in expected_broker(sess).items():
            out[sym] = out.get(sym, 0) + q
    return {k: v for k, v in out.items() if v}


async def realized_total(key: str) -> float:
    """Sum of realized P&L ($, net of fees) over this strategy's finished Alpaca paper
    sessions. Sessions from the deleted simulator don't count toward equity."""
    from sqlalchemy import func
    async with session_scope() as s:
        v = (await s.execute(select(func.sum(XspFlySession.realized_pnl)).where(
            XspFlySession.strategy_key == key, XspFlySession.mode == "alpaca_paper",
            XspFlySession.realized_pnl.is_not(None)))).scalar()
        return float(v or 0.0)


async def recent_sessions(key: str, limit: int = 10) -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(select(XspFlySession).where(
            XspFlySession.strategy_key == key).order_by(
            XspFlySession.session_date.desc()).limit(limit))).scalars().all()
        return [_to_dict(r) for r in rows]


# --- legs as TradeRecords ---------------------------------------------------------
async def finalize_entry(sess: dict, key: str, now: datetime) -> None:
    """Atomically: one open TradeRecord per filled leg + the session row as open."""
    import copy
    async with session_scope() as s:
        real = sess.get("mode") == "alpaca_paper"
        for leg in sess["legs"]:
            if not leg.get("qty"):
                continue
            # Real Alpaca fills: the OCC symbol (matches the ledger, like QQQ's legs) and
            # transmitted=True so startup reconciliation compares them with Alpaca.
            sym = (leg.get("occ") or leg["symbol"]).replace(" ", "") if real \
                else leg["symbol"][:16]
            tr = TradeRecord(strategy_key=key, symbol=sym,
                             side="sell" if leg["action"] == "SELL" else "buy",
                             quantity=float(leg["qty"]), entry_price=leg["entry_price"],
                             stop=leg.get("stop_level"), entry_time=_db(now), status="open",
                             transmitted=real)
            s.add(tr)
            await s.flush()
            leg["trade_id"] = tr.id
        row = await s.get(XspFlySession, sess["id"])
        for f in _FIELDS:
            if f in sess:
                v = sess[f]
                setattr(row, f, copy.deepcopy(v) if isinstance(v, (list, dict)) else _db(v))


async def close_leg_trade(trade_id: int | None, exit_price: float, pnl: float,
                          now: datetime) -> None:
    if trade_id is None:
        return
    async with session_scope() as s:
        tr = await s.get(TradeRecord, trade_id)
        if tr is None or tr.status != "open":
            return
        tr.status = "closed"
        tr.exit_price = exit_price
        tr.exit_time = _db(now)
        tr.pnl = round(pnl, 2)
        cost = (tr.entry_price or 0) * (tr.quantity or 0) * 100
        tr.return_pct = round(pnl / cost, 6) if cost else None


async def trade_states(trade_ids: list[int]) -> dict[int, dict]:
    if not trade_ids:
        return {}
    async with session_scope() as s:
        rows = (await s.execute(select(TradeRecord).where(
            TradeRecord.id.in_(trade_ids)))).scalars().all()
        return {r.id: {"status": r.status, "pnl": r.pnl, "exit_price": r.exit_price}
                for r in rows}


# --- persisted halt ---------------------------------------------------------------
async def halt_reason() -> str | None:
    async with session_scope() as s:
        row = await s.get(SystemFlag, HALT_FLAG)
        return (row.reason or "halted") if row and row.on else None


async def set_halt(reason: str) -> None:
    async with session_scope() as s:
        row = await s.get(SystemFlag, HALT_FLAG)
        if row is None:
            s.add(SystemFlag(key=HALT_FLAG, on=True, reason=reason))
        else:
            row.on, row.reason = True, reason


async def clear_halt() -> bool:
    async with session_scope() as s:
        row = await s.get(SystemFlag, HALT_FLAG)
        was = bool(row and row.on)
        if row is not None:
            row.on, row.reason = False, ""
        return was
