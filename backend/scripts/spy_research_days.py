"""Price one completed SPY session under the published one-cent Research assumptions."""
from datetime import date, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

import httpx

from app.options.blackscholes import bs_price, implied_vol
from app.strategies.qqq_credit_spread.logic import occ_symbol


ET = ZoneInfo("America/New_York")
ENTRY = time(9, 45)
ENTRY_WINDOW = timedelta(minutes=10)
HALF_SPREAD = 0.005
FEE = 0.035


def _bars(client, path, **params):
    """Alpaca pages across all requested symbols; only a successful final page completes the query."""
    result = {}
    seen = set()
    while True:
        response = client.get(path, params=params)
        response.raise_for_status()
        page = response.json()
        for symbol, bars in (page["bars"] or {}).items():
            result.setdefault(symbol, []).extend(bars)
        token = page.get("next_page_token")
        if not token:
            return result
        if token in seen:
            raise ValueError("Repeated Alpaca bar pagination token")
        seen.add(token)
        params["page_token"] = token


def _stock_bars(client, timeframe, span):
    query = dict(span, timeframe=timeframe, symbols="SPY", adjustment="raw", feed="sip")
    try:
        data = _bars(client, "/v2/stocks/bars", **query)
    except httpx.HTTPStatusError as exc:
        # The Research study uses IEX when the account lacks SIP entitlement.
        if exc.response.status_code != 403:
            raise
        query["feed"] = "iex"
        data = _bars(client, "/v2/stocks/bars", **query)
    return data.get("SPY", [])


def _timestamp(bar):
    return datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))


def _entry_bar(bars, entry):
    return min((b for b in bars if entry <= _timestamp(b) <= entry + ENTRY_WINDOW),
               key=_timestamp, default=None)


def _has_closing_bars(bars, close):
    return any(close - timedelta(minutes=5) <= _timestamp(b) <= close for b in bars)


def _intrinsic(kind, spot, strike):
    return max(0.0, spot - strike if kind == "C" else strike - spot)


def _short_exit(kind, bars, entry, close, entry_price, strike, spot_close):
    stop = 1.5 * entry_price
    # Match the saved study's minute-high trigger and gap-through open, including
    # the minute after the equity close. A held leg expires at intrinsic value.
    for bar in sorted(bars, key=_timestamp):
        if entry < _timestamp(bar) <= close + timedelta(minutes=1) and bar["h"] >= stop:
            return round(max(stop, bar["o"]), 4) + HALF_SPREAD, 1
    value = _intrinsic(kind, spot_close, strike)
    return value, int(value > 0)


def price_session(client: httpx.Client, session: dict) -> tuple[str | None, str | None]:
    """Return (net USD, None) or (None, entry-gap reason); incomplete retrieval raises."""
    day = date.fromisoformat(session["date"])
    entry = datetime.combine(day, ENTRY, ET)
    close = datetime.combine(day, time.fromisoformat(session["close"]), ET)
    # The final relevant bar opens one minute after the exchange close. Bounding
    # the request also avoids querying the provider's embargoed/future hours.
    span = {"start": f"{day}T00:00:00Z", "end": (close + timedelta(minutes=1)).isoformat(),
            "limit": 10_000}
    stock = _stock_bars(client, "1Min", span)
    daily = [b for b in _stock_bars(client, "1Day", span)
             if _timestamp(b).astimezone(ET).date() == day]
    if (len(daily) != 1 or not isfinite(daily[0]["c"]) or daily[0]["c"] <= 0
            or not _has_closing_bars(stock, close)):
        raise ValueError(f"Incomplete SPY closing data for {day}")
    spot_close = round(daily[0]["c"], 4)
    stock_entry = _entry_bar(stock, entry)
    if not stock_entry:
        return None, "no_stock_entry"
    spot = stock_entry["vw"]
    if not isfinite(spot) or spot <= 0:
        raise ValueError(f"Invalid SPY entry price for {day}")
    body = round(spot)
    strikes = {"C": body + 10, "P": body - 10}
    short_symbols = {k: occ_symbol(day, k, body, "SPY") for k in strikes}
    wing_symbols = {k: occ_symbol(day, k, strike, "SPY") for k, strike in strikes.items()}
    symbols = sorted([*short_symbols.values(), *wing_symbols.values()])
    options = _bars(client, "/v1beta1/options/bars", timeframe="1Min", symbols=",".join(symbols), **span)
    if not any(_has_closing_bars(bars, close) for bars in options.values()):
        # An opening ATM fly can be far from the money by the close. Check the
        # closing ATM contracts before accepting a quiet wing or a missing entry
        # as genuine, in case the provider has only published part of the day.
        closing_atm = sorted(occ_symbol(day, k, round(spot_close), "SPY") for k in strikes)
        check = _bars(client, "/v1beta1/options/bars", timeframe="1Min",
                      symbols=",".join(closing_atm), **dict(span, start=(close - timedelta(minutes=5)).isoformat()))
        if not any(_has_closing_bars(bars, close) for bars in check.values()):
            raise ValueError(f"Incomplete SPY option closing data for {day}")
    body_bars = {k: options.get(sym, []) for k, sym in short_symbols.items()}
    if not all(body_bars.values()):
        return None, "missing_option_bars"
    bars = {k: _entry_bar(v, entry) for k, v in body_bars.items()}
    if not all(bars.values()):
        return None, "no_option_entry"
    premiums = {k: bar["vw"] for k, bar in bars.items()}
    if any(not isfinite(p) or p <= 0 for p in premiums.values()):
        return None, "bad_entry_price"

    years = (close - entry).total_seconds() / (365 * 86400)
    vols = [implied_vol(p, spot, body, years, 0, k, lo=0.0001, hi=10, tol=1e-10)
            for k, p in premiums.items() if p > _intrinsic(k, spot, body) + 1e-6]
    available = [vol for vol in vols if vol is not None]
    iv = sum(available) / len(available) if available else 0
    points = 0.0
    charges = 4  # four opening fills, plus stop fills and ITM exercises/assignments
    for k, strike in strikes.items():
        price, extra_fee = _short_exit(k, body_bars[k], entry, close, premiums[k], body, spot_close)
        points += round(premiums[k], 4) - HALF_SPREAD - price
        wing = _entry_bar(options.get(wing_symbols[k], []), entry)
        wing_price = (wing["vw"] if wing and wing["vw"] > 0
                      else bs_price(spot, strike, years, iv, 0, k))
        wing_value = _intrinsic(k, spot_close, strike)
        points += wing_value - (round(wing_price, 4) + HALF_SPREAD)
        charges += extra_fee + int(wing_value > 0)
    pnl = points * 100 - charges * FEE
    if not isfinite(pnl):
        raise ValueError(f"Invalid SPY option prices for {day}")
    return f"{pnl:.6f}", None
