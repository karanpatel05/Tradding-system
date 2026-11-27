"""Iron fly market data: 0DTE contracts, raw NBBO, underlying spot and XSP settlement.

The runner only talks to the ``Feed`` protocol, so tests drive it with a scripted fake
and a simulated clock. ``TastytradeFeed`` is the live implementation.

Deliberately NOT the facade's ``option_mid``: its MidSanity guard drops any mid that
moves >50% within 15 minutes, and a 1.5x stop IS a 50% move. Stops read raw bid/ask
straight from the streamer cache; only missing/crossed quotes are ignored
(``logic.quote_valid``).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol

logger = logging.getLogger("trading.xsp_fly.feed")

# Network lookups never get to stall the strategy loop: candle requests queue behind the
# chart's (facade.bars has no outer timeout), and a slow chain fetch must not eat the
# 09:45 entry window. A timeout is a miss (None), retried by the caller.
LOOKUP_TIMEOUT = 15.0


@dataclass(frozen=True)
class Quote:
    bid: float | None
    ask: float | None
    ts: datetime | None          # receive time (aware UTC)

    def age(self, now: datetime) -> float | None:
        return None if self.ts is None else (now - self.ts).total_seconds()


@dataclass(frozen=True)
class Spot:
    price: float
    ts: datetime | None

    def age(self, now: datetime) -> float | None:
        return None if self.ts is None else (now - self.ts).total_seconds()


@dataclass(frozen=True)
class Contract:
    right: str                   # "C" | "P"
    strike: float
    symbol: str                  # streamer symbol, e.g. ".XSP261120C660"
    occ: str                     # broker symbol, e.g. "XSP   261120C00660000"
    stops_trading_at: datetime | None = None


class Feed(Protocol):
    async def contracts(self, ticker: str, expiry: date) -> list[Contract] | None:
        """PM-settled contracts for one expiry. [] = none listed; None = lookup failed."""

    async def subscribe(self, symbols: list[str]) -> None: ...

    def spot(self, ticker: str) -> Spot | None: ...

    def quote(self, symbol: str) -> Quote | None: ...

    async def settlement_price(self, ticker: str, d: date, close_dt: datetime,
                               now: datetime) -> float | None:
        """Cash settlement value for XSP after the close; None for physical delivery."""

    def data_ready(self) -> bool: ...


def _utc(epoch: float | None) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, tz=timezone.utc)


class TastytradeFeed:
    """Live feed: Tastytrade chain + DXLink streamer cache (non-blocking reads)."""

    def __init__(self) -> None:
        self._contracts: dict[tuple[str, date], list[Contract]] = {}

    def _streamer(self):
        from app.tastytrade.streamer import get_streamer
        return get_streamer()

    def data_ready(self) -> bool:
        from app.broker.status import is_ready_cached
        return is_ready_cached()

    async def contracts(self, ticker: str, expiry: date) -> list[Contract] | None:
        key = (ticker, expiry)
        if key in self._contracts:
            return self._contracts[key]
        try:
            from tastytrade.instruments import get_option_chain

            from app.tastytrade.session import get_tt_session
            sess = await get_tt_session()
            if sess is None:
                return None
            chain = await asyncio.wait_for(get_option_chain(sess, ticker), LOOKUP_TIMEOUT)
        except Exception:  # noqa: BLE001 - failed/timed-out lookup is retryable
            logger.warning("%s chain lookup failed", ticker, exc_info=True)
            return None
        best: dict[tuple[str, float], Contract] = {}
        for o in chain.get(expiry, []):
            if (getattr(o, "root_symbol", ticker) or ticker) != ticker:
                continue
            settle = str(getattr(o, "settlement_type", "PM") or "PM").upper()
            if settle != "PM":
                continue
            # The SDK's PM field identifies settlement time for both products.
            # SPY delivers ETF shares; adjusted deliverables don't fit the fly's 100x sizing.
            if ticker == "SPY" and (o.underlying_symbol != ticker or o.shares_per_contract != 100
                                    or o.option_chain_type != "Standard" or not o.active
                                    or o.is_closing_only):
                continue
            if not o.streamer_symbol or not o.symbol:
                continue
            right = ("C" if str(getattr(o.option_type, "value", o.option_type))
                     .upper().startswith("C") else "P")
            k = float(o.strike_price)
            best[(right, k)] = Contract(right=right, strike=k, symbol=o.streamer_symbol,
                                        occ=o.symbol,
                                        stops_trading_at=getattr(o, "stops_trading_at",
                                                                 None))
        out = sorted(best.values(), key=lambda c: (c.strike, c.right))
        self._contracts[key] = out
        return out

    async def subscribe(self, symbols: list[str]) -> None:
        st = self._streamer()
        for sym in symbols:
            try:
                if sym.startswith("."):
                    await st.quote(sym, timeout=0.0)
                else:
                    await st.trade_price(sym, timeout=0.0)
            except Exception:  # noqa: BLE001
                logger.warning("subscribe %s failed", sym, exc_info=True)

    def spot(self, ticker: str) -> Spot | None:
        price, ts = self._streamer().cached_trade(ticker)
        if price is None or price <= 0:
            return None
        return Spot(price=price, ts=_utc(ts))

    def quote(self, symbol: str) -> Quote | None:
        q, ts = self._streamer().cached_quote(symbol)
        if not q:
            return None
        return Quote(bid=q.get("bid"), ask=q.get("ask"), ts=_utc(ts))

    async def settlement_price(self, ticker: str, d: date, close_dt: datetime,
                               now: datetime) -> float | None:
        # A SPY close is a stock price. Only the cash-settled index uses this path.
        if ticker != "XSP" or now < close_dt:
            return None
        s = self.spot(ticker)
        # An index print received after the close is the closing value.
        if s is not None and s.ts is not None and s.ts >= close_dt and d == now.date():
            return s.price
        try:
            from app.marketdata import facade
            bars = await asyncio.wait_for(facade.bars(ticker, "1d", datetime.combine(
                d, datetime.min.time(), tzinfo=timezone.utc)), LOOKUP_TIMEOUT)
        except Exception:  # noqa: BLE001 - incl. TimeoutError: a miss, retried later
            return None
        for b in bars or []:
            t = datetime.fromtimestamp(int(b["time"]) / 1000, tz=close_dt.tzinfo)
            if t.date() == d and b.get("close"):
                return float(b["close"])
        return None
