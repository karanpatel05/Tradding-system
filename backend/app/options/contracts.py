"""Polygon OCC option-ticker helpers (pure).

Format: ``O:<UNDERLYING><YYMMDD><C|P><strike*1000, 8 digits>``
e.g. SPY 2026-06-22 $747 call -> ``O:SPY260622C00747000``."""
from __future__ import annotations

from datetime import date


def occ_ticker(underlying: str, expiry: date, right: str, strike: float) -> str:
    strike_int = int(round(strike * 1000))
    return f"O:{underlying.upper()}{expiry:%y%m%d}{right.upper()}{strike_int:08d}"


def parse_occ(ticker: str) -> dict:
    body = ticker[2:] if ticker.startswith("O:") else ticker
    tail = body[-15:]          # 6 date + 1 right + 8 strike
    underlying = body[:-15]
    expiry = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
    right = tail[6]
    strike = int(tail[7:]) / 1000.0
    return {"underlying": underlying, "expiry": expiry, "right": right, "strike": strike}
