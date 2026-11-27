"""Offline parity checks for incremental expiry-valued SPY Research sessions."""
from datetime import datetime, time

import httpx
import pytest

from scripts.spy_research_days import ET, price_session


SESSION = {"date": "2026-11-27", "open": "09:30", "close": "13:00"}
CALL, PUT = "SPY261127C00500000", "SPY261127P00500000"
CALL_WING, PUT_WING = "SPY261127C00510000", "SPY261127P00490000"


def bar(hhmm, vwap):
    timestamp = datetime.combine(datetime.fromisoformat(SESSION["date"]).date(), time.fromisoformat(hhmm), ET)
    return {"t": timestamp.isoformat(), "vw": vwap, "o": vwap, "h": vwap, "c": vwap}


def query(*, stock_close=True, missing_entry=False, partial_options=False):
    def respond(request):
        assert request.method == "GET" and request.url.host == "data.alpaca.markets"
        params = request.url.params
        assert params["end"].endswith("13:01:00-05:00")
        if request.url.path == "/v2/stocks/bars":
            stock = [bar("09:45", 499.5)]
            if stock_close:
                stock.append(bar("12:59", 505))
            if params["timeframe"] == "1Day":
                stock = [bar("00:00", 505)]
            return httpx.Response(200, json={"bars": {"SPY": stock}})
        assert request.url.path == "/v1beta1/options/bars"
        if params["start"].endswith("12:55:00-05:00"):
            return httpx.Response(200, json={"bars": {}})
        assert set(params["symbols"].split(",")) == {CALL, PUT, CALL_WING, PUT_WING}
        if "page_token" not in params:
            body = [bar("10:45" if missing_entry else "09:45", 1.0),
                    dict(bar("11:00", 1.6), h=1.8, o=1.7)]
            return httpx.Response(200, json={"bars": {CALL: body}, "next_page_token": "more"})
        assert params["page_token"] == "more"
        puts = [bar("09:45", 1.5)]
        if not partial_options:
            puts.append(bar("12:59", 0.01))
        return httpx.Response(200, json={"bars": {PUT: puts, CALL_WING: [bar("09:47", .20)],
                                                   PUT_WING: [bar("09:45", .10)]}})
    return httpx.Client(base_url="https://data.alpaca.markets", transport=httpx.MockTransport(respond))


def test_half_day_stop_on_gap_through_and_expiry_fees():
    with query() as client:
        assert price_session(client, SESSION) == ("47.325000", None)


def test_entry_unavailable_after_a_complete_options_day():
    with query(missing_entry=True) as client:
        assert price_session(client, SESSION) == (None, "no_option_entry")


@pytest.mark.parametrize("kwargs", [{"stock_close": False}, {"partial_options": True}])
def test_incomplete_closing_history_is_retryable(kwargs):
    with query(**kwargs) as client, pytest.raises(ValueError, match="Incomplete SPY"):
        price_session(client, SESSION)
