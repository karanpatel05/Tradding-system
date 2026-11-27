"""SPY 0DTE iron fly research. Run from backend with python -m scripts.run_spy_fly_backtest.

fetch --start YYYY-MM-DD --end YYYY-MM-DD downloads historical market data.
run --start YYYY-MM-DD --end YYYY-MM-DD replays cached days in an isolated in-memory DB.
"""
import argparse
import asyncio
from datetime import date, datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")
DATABASE = "sqlite+aiosqlite:///:memory:"
ENTRY = time(9, 45)
BAR_MINUTES = 5
# Alpaca's Market Data FAQ defines these eligible OPRA single-leg sale conditions.
# Cancellations, late reports and individual allocations of a multi-leg package
# cannot establish the standalone price of a leg at the replay clock.
OPTION_CONDITIONS = frozenset("IJSabcde")
SPOT_CONDITIONS = {" ", "@", "E", "F", "X"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        try:
            json.dump(value, tmp, separators=(",", ":"), allow_nan=False)
            tmp.close()
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


def stamp(timestamp):
    micros = round(datetime.fromisoformat(timestamp).timestamp() * 1_000_000)
    remainder = re.search(r"\.\d{6}(\d+)", timestamp)
    return micros + int(bool(remainder and int(remainder[1])))


def pages(client, path, field, **params):
    import time as clock
    params["limit"] = 10_000
    while True:
        for attempt in range(4):
            response = client.get(path, params=params)
            if response.status_code != 429 or attempt == 3:
                break
            clock.sleep(2 ** attempt)
        response.raise_for_status()
        payload = response.json()
        yield payload.get(field) or {}
        if not (token := payload.get("next_page_token")):
            return
        params["page_token"] = token


def fetch_day(client, day, close, params):
    from app.backtest.spy_iron_fly import body_at, option_symbols
    from app.services.backtest_reports import SPY_SOURCE
    entry = datetime.combine(day, ENTRY, ET)
    start = entry - timedelta(minutes=BAR_MINUTES)
    deadline = entry + timedelta(seconds=params.entry_grace_seconds)
    bar_start = start.isoformat()
    bar_pages = pages(client, "/v2/stocks/bars", "bars", symbols="SPY", timeframe="5Min",
                      feed="sip", adjustment="raw", start=bar_start, end=bar_start)
    bars = [b for page in bar_pages for b in page.get("SPY", []) if stamp(b["t"]) == stamp(bar_start)]
    spot = [[stamp(entry.isoformat()), bars[0]["c"]]] if bars else []
    stock_pages = pages(client, "/v2/stocks/trades", "trades", symbols="SPY", feed="sip",
                        start=entry.isoformat(), end=deadline.isoformat())
    for page in stock_pages:
        spot.extend([stamp(t["t"]), t["p"]] for t in page.get("SPY", [])
                    if t["p"] > 0 and set(t.get("c") or []) <= SPOT_CONDITIONS)
    # Fetch each possible four-leg fly for the entry window. The runner still
    # picks the ATM body using only the latest spot observed at its own clock.
    strikes = {body_at(price) for _, price in spot}
    contracts = option_symbols(day, strikes, params.wing_width)
    for leg in contracts.values():
        leg["prints"] = []
    if contracts:
        trade_pages = pages(client, "/v1beta1/options/trades", "trades", symbols=",".join(sorted(contracts)),
                            start=(entry - timedelta(seconds=params.max_quote_age)).isoformat(),
                            end=close.isoformat())
        for page in trade_pages:
            for symbol, trades in page.items():
                contracts[symbol]["prints"].extend([stamp(t["t"]), t["p"]] for t in trades
                                                   if t["p"] > 0 and t.get("c") in OPTION_CONDITIONS)
    return {"source": SPY_SOURCE, "ticker": "SPY", "date": day.isoformat(),
            "spot": spot, "contracts": contracts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("fetch", "run"))
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end must be on or after --start")

    # Set before strategy imports, which initialize SQLAlchemy and settings.
    os.environ["DATABASE_URL"] = DATABASE
    from app.config import get_settings
    from app.strategies.xsp_iron_fly import calendar
    from app.strategies.xsp_iron_fly.params import IronFlyParams
    params = IronFlyParams.for_spy()
    if (params.entry_time, params.wing_width) != (ENTRY, 10):
        raise ValueError("SPY replay history requires a 09:45 entry and $10 wings")
    root = Path(get_settings().history_cache_dir) / "spy_bt_alpaca"
    days = []
    day = args.start
    while day <= args.end:
        if not calendar.calendar_configured(day):
            parser.error(f"No exchange calendar configured for {day.year}")
        if calendar.is_trading_day(day):
            days.append(day)
        day += timedelta(days=1)
    if not days or calendar.close_time(days[-1]) > datetime.now(ET):
        parser.error("Select a completed range with trading sessions")
    if args.action == "fetch":
        from app.broker.alpaca_client import headers
        from app.services.backtest_reports import SPY_SOURCE
        with httpx.Client(base_url="https://data.alpaca.markets", headers=headers(), timeout=45) as client:
            for day in days:
                path = root / "days" / f"{day}.json"
                if path.exists() and json.loads(path.read_bytes()).get("source") == SPY_SOURCE:
                    continue
                record = fetch_day(client, day, calendar.close_time(day), params)
                write_json(path, record)
                print(f"{day}: {len(record['contracts'])} SPY contracts", flush=True)
        return
    from app.backtest.spy_iron_fly import replay
    def records():
        for day in days:
            path = root / "days" / f"{day}.json"
            if not path.is_file():
                parser.error(f"Missing {day} history. Fetch the date range first.")
            record = json.loads(path.read_bytes())
            if record.get("date") != day.isoformat():
                parser.error(f"Cached day does not match filename: {day}")
            yield record

    logging.getLogger("trading.xsp_fly").setLevel(logging.ERROR)
    result = asyncio.run(replay(records(), params))
    write_json(root / "results_5m.json", result)
    for width, scenario in result["by_width"].items():
        print(f"{width}: {scenario['aggregate']}", flush=True)


if __name__ == "__main__":
    main()
