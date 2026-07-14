"""facade.data_status: summarizes which source served recently (dashboard Data chip)."""
import time

from app.marketdata import facade


def test_recency_window_and_priority(monkeypatch):
    facade._last_source.clear()
    facade._last_source_ts.clear()
    now = time.monotonic()
    facade._last_source.update({"stock_quote:QQQ": "tastytrade", "vix": "ibkr",
                                "option_mid:OLD": "tastytrade"})
    facade._last_source_ts.update({"stock_quote:QQQ": now, "vix": now,
                                   "option_mid:OLD": now - 10_000})   # stale, outside window
    out = facade.data_status(window_seconds=900)
    assert out["serving"] == "tastytrade"
    assert out["recent_sources"] == {"tastytrade": 1, "ibkr": 1}
    # Only IBKR recently -> fallback state
    facade._last_source_ts["stock_quote:QQQ"] = now - 10_000
    assert facade.data_status()["serving"] == "ibkr"
    # Nothing recent -> idle
    facade._last_source_ts["vix"] = now - 10_000
    assert facade.data_status()["serving"] == "idle"
    facade._last_source.clear(); facade._last_source_ts.clear()
