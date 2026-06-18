"""US-listed equity universe.

For now this is a curated liquid seed list so the scan/rank scaffolding is
exercisable. TODO: replace with the full US-listed universe — e.g. pull active
equities from the Tastytrade instruments endpoint
(`/instruments/equities/active`) and cache daily.
"""

from __future__ import annotations

# Liquid large/mega-cap seed universe (placeholder).
SEED_UNIVERSE: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "JPM", "V", "MA", "UNH", "HD", "PG", "COST", "XOM", "JNJ", "WMT",
    "BAC", "KO", "PEP", "CRM", "ADBE", "ORCL", "INTC", "QCOM", "CSCO", "TXN",
    "DIS", "NKE", "MCD", "BA", "CAT", "GE", "F", "GM", "T", "VZ",
]


async def get_universe() -> list[str]:
    """Return the tradable US-listed universe. Async so the real implementation
    can fetch + cache from Tastytrade without changing callers."""
    return list(SEED_UNIVERSE)
