"""Manual live validation against a running IB Gateway (paper). NOT a unit test.

Usage (with IB Gateway running + logged into the paper account, API enabled,
listening on the configured port — 4002 for paper Gateway):

    ../.venv/bin/python -m scripts.connect_smoke
    ENABLE_LIVE_ORDERS=1 ../.venv/bin/python -m scripts.connect_smoke --bracket NVDA

Without --bracket it only reads (accounts, a quote, positions). With --bracket and
ENABLE_LIVE_ORDERS=1 it places a 1-share paper bracket, waits for you, then flattens.
"""
from __future__ import annotations

import argparse
import asyncio

from app.engine.execution import flatten_all, submit_bracket_order
from app.ibkr.account import get_accounts, get_positions
from app.ibkr.gateway import get_gateway
from app.ibkr.market import get_quotes


async def main(bracket_symbol: str | None) -> int:
    await get_gateway().connect()
    print("Connected:", get_gateway().is_connected())
    print("accounts:", await get_accounts())
    print("NVDA quote:", (await get_quotes(["NVDA"]))["NVDA"])
    print("positions:", await get_positions())

    if bracket_symbol:
        sym = bracket_symbol.upper()
        price = (await get_quotes([sym]))[sym]["price"]
        if not price:
            print(f"No price for {sym}; skipping bracket.")
        else:
            res = await submit_bracket_order(sym, "long", 1, price, price * 0.98,
                                             price * 1.03, reason="smoke")
            print("bracket:", res)
            input("Press Enter to flatten...")
            print("flatten:", await flatten_all())

    get_gateway().disconnect()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="IBKR Gateway connection smoke test.")
    ap.add_argument("--bracket", default=None, help="symbol for a 1-share paper bracket test")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.bracket)))
