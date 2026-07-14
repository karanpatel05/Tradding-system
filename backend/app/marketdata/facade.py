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

_SOURCE_TIMEOUT = 10.0
_CHAIN_TTL = 3600.0
_JOURNAL_COOLDOWN = 300.0

_sanity = MidSanity()
_chains: dict[str, tuple[float, dict]] = {}
_last_source: dict[str, str] = {}
_last_source_ts: dict[str, float] = {}
_journal_cooldown: dict[str, float] = {}


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
    """Record which source served `op` (an instrument-scoped key such as
    "stock_quote:QQQ"). The dict write happens before any await so concurrent
    calls for different instruments never race each other. Journaling a
    transition is cooled down per (category, source) so a whole-source flip
    (e.g. 30 option keys transitioning at once) doesn't spam the journal."""
    prev = _last_source.get(op)
    _last_source[op] = source
    _last_source_ts[op] = time.monotonic()
    if prev is None or prev == source:
        return
    category, _, instrument = op.partition(":")
    cooldown_key = f"{category}->{source}"
    now = time.monotonic()
    if now - _journal_cooldown.get(cooldown_key, 0.0) <= _JOURNAL_COOLDOWN:
        return
    _journal_cooldown[cooldown_key] = now
    from app.services.journal import log_event
    label = f"{category} {instrument}".strip()
    await log_event(f"market data: {label} now served by {source} (was {prev})",
                    category="action")


async def _race(op: str, tt_call, ib_call):
    """Try primary then fallback; a result of None/[]/{} is a miss. IBKR is only
    attempted while the Gateway is READY. Exceptions count as misses. `op` is an
    instrument-scoped key (e.g. "stock_quote:QQQ") used for source tracking."""
    order = [("tastytrade", tt_call), ("ibkr", ib_call)]
    if get_settings().data_primary == "ibkr":
        order.reverse()
    for name, call in order:
        if call is None or (name == "ibkr" and not _ib_ready()):
            continue
        try:
            out = await asyncio.wait_for(call(), _SOURCE_TIMEOUT)
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
        d = {"bid": bid, "ask": ask, "last": last, "close": None, "price": price}
        return d if d["price"] is not None else None

    async def ib():
        q = (await _ib_stock_quotes([symbol])).get(symbol)
        return q if q and q.get("price") is not None else None

    return await _race(f"stock_quote:{symbol}", tt, ib)


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

    return await _race(f"chain:{underlying}", tt, ib) or []


async def chain_expiries(underlying: str) -> list[str]:
    async def tt():
        d = await _tt_chain(underlying)
        return d["expiries"] if d else None

    async def ib():
        from app.options.chain import _ib_option_chain as ib_chain
        out = await ib_chain(underlying)
        return out["expirations"] or None

    return await _race(f"chain_expiries:{underlying}", tt, ib) or []


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
            if reason not in (None, "missing"):
                await _warn_sanity(key, q["bid"], q["ask"], reason)
        elif q["price"] is not None:
            # last/close only: no spread to validate, but the jump rule still protects.
            mid, reason = _sanity.check(key, q["price"], q["price"])
            if reason not in (None, "missing"):
                await _warn_sanity(key, q["price"], q["price"], reason)
        else:
            return None
        return mid

    return await _race(f"option_mid:{key}", tt, ib)


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
    if isinstance(end, datetime) and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    async def tt():
        return await _get_streamer().candles(symbol, timeframe, start=start, end=end) or None

    async def ib():
        from app.providers.ibkr_historical import IBKRHistoricalProvider
        return await IBKRHistoricalProvider().get_bars(symbol, timeframe, start=start, end=end)

    return await _race(f"bars:{symbol}", tt, ib) or []


def data_status(window_seconds: float = 900.0) -> dict:
    """Which source has actually served market data recently — for the dashboard's
    Data chip. "serving" is tastytrade > ibkr > idle by recent-hit presence."""
    now = time.monotonic()
    recent: dict[str, int] = {}
    for op, src in _last_source.items():
        if now - _last_source_ts.get(op, 0.0) <= window_seconds:
            recent[src] = recent.get(src, 0) + 1
    serving = ("tastytrade" if recent.get("tastytrade")
               else "ibkr" if recent.get("ibkr") else "idle")
    return {"serving": serving, "recent_sources": recent}
