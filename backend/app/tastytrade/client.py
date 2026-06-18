"""Thin wrapper around the tastytrade SDK.

Responsibilities
----------------
* Build an OAuth :class:`tastytrade.Session` from the server-side provider secret
  plus the user-supplied refresh token.
* Guarantee the access token is fresh before every call (refresh-on-expiry, not a
  timer) by delegating to ``Session.refresh()`` which is a no-op when still valid.
* Expose the account/market-data surface the rest of the app needs.

The live session itself is owned by :data:`app.auth.manager.session_manager`; this
wrapper reads it from there so there is a single source of truth and the kill
switch / logout can invalidate it in one place.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.auth.manager import session_manager
from app.config import get_settings

logger = logging.getLogger("trading.tastytrade")
settings = get_settings()


class OAuthNotConfigured(RuntimeError):
    """Raised when the provider secret is missing — the app booted without it."""


class NotAuthenticated(RuntimeError):
    """Raised when an authenticated call is attempted with no live session."""


async def build_session(refresh_token: str, provider_secret: str | None = None):
    """Create and validate a Tastytrade OAuth session from a refresh token.

    The OAuth client/provider secret may come from the request (entered in the
    auth panel) or fall back to the server's configured ``TASTYTRADE_PROVIDER_SECRET``.
    A panel-supplied secret is stored only inside the server-side ``Session`` object
    (which reuses it for auto-refresh) and is never returned to the client.

    Performs the OAuth exchange immediately (force refresh) so an invalid refresh
    token / secret fails *here*, at login, rather than later on first use.
    """
    secret = provider_secret or settings.tastytrade_provider_secret
    if not secret:
        raise OAuthNotConfigured(
            "No OAuth client secret available. Enter it in the auth panel or set "
            "TASTYTRADE_PROVIDER_SECRET on the server."
        )
    # Imported lazily so the app can boot even if the SDK has surprises.
    from tastytrade import Session

    session = Session(
        provider_secret=secret,
        refresh_token=refresh_token,
        is_test=settings.tastytrade_is_test,
    )
    await session.refresh(force=True)  # raises on a bad refresh token / secret
    return session


class TastytradeClient:
    """Account + market-data accessor bound to the active server-side session."""

    def _session(self):
        session = session_manager.tasty_session
        if session is None:
            raise NotAuthenticated("No active Tastytrade session.")
        return session

    async def _fresh_session(self):
        """Return the session, refreshing the access token if near expiry."""
        session = self._session()
        await session.refresh()  # no-op unless within 60s of expiry
        return session

    async def get_accounts(self) -> list:
        """All accounts on the authenticated login (``Account.get`` with no
        account number returns the full list)."""
        from tastytrade import Account

        session = await self._fresh_session()
        return await Account.get(session)

    async def get_customer(self) -> dict:
        session = await self._fresh_session()
        return await session.get_customer()

    async def get_positions(self, account) -> list:
        """Positions for an ``Account`` object (as returned by ``get_accounts``).

        Takes the real Account — it can't be reconstructed from the number alone
        (the SDK model has many required fields)."""
        session = await self._fresh_session()
        return await account.get_positions(session)

    # --- market data (DXLink / DXFeed) -----------------------------------
    async def get_quotes(self, symbols: list[str], timeout: float = 4.0) -> dict[str, dict]:
        """One-shot snapshot of last / bid / ask / change / prev-close for ``symbols``.

        Opens a short-lived DXLink stream and collects one Quote, Trade and Summary
        per symbol (or until ``timeout``), then closes. The three event types are
        collected **concurrently** so a symbol that emits no Quote (e.g. cash
        indices like SPX/VIX, which have no order book) doesn't burn the whole
        timeout before Trades are read.

        Degrades gracefully: symbols with no data come back with null fields. For
        indices ``Trade.change`` is often absent, so the daily change is derived
        from ``Summary.prev_day_close_price`` when needed.
        """
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Quote, Summary, Trade

        session = await self._fresh_session()
        symset = set(symbols)
        result: dict[str, dict] = {
            s: {"symbol": s, "last": None, "bid": None, "ask": None, "change": None,
                "change_pct": None, "prev_close": None, "day_volume": None}
            for s in symbols
        }
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, symbols)
            await streamer.subscribe(Trade, symbols)
            await streamer.subscribe(Summary, symbols)
            quotes, trades, summaries = await asyncio.gather(
                self._collect(streamer, Quote, symset, timeout),
                self._collect(streamer, Trade, symset, timeout),
                self._collect(streamer, Summary, symset, timeout),
            )

        for sym, q in quotes.items():
            result[sym]["bid"] = _f(q.bid_price)
            result[sym]["ask"] = _f(q.ask_price)
        for sym, s in summaries.items():
            result[sym]["prev_close"] = _f(s.prev_day_close_price)
        for sym, t in trades.items():
            r = result[sym]
            r["last"] = _f(t.price)
            r["day_volume"] = _f(t.day_volume)
            # Prefer the feed's own change; fall back to last − prev_close (the
            # path that actually fires for indices).
            change = _f(t.change)
            if change is None and r["last"] is not None and r["prev_close"]:
                change = round(r["last"] - r["prev_close"], 4)
            r["change"] = change
            if change is not None and r["prev_close"]:
                r["change_pct"] = round(change / r["prev_close"], 4)
        return result

    # NOTE: historical candles are NO LONGER served here. All historical/backtest
    # market data comes from Massive (see app/providers/massive.py). This client is
    # live-only: quotes, streaming, accounts, positions, and order routing.

    @staticmethod
    async def _collect(streamer, event_class, symbols: set[str], timeout: float) -> dict:
        """Collect one event per symbol from a stream, or until timeout."""
        out: dict = {}
        remaining = set(symbols)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while remaining and (left := deadline - loop.time()) > 0:
            try:
                ev = await asyncio.wait_for(streamer.get_event(event_class), timeout=left)
            except (asyncio.TimeoutError, TimeoutError):
                break
            sym = getattr(ev, "event_symbol", None)
            if sym in remaining:
                out[sym] = ev
                remaining.discard(sym)
        return out


def _f(value) -> float | None:
    """Coerce SDK Decimal/None values to plain float for JSON serialization."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Module-level accessor (kept as a function for easy dependency injection/mocking).
_client = TastytradeClient()


def get_client() -> TastytradeClient:
    return _client
