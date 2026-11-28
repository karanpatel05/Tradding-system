from datetime import datetime, timedelta

import httpx
import pytest

from scripts.qqq_backtest_days import ET, FIELDS, price_bars, price_session


DAY = {"date": "2026-11-25", "open": "09:30", "close": "16:00"}
EARLY = {"date": "2026-11-27", "open": "09:30", "close": "13:00"}


def bar(session, clock, price):
    return {"t": f"{session['date']}T{clock}:00-05:00",
            "o": price, "h": price, "l": price, "c": price, "v": 3}


def stocks(session=DAY, first=499.05, last=500.45):
    start = datetime.fromisoformat(session["date"] + "T09:30").replace(tzinfo=ET)
    bars = [bar(session, (start + timedelta(minutes=i)).strftime("%H:%M"), first) for i in range(60)]
    bars[-1] = bar(session, "10:29", last)
    return bars


def options(session=DAY, *, call=False):
    # Closes completed at 10:30 and after the scheduled exit must be ignored.
    right, strikes = ("C", (500, 501)) if call else ("P", (500, 499))
    prefix = "QQQ" + session["date"].replace("-", "")[2:] + right
    exit_hour = "12" if session == EARLY else "15"
    return {
        prefix + f"{strikes[0] * 1000:08d}": [
            bar(session, "10:29", 8), bar(session, "10:30", 1.43),
            bar(session, f"{exit_hour}:49", 2.25), bar(session, f"{exit_hour}:50", 2.90)],
        prefix + f"{strikes[1] * 1000:08d}": [
            bar(session, "10:29", 8), bar(session, "10:30", 1.01),
            bar(session, f"{exit_hour}:49", 2.0), bar(session, f"{exit_hour}:50", 2.0)],
    }


@pytest.mark.parametrize("session", [DAY, EARLY])
def test_entry_after_signal_and_scheduled_exit_with_two_sided_cost(session):
    row = price_bars(session, stocks(session), options(session))
    assert tuple(row) == FIELDS
    assert row == {"date": session["date"], "status": "traded", "side": "put",
                   "short_strike": "500", "long_strike": "499", "entry_credit": "0.41",
                   "exit_debit": "0.26", "exit_method": "bar", "net_pnl_usd": "15.00", "reason": ""}


def test_rule_skips_and_missing_first_hour_are_distinct():
    assert price_bars(DAY, stocks(first=500.45), {})["status"] == "skipped"
    assert price_bars(DAY, stocks(last=500), {})["reason"] == "short_strike_itm_at_entry"
    row = price_bars(DAY, stocks()[1:], {})
    assert (row["status"], row["net_pnl_usd"]) == ("missing", "")


def test_requires_synchronous_entry_and_applies_minimum_after_spread():
    legs = options()
    short, long = legs.values()
    long[1] = bar(DAY, "10:31", 1.01)
    assert price_bars(DAY, stocks(), legs)["status"] == "missing"
    short.append(bar(DAY, "10:31", 1.03))
    row = price_bars(DAY, stocks(), legs)
    assert (row["status"], row["reason"]) == ("skipped", "below_min_credit")
    short[-1] = bar(DAY, "10:31", 1.04)
    assert price_bars(DAY, stocks(), legs)["entry_credit"] == "0.02"
    short[-1] = bar(DAY, "10:31", 2.01)
    assert price_bars(DAY, stocks(), legs)["reason"] == "incoherent_entry_mark"


def test_intrinsic_exit_or_unresolved_after_a_known_entry():
    legs = options(call=True)
    equity = stocks(first=500.45, last=499.42)
    _, long = legs.values()
    long[2] = bar(DAY, "15:48", 2)  # unpaired exit minutes
    row = price_bars(DAY, equity, legs)
    assert (row["status"], row["entry_credit"], row["net_pnl_usd"]) == ("unresolved", "0.41", "")
    equity.extend([bar(DAY, "15:34", 500.55), bar(DAY, "15:50", 504)])
    row = price_bars(DAY, equity, legs)
    assert (row["side"], row["exit_method"], row["exit_debit"], row["net_pnl_usd"]) == (
        "call", "intrinsic", "0.56", "-15.00")
    equity[60] = bar(DAY, "15:34", 504)
    assert price_bars(DAY, equity, legs)["exit_debit"] == "1.01"
    equity[60] = bar(DAY, "15:33", 504)
    assert price_bars(DAY, equity, legs)["status"] == "unresolved"


def test_historical_gets_honor_pagination_and_raise_provider_errors():
    equity = stocks()
    legs = options()
    requests = []

    def handle(request):
        params = request.url.params
        requests.append(request)
        assert request.method == "GET"
        if request.url.path == "/v2/stocks/bars":
            assert params["feed"] == "sip" and params["adjustment"] == "raw"
            return httpx.Response(200, json={"bars": {"QQQ": equity}})
        assert request.url.path == "/v1beta1/options/bars"
        assert set(params["symbols"].split(",")) == legs.keys()
        first, second = legs
        if params.get("page_token") == "more":
            return httpx.Response(200, json={"bars": {second: legs[second]}})
        return httpx.Response(200, json={"bars": {first: legs[first]}, "next_page_token": "more"})

    with httpx.Client(base_url="https://data.alpaca.markets", transport=httpx.MockTransport(handle)) as client:
        assert price_session(client, DAY) == price_bars(DAY, equity, legs)
        assert len(requests) == 3
    with httpx.Client(base_url="https://data.alpaca.markets", transport=httpx.MockTransport(
            lambda request: httpx.Response(429))) as client, pytest.raises(httpx.HTTPStatusError):
        price_session(client, DAY)
