"""Manual live check of the Polygon options provider (uses the configured key).
NOT a unit test.

    ../.venv/bin/python -m scripts.polygon_options_smoke
"""
from __future__ import annotations

import asyncio
from datetime import date

from app.options import get_options_provider
from app.options.base import PolygonNotAuthorized


async def main() -> int:
    p = get_options_provider()
    chain = await p.list_contracts("SPY", expired=False, contract_type="call", limit=50)
    print(f"SPY current call contracts: {len(chain)} "
          f"(sample: {chain[0].ticker if chain else 'none'})")
    if chain:
        c = min(chain, key=lambda x: abs(x.strike - 745))  # ~ATM, liquid
        try:
            bars = await p.get_aggregates(c.ticker, "1d", date(2026, 4, 1), c.expiration)
            print(f"{c.ticker} (strike {c.strike}): {len(bars)} daily bars"
                  + (f", earliest {bars[0]['time']} latest {bars[-1]['time']}" if bars else ""))
        except PolygonNotAuthorized as e:
            print(f"{c.ticker}: NOT AUTHORIZED for that range — {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
