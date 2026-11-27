"""Offline SPY replay coverage, instrument identity and isolated CLI behavior."""
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from app.backtest.spy_iron_fly import (ET, PrintSeries, ReplayClock, SpyPrintFeed, aggregate,
                                     body_at, option_symbols, session_row)
from app.services import backtest_reports
from app.strategies.xsp_iron_fly.params import IronFlyParams

DAY = date(2026, 11, 17)
ENTRY = datetime.combine(DAY, time(9, 45), ET)
CLOSE_START = datetime.combine(DAY, time(15, 30), ET)
KEY = "spy_iron_fly"
BACKEND = Path(__file__).resolve().parents[1]


def us(at):
    return round(at.timestamp() * 1_000_000)


def fixture_day():
    # A dated test fixture with prints available at entry and the 15:30 exit.
    contracts = option_symbols(DAY, {750}, 10)
    for leg in contracts.values():
        is_body = leg["strike"] == 750
        leg["prints"] = [
            [us(at + timedelta(seconds=second)), price]
            for at, price in [(ENTRY, 2.0 if is_body else 0.5), (CLOSE_START, 1.5 if is_body else 0.25)]
            for second in range(50)
        ]
    return {"date": str(DAY), "ticker": "SPY", "source": backtest_reports.SPY_SOURCE,
            "spot": [[us(ENTRY), 749.5]], "contracts": contracts}


def test_as_of_print_window_and_contract_identity():
    series = PrintSeries([[us(ENTRY), 1.0], [us(ENTRY + timedelta(seconds=7)), 2.0]], DAY)
    assert series.latest(ENTRY - timedelta(microseconds=1), 5) is None
    assert series.latest(ENTRY, 5) == (1.0, ENTRY)
    assert series.latest(ENTRY + timedelta(seconds=6), 5) is None
    assert series.latest(ENTRY + timedelta(seconds=7), 5)[0] == 2.0
    assert body_at(749.5) == 750
    record = fixture_day()
    feed = SpyPrintFeed(record, IronFlyParams.for_spy(), ReplayClock(ENTRY))
    assert feed.spot("SPY").price == 749.5 and feed.spot("XSP") is None
    quote = feed.quote("SPY261117C00750000")
    assert (quote.bid, quote.ask) == pytest.approx((1.995, 2.005))
    record["contracts"]["XSP261117C00750000"] = record["contracts"].pop("SPY261117C00750000")
    with pytest.raises(ValueError, match="identity"):
        SpyPrintFeed(record, IronFlyParams.for_spy(), ReplayClock(ENTRY))


def test_unresolved_sessions_do_not_inflate_win_rate_or_report_zero():
    rows = [{"date": str(DAY), "status": "unresolved", "pnl": None, "fees": 4.0}]
    sums = aggregate(rows)
    assert sums["unresolved"] == 1 and sums["traded"] == 0
    assert all(sums[k] is None for k in ("win_rate", "max_drawdown", "total_pnl", "fees", "best_day"))
    rows.append({"date": "2026-11-18", "status": "closed", "pnl": 100.0, "fees": 8.0})
    assert aggregate(rows)["total_pnl"] == 100.0 and aggregate(rows)["fees"] == 8.0


def test_unfilled_combo_with_planned_legs_is_an_untraded_session():
    session = {"session_date": DAY, "status": "skipped", "reason": "entry not filled",
               "legs": [{"qty": 0} for _ in range(4)], "fills": [], "realized_pnl": 0.0}
    row = session_row(session)
    assert row["status"] == "skipped" and row["contracts"] == 0 and not row["data_gap"]
    assert aggregate([row])["rule_skips"] == {str(DAY): "entry not filled"}
    session["legs"][0]["qty"] = 1
    assert session_row(session)["status"] == "unresolved"


def test_fetch_uses_completed_spot_bar_and_eligible_single_leg_trades():
    from scripts.run_spy_fly_backtest import fetch_day, stamp

    def history(request):
        path = request.url.path
        if path == "/v2/stocks/bars":
            assert request.url.params["start"] == request.url.params["end"] == "2026-11-17T09:40:00-05:00"
            return httpx.Response(200, json={"bars": {"SPY": [{"t": "2026-11-17T14:40:00Z", "c": 749.5}]}})
        if path == "/v2/stocks/trades":
            return httpx.Response(200, json={"trades": {"SPY": [
                {"t": "2026-11-17T14:45:05Z", "p": 751, "c": [" "]},
                {"t": "2026-11-17T14:45:06Z", "p": 900, "c": ["Z"]}]}})
        assert path == "/v1beta1/options/trades"
        symbols = set(request.url.params["symbols"].split(","))
        assert symbols == set(option_symbols(DAY, {750, 751}, 10))
        return httpx.Response(200, json={"trades": {"SPY261117C00750000": [
            {"t": "2026-11-17T14:44:59.000000123Z", "p": 2, "c": "I"},
            {"t": "2026-11-17T14:45:00Z", "p": 10, "c": "f"},
            {"t": "2026-11-17T14:45:01Z", "p": 20, "c": "A"}]}})

    with httpx.Client(base_url="https://test.invalid", transport=httpx.MockTransport(history)) as client:
        record = fetch_day(client, DAY, CLOSE_START + timedelta(minutes=30), IronFlyParams.for_spy())
    ticks = record["contracts"]["SPY261117C00750000"]["prints"]
    assert ticks == [[stamp("2026-11-17T14:44:59Z") + 1, 2]]
    feed = SpyPrintFeed(record, IronFlyParams.for_spy(), ReplayClock(ENTRY))
    assert feed.spot("SPY").price == 749.5


def test_offline_replay_and_saved_report_isolation(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "history_cache_dir", str(tmp_path))
    report_dir = tmp_path / "spy_bt_alpaca"
    day_dir = report_dir / "days"
    day_dir.mkdir(parents=True)
    (day_dir / f"{DAY}.json").write_text(json.dumps(fixture_day()))
    configured_db = tmp_path / "app.db"
    env = dict(os.environ, HISTORY_CACHE_DIR=str(tmp_path), DATABASE_URL=f"sqlite+aiosqlite:///{configured_db}")
    command = [sys.executable, "-m", "scripts.run_spy_fly_backtest", "run", "--start", str(DAY), "--end", str(DAY)]
    subprocess.run(command, env=env, cwd=BACKEND, check=True, capture_output=True, text=True, timeout=30)

    assert not configured_db.exists()
    assert backtest_reports.list_reports(KEY)[0]["start"] == str(DAY)
    report = backtest_reports.get_report(KEY, "alpaca-prints")
    assert report["params"]["ticker"] == "SPY" and report["params"]["flatten_minutes_before_close"] == 30
    assert set(report["by_width"]) == {"0.01"}
    scenario = report["by_width"]["0.01"]
    row = scenario["sessions"][0]
    assert row["status"] == "closed" and row["body"] == 750
    assert row["contracts"] == 1 and row["fees"] == 8.0
    # $50 at print prices, less $4 to cross all four spreads round trip and $8 in fees.
    assert row["pnl"] == pytest.approx(38)
    assert scenario["aggregate"]["total_pnl"] == row["pnl"]
    assert not backtest_reports.has_reports("xsp_iron_fly")
    report["params"]["ticker"] = "XSP"
    (report_dir / "results_5m.json").write_text(json.dumps(report))
    assert not backtest_reports.has_reports(KEY)
    assert backtest_reports.get_report(KEY, "alpaca-prints") is None
