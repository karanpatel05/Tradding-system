"""Offline SPY replay coverage, instrument identity and isolated CLI behavior."""
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.backtest.spy_iron_fly import (ET, PrintSeries, ReplayClock, SpyPrintFeed, aggregate,
                                     body_at, option_symbols)
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
    feed = SpyPrintFeed(record, IronFlyParams.for_spy(), ReplayClock(ENTRY), 0.05)
    assert feed.spot("SPY").price == 749.5 and feed.spot("XSP") is None
    record["contracts"]["XSP261117C00750000"] = record["contracts"].pop("SPY261117C00750000")
    with pytest.raises(ValueError, match="identity"):
        SpyPrintFeed(record, IronFlyParams.for_spy(), ReplayClock(ENTRY), 0.05)


def test_unresolved_sessions_do_not_inflate_win_rate_or_report_zero():
    rows = [{"date": str(DAY), "status": "unresolved", "pnl": None, "fees": 4.0}]
    sums = aggregate(rows)
    assert sums["unresolved"] == 1 and sums["traded"] == 0
    assert all(sums[k] is None for k in ("win_rate", "max_drawdown", "total_pnl", "fees", "best_day"))
    rows.append({"date": "2026-11-18", "status": "closed", "pnl": 100.0, "fees": 8.0})
    assert aggregate(rows)["total_pnl"] == 100.0 and aggregate(rows)["fees"] == 8.0


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
    for width, scenario in report["by_width"].items():
        row = scenario["sessions"][0]
        assert row["status"] == "closed" and row["body"] == 750
        assert row["contracts"] == 1 and row["fees"] == 8.0
        # Two short legs each gain $0.50; the $0.25 wing losses, modeled bid/ask
        # on both sides of each leg, and eight $1 fees reduce that realized gain.
        assert row["pnl"] == pytest.approx(50 - 4 * float(width) * 100 - 8)
        assert scenario["aggregate"]["total_pnl"] == row["pnl"]
    assert not backtest_reports.has_reports("xsp_iron_fly")
    report["params"]["ticker"] = "XSP"
    (report_dir / "results_5m.json").write_text(json.dumps(report))
    assert not backtest_reports.has_reports(KEY)
    assert backtest_reports.get_report(KEY, "alpaca-prints") is None
