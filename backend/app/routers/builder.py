"""Options Strategy Builder API. Read-only market data (chain + quotes) via the
Tastytrade-backed facade; saved trades are tracked (paper) only — NO orders are ever
transmitted."""
from __future__ import annotations

from typing import Literal
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.options import chain as chain_mod
from app.options import store
from app.options.combo import chance_of_profit, combo_value, curve_stats, payoff_curve

router = APIRouter(prefix="/api/builder", tags=["builder"])


class Leg(BaseModel):
    action: Literal["BUY", "SELL"]
    right: Literal["C", "P", "S"]
    strike: float
    expiry: str = ""  # YYYY-MM-DD; ignored (may be "") for stock legs ("S")
    qty: int = 1


class QuoteBody(BaseModel):
    underlying: str
    legs: list[Leg] = Field(min_length=1)
    spot: float | None = None   # enables theoretical (Black-Scholes) fill for unquoted legs
    iv: float = 0.25
    r: float = 0.04


class PayoffBody(BaseModel):
    legs: list[Leg] = Field(min_length=1)
    net_debit: float
    iv: float = 0.25
    r: float | None = None
    eval_date: str | None = None
    lo: float | None = None
    hi: float | None = None
    points: int = 61
    spot: float | None = None   # enables chance-of-profit (lognormal mass over profit regions)


class GreeksBody(BaseModel):
    legs: list[Leg] = Field(min_length=1)
    spot: float
    iv: float = 0.25
    r: float = 0.04
    curve: bool = False
    lo: float | None = None
    hi: float | None = None


class SaveBody(BaseModel):
    underlying: str
    legs: list[Leg] = Field(min_length=1)
    net_debit: float
    iv: float = 0.25
    target_pct: float | None = None
    stop_pct: float | None = None
    contracts: int = Field(1, ge=1)


def _legs(models: list[Leg]) -> list[dict]:
    return [m.model_dump() for m in models]


def _serialize(rec) -> dict:
    return {
        "id": rec.id, "underlying": rec.underlying, "legs": rec.legs,
        "net_debit": rec.net_debit, "iv": rec.iv, "contracts": rec.contracts,
        "target_pct": rec.target_pct, "stop_pct": rec.stop_pct,
        "status": rec.status,
        "entry_time": rec.entry_time.isoformat() if rec.entry_time else None,
        "exit_time": rec.exit_time.isoformat() if rec.exit_time else None,
        "exit_reason": rec.exit_reason, "pnl": rec.pnl,
        "last_value": rec.last_value, "last_pnl": rec.last_pnl,
        "last_pnl_pct": rec.last_pnl_pct,
        "valued_at": rec.valued_at.isoformat() if rec.valued_at else None,
    }


def _strike_span(legs: list[dict]) -> tuple[float, float]:
    strikes = [leg["strike"] for leg in legs if leg["right"] != "S"] or [100.0]
    mid = sum(strikes) / len(strikes)
    return round(mid * 0.8, 2), round(mid * 1.2, 2)


@router.get("/chain")
async def chain(underlying: str) -> dict:
    return await chain_mod.get_option_chain(underlying)


@router.get("/spot")
async def spot(underlying: str) -> dict:
    """Header quote for the Builder: last price + day change vs the previous daily close
    (facade-served; falls back to daily bars when the live quote has no close)."""
    from datetime import datetime, timedelta, timezone

    from app.marketdata import facade

    q = await facade.stock_quote(underlying)
    price = (q or {}).get("price")
    prev = (q or {}).get("close")
    if prev is None and price is not None:
        bars = await facade.bars(underlying, "1d",
                                 start=datetime.now(timezone.utc) - timedelta(days=7))
        closed = [b for b in bars if b.get("close") is not None]
        if closed:
            from zoneinfo import ZoneInfo
            # dxfeed stamps a daily bar at 20:00 ET of the PRIOR calendar day (the
            # extended session start), so the bar's SESSION date = timestamp + 4h in
            # exchange time. Compare session dates, in ET, or "previous close" picks
            # today's own close.
            et = ZoneInfo("America/New_York")
            today = datetime.now(et).date()
            last_day = (datetime.fromtimestamp(closed[-1]["time"] / 1000, tz=et)
                        + timedelta(hours=4)).date()
            # If the newest daily bar is today's (still forming), the previous close is
            # the bar before it; otherwise the newest bar IS the previous close.
            if last_day == today and len(closed) >= 2:
                prev = closed[-2]["close"]
            elif last_day != today:
                prev = closed[-1]["close"]
    out = {"underlying": underlying.upper(), "price": price, "prev_close": prev,
           "change_abs": None, "change_pct": None}
    if price is not None and prev:
        out["change_abs"] = round(price - prev, 4)
        out["change_pct"] = round((price - prev) / prev, 6)
    return out


@router.get("/chain-strikes")
async def chain_strikes(underlying: str, expiry: str) -> dict:
    """Strikes available for one expiry (drives the strike ruler per-expiry)."""
    from app.marketdata import facade
    return {"underlying": underlying.upper(), "expiry": expiry,
            "strikes": await facade.chain_strikes_for(underlying, expiry)}


@router.get("/chain-quotes")
async def chain_quotes(underlying: str, expiry: str, count: int = 8) -> dict:
    count = max(1, min(int(count), 25))  # bound the concurrent quote fan-out
    return await chain_mod.get_chain_quotes(underlying, expiry, count)


