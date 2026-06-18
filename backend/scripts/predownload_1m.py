"""Pre-download 1-minute historical bars for the watchlist from Massive (Polygon).

Fetches ~2 years of 1m candles for each configured watchlist symbol and writes
them into the app's local history cache (``data_cache/massive__SYMBOL__1m.json``),
the exact files/namespace the backtest reads — so once this finishes a 1m backtest
runs entirely from disk.

Run (needs MASSIVE_API_KEY set, e.g. in backend/.env):
    python -m scripts.predownload_1m
    python -m scripts.predownload_1m --years 2 --interval 1m TSLA NVDA   # overrides

Sequential per symbol (gentle on rate limits); the provider already handles
pagination + 429 backoff. Re-running overwrites with a fresh pull.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from app.config import get_settings
from app.providers.massive import MassiveHistoricalProvider
from app.services import history_cache


async def _download(symbols: list[str], interval: str, years: float,
                    min_interval: float, max_retries: int) -> int:
    settings = get_settings()
    if not settings.massive_configured:
        print("ERROR: MASSIVE_API_KEY is not set. Add it to backend/.env "
              "(MASSIVE_API_KEY=...) or export it, then re-run.", file=sys.stderr)
        return 1

    # A bulk-friendly provider: serial requests, proactively paced so a long
    # paginated pull stays under a rate-limited tier (Polygon free = 5/min ≈ 13s).
    provider = MassiveHistoricalProvider(
        settings.massive_api_key, settings.massive_base_url,
        max_concurrency=1, min_request_interval=min_interval, max_retries=max_retries)
    start = datetime.now(timezone.utc).replace(microsecond=0) - _years_delta(years)
    print(f"Provider: {provider.name} | interval={interval} | start={start.date()} "
          f"| symbols={len(symbols)}\n")

    failures: list[str] = []
    for i, symbol in enumerate(symbols, start=1):
        print(f"[{i}/{len(symbols)}] {symbol} … ", end="", flush=True)
        try:
            bars = await provider.get_bars(symbol, interval, start=start)
        except Exception as exc:  # noqa: BLE001 — one bad symbol shouldn't abort the batch
            print(f"FAILED ({exc!r})")
            failures.append(symbol)
            continue
        if not bars:
            print("no bars returned (skipped)")
            failures.append(symbol)
            continue
        history_cache.save(symbol, interval, bars, source=provider.name)
        first = _iso(bars[0]["time"])
        last = _iso(bars[-1]["time"])
        print(f"{len(bars):>8,} bars  {first} → {last}  → "
              f"massive__{symbol}__{interval}.json")

    print()
    if failures:
        print(f"Done with {len(failures)} failure(s): {', '.join(failures)}")
        return 2
    print(f"Done. Cached {interval} history for all {len(symbols)} symbols in "
          f"{settings.history_cache_dir}")
    return 0


def _years_delta(years: float):
    from datetime import timedelta
    return timedelta(days=round(years * 365.25))


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-download intraday history from Massive.")
    ap.add_argument("symbols", nargs="*", help="symbols (default: the configured watchlist)")
    ap.add_argument("--interval", default="1m", choices=["1m", "5m", "15m", "1d"])
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--min-interval", type=float, default=13.0,
                    help="seconds between API requests (free tier 5/min ≈ 13; paid: 0)")
    ap.add_argument("--max-retries", type=int, default=8, help="429 retry budget per request")
    args = ap.parse_args()
    symbols = [s.upper() for s in args.symbols] or get_settings().watchlist_symbols
    return asyncio.run(_download(symbols, args.interval, args.years,
                                 args.min_interval, args.max_retries))


if __name__ == "__main__":
    raise SystemExit(main())
