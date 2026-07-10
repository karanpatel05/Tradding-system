"""Provider-agnostic market data: Tastytrade primary, IBKR fallback (only while the
Gateway is READY), controlled by settings.data_primary. Consumers call these functions
and never import a provider directly. Every option mid passes the sanity guard - a
suspect quote comes back as None, never as a number. Total failure -> None/[], never
an exception into a strategy loop."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from app.config import get_settings
from app.marketdata.sanity import MidSanity

_TT_TIMEOUT = 10.0
_CHAIN_TTL = 3600.0

_sanity = MidSanity()
_chains: dict[str, tuple[float, dict]] = {}
_last_source: dict[str, str] = {}


# Indirection points so tests (and the fallback) can be faked without network.
def _get_streamer():
    from app.tastytrade.streamer import get_streamer
    return get_streamer()


def _ib_ready() -> bool:
    from app.ibkr.gateway import ConnState, get_gateway
    return get_gateway().state is ConnState.READY


async def _ib_stock_quotes(symbols, timeout: float = 3.0):
    from app.ibkr.options_data import ib_stock_quotes
    return await ib_stock_quotes(symbols, timeout=timeout)


async def _note_source(op: str, source: str) -> None:
    prev = _last_source.get(op)
    if prev is not None and prev != source:
        from app.services.journal import log_event
        await log_event(f"market data: {op} now served by {source} (was {prev})",
                        category="action")
    _last_source[op] = source


async def _race(op: str, tt_call, ib_call):
    """Try primary then fallback; a result of None/[]/{} is a miss. IBKR is only
    attempted while the Gateway is READY. Exceptions count as misses."""
    order = [("tastytrade", tt_call), ("ibkr", ib_call)]
    if get_settings().data_primary == "ibkr":
        order.reverse()
    for name, call in order:
        if call is None or (name == "ibkr" and not _ib_ready()):
            continue
        try:
            out = await asyncio.wait_for(call(), _TT_TIMEOUT)
        except Exception:  # noqa: BLE001 - a failed source is a miss, not a crash
            out = None
        if out not in (None, [], {}):
            await _note_source(op, name)
            return out
    return None


# --- stock quotes ---------------------------------------------------------
async def stock_quote(symbol: str) -> dict | None:
    async def tt():
        st = _get_streamer()
        q, last = await asyncio.gather(st.quote(symbol), st.trade_price(symbol))
        if q is None and last is None:
            return None
        bid = q["bid"] if q else None
        ask = q["ask"] if q else None
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        price = last if last is not None else mid
        return {"bid": bid, "ask": ask, "last": last, "close": None, "price": price}

    async def ib():
        return (await _ib_stock_quotes([symbol])).get(symbol)

    out = await _race("stock_quote", tt, ib)
    return out if out and out.get("price") is not None else out


async def stock_quotes(symbols: list[str], timeout: float = 3.0) -> dict[str, dict]:
    results = await asyncio.gather(*[stock_quote(s) for s in symbols])
    empty = {"bid": None, "ask": None, "last": None, "close": None, "price": None}
    return {s: (r or dict(empty)) for s, r in zip(symbols, results)}


# --- option chain ---------------------------------------------------------
async def _tt_chain(underlying: str) -> dict | None:
    """Full TT chain, cached 1h: expiries, strikes and (expiry, strike, right) ->
    streamer-symbol map used by option_mid."""
    now = time.monotonic()
    hit = _chains.get(underlying)
    if hit and now - hit[0] < _CHAIN_TTL:
        return hit[1]
    from app.tastytrade.session import get_tt_session
    s = await get_tt_session()
    if s is None:
        return None
    from tastytrade.instruments import get_option_chain
    chain = await get_option_chain(s, underlying)
    if not chain:
        return None
    by_key: dict = {}
    strikes: set[float] = set()
    for exp, options in chain.items():
        e = exp.isoformat()
        for o in options:
            r = "C" if str(getattr(o.option_type, "value", o.option_type)).upper().startswith("C") else "P"
            k = float(o.strike_price)
            strikes.add(k)
            if o.streamer_symbol:
                by_key[(e, k, r)] = o.streamer_symbol
    data = {"expiries": sorted(e.isoformat() for e in chain),
            "strikes": sorted(strikes), "by_key": by_key}
    _chains[underlying] = (now, data)
    return data


async def chain_strikes(underlying: str) -> list[float]:
    async def tt():
        d = await _tt_chain(underlying)
        return d["strikes"] if d else None

    async def ib():
        from app.ibkr.options_data import ib_chain_strikes
        return await ib_chain_strikes(underlying)

    return await _race("chain", tt, ib) or []


async def chain_expiries(underlying: str) -> list[str]:
    async def tt():
        d = await _tt_chain(underlying)
        return d["expiries"] if d else None

    async def ib():
        from app.options.chain import get_option_chain as ib_chain
        out = await ib_chain(underlying)
        return out["expirations"] or None

    return await _race("chain_expiries", tt, ib) or []


# --- option mid (sanity-guarded for BOTH sources) ---------------------------
async def option_mid(underlying: str, expiry, strike: float, right: str) -> float | None:
    e = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)
    key = f"{underlying}|{e}|{strike:g}|{right.upper()}"

    async def tt():
        d = await _tt_chain(underlying)
        sym = d["by_key"].get((e, float(strike), right.upper())) if d else None
        if not sym:
            return None
        q = await _get_streamer().quote(sym)
        if q is None:
            return None
        mid, reason = _sanity.check(key, q["bid"], q["ask"])
        if reason not in (None, "missing"):
            await _warn_sanity(key, q["bid"], q["ask"], reason)
        return mid

    async def ib():
        from app.ibkr.options_data import ib_option_quote
        q = await ib_option_quote(underlying, expiry, strike, right)
        if q is None:
            return None
        if q["bid"] is not None and q["ask"] is not None:
            mid, reason = _sanity.check(key, q["bid"], q["ask"])
        elif q["price"] is not None:
            # last/close only: no spread to validate, but the jump rule still protects.
            mid, reason = _sanity.check(key, q["price"], q["price"])
        else:
            return None
        if reason not in (None, "missing"):
            await _warn_sanity(key, q["bid"], q["ask"], reason)
        return mid

    return await _race("option_mid", tt, ib)


async def _warn_sanity(key: str, bid, ask, reason: str) -> None:
    from app.services.journal import log_event
    await log_event(f"sanity: rejected mid for {key} ({reason}; bid={bid} ask={ask})",
                    level="warning", category="decision")


# --- VIX --------------------------------------------------------------------
async def vix() -> float | None:
    async def tt():
        st = _get_streamer()
        q = await st.quote("VIX")
        if q and q["bid"] is not None and q["ask"] is not None:
            return (q["bid"] + q["ask"]) / 2
        return await st.trade_price("VIX")

    async def ib():
        from app.ibkr.options_data import ib_vix
        return await ib_vix()

    return await _race("vix", tt, ib)


# --- historical bars --------------------------------------------------------
async def bars(symbol: str, timeframe: str, start, end=None) -> list[dict]:
    if isinstance(start, datetime) and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    async def tt():
        return await _get_streamer().candles(symbol, timeframe, start=start, end=end) or None

    async def ib():
        from app.providers.ibkr_historical import IBKRHistoricalProvider
        return await IBKRHistoricalProvider().get_bars(symbol, timeframe, start=start, end=end)

    return await _race("bars", tt, ib) or []