@router.post("/quote")
async def quote(body: QuoteBody) -> dict:
    """Live mids per leg; legs without a live quote fall back to their Black-Scholes
    theoretical value when a spot is supplied (weekend/after-hours: the payoff still
    renders). Theo legs are flagged so the UI can mark them and gate saving."""
    legs = _legs(body.legs)
    mids = await chain_mod.get_leg_mids(body.underlying, legs)
    theo = [False] * len(legs)
    for i, leg in enumerate(legs):
        if leg["right"] == "S":
            mids[i] = body.spot if (body.spot or 0) > 0 else None
            theo[i] = False
    if body.spot and body.spot > 0:
        from app.options.blackscholes import bs_price
        today = date.today()
        for i, (leg, mid) in enumerate(zip(legs, mids)):
            if mid is None:
                T = max(0.0, (date.fromisoformat(leg["expiry"]) - today).days / 365.0)
                mids[i] = round(bs_price(body.spot, leg["strike"], T, body.iv, body.r,
                                         leg["right"]), 4)
                theo[i] = True
    out_legs = [{**leg, "mid": mid, "theo": t} for leg, mid, t in zip(legs, mids, theo)]
    value = combo_value(legs, mids)
    missing = [i for i, m in enumerate(mids) if m is None]
    return {"legs": out_legs, "net_debit": None if value is None else round(value, 4),
            "missing": missing, "theoretical": any(theo)}


@router.post("/payoff")
async def payoff(body: PayoffBody) -> dict:
    legs = _legs(body.legs)
    r = body.r if body.r is not None else 0.04
    if body.eval_date:
        ev = date.fromisoformat(body.eval_date)
    else:
        opt_legs = [leg for leg in legs if leg["right"] != "S"]
        ev = (min(date.fromisoformat(leg["expiry"]) for leg in opt_legs)
              if opt_legs else date.today())
    lo, hi = (body.lo, body.hi) if body.lo is not None and body.hi is not None else _strike_span(legs)
    curve = payoff_curve(legs, body.net_debit, ev, body.iv, r, lo, hi, body.points)
    T = max(0.0, (ev - date.today()).days / 365.0)
    cop = chance_of_profit(curve, body.spot, body.iv, r, T)
    return {"curve": curve, "eval_date": ev.isoformat(), "chance_of_profit": cop,
            **curve_stats(curve)}


@router.post("/greeks")
async def greeks(body: GreeksBody) -> dict:
    """Per-leg + net position greeks (x100 x qty for options; stock legs x qty shares
    for delta). Optional delta curve across [lo, hi] for the chart overlay."""
    from app.options.blackscholes import bs_greeks
    legs = _legs(body.legs)
    today = date.today()

    def leg_greeks(leg, S):
        T = 0.0 if leg["right"] == "S" or not leg["expiry"] else max(
            (date.fromisoformat(leg["expiry"]) - today).days, 0) / 365.0
        g = bs_greeks(S, leg["strike"] or S, T, body.iv, body.r, leg["right"])
        sign = 1 if leg["action"] == "BUY" else -1
        mult = leg["qty"] if leg["right"] == "S" else 100 * leg["qty"]
        return {k: sign * mult * v for k, v in g.items()}

    per_leg, net = [], {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        g = leg_greeks(leg, body.spot)
        per_leg.append({**{k: round(v, 4) for k, v in g.items()},
                        "right": leg["right"], "strike": leg["strike"], "action": leg["action"]})
        for k in net:
            net[k] += g[k]
    out = {"net": {k: round(v, 4) for k, v in net.items()}, "legs": per_leg}
    if body.curve:
        lo, hi = (body.lo, body.hi) if body.lo is not None and body.hi is not None \
            else (body.spot * 0.5, body.spot * 1.5)
        pts, step = 81, (hi - lo) / 80
        out["delta_curve"] = [
            {"price": round(lo + i * step, 2),
             "delta": round(sum(leg_greeks(l, lo + i * step)["delta"] for l in legs), 2)}
            for i in range(pts)]
    return out


@router.post("/trades")
async def save_trade(body: SaveBody) -> dict:
    tid = await store.save_trade(body.underlying, _legs(body.legs), body.net_debit,
                                 body.iv, body.target_pct, body.stop_pct, body.contracts)
    return _serialize(await store.get_trade(tid))


@router.get("/trades")
async def list_trades_route(status: str | None = None) -> dict:
    return {"trades": [_serialize(t) for t in await store.list_trades(status)]}


@router.get("/trades/{trade_id}")
async def get_trade_route(trade_id: int) -> dict:
    rec = await store.get_trade(trade_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown trade")
    opt_legs = [leg for leg in rec.legs if leg["right"] != "S"]
    ev = (min(date.fromisoformat(leg["expiry"]) for leg in opt_legs)
          if opt_legs else date.today())
    lo, hi = _strike_span(rec.legs)
    curve = payoff_curve(rec.legs, rec.net_debit, ev, rec.iv, 0.04, lo, hi, 61)
    return {**_serialize(rec), "curve": curve, "eval_date": ev.isoformat(),
            **curve_stats(curve)}


@router.post("/trades/{trade_id}/close")
async def close_trade_route(trade_id: int) -> dict:
    rec = await store.get_trade(trade_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown trade")
    pnl = rec.last_pnl if rec.last_pnl is not None else 0.0
    await store.close_trade(trade_id, pnl=pnl, reason="manual")
    return _serialize(await store.get_trade(trade_id))
