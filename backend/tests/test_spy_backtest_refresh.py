import csv
from datetime import date, datetime, timezone

import httpx
import pytest

from app.config import get_settings
from app.services import spy_research_report as report
from scripts import refresh_spy_backtest as job


def test_cutoff_uses_previous_new_york_date():
    assert job.previous_date(datetime(2026, 11, 28, 2, tzinfo=timezone.utc)) == date(2026, 11, 26)
    assert job.previous_date(datetime(2026, 11, 28, 6, tzinfo=timezone.utc)) == date(2026, 11, 27)


def test_seed_holiday_catchup_and_retry_without_replacing_history(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "history_cache_dir", str(tmp_path))
    legacy = tmp_path / "spy_iron_fly_research" / "derived" / report.PERIOD_CSV
    legacy.parent.mkdir(parents=True)
    with legacy.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(report.DAILY_FIELDS)
        w.writerow(("2026-11-25", "1", *report.DAILY_SETTINGS, "1032.520000"))
    original = legacy.read_bytes()
    friday = {"date": "2026-11-27", "open": "09:30", "close": "13:00"}
    monday = {"date": "2026-11-30", "open": "09:30", "close": "16:00"}
    def calendar(req):
        assert req.method == "GET" and req.url.path == "/v2/calendar"
        q = req.url.params
        return httpx.Response(200, json=[s for s in (friday, monday) if q["start"] <= s["date"] <= q["end"]])
    priced = []
    def price(_, session):
        priced.append(session)
        if session == friday and priced.count(friday) == 1:
            return None, "no_option_entry"
        if session == monday:
            raise RuntimeError("provider unavailable")
        return "-293.250000", None
    monkeypatch.setattr(job, "price_session", price)
    with httpx.Client(base_url="https://calendar.test", transport=httpx.MockTransport(calendar)) as client:
        job.refresh(date(2026, 11, 26), client, None)
        active = report.daily_csv_path()
        assert active.read_bytes() == original and not priced
        assert report.period_csv() == active
        job.refresh(date(2026, 11, 27), client, None)
        export = report.read_period()
        assert export["end"] == friday["date"] and export["summary"]["trades"] == 1
        assert friday["date"] in export["missing_dates"]
        job.refresh(date(2026, 11, 29), client, None)
        assert report.read_period()["summary"]["total_pnl"] == 739.27
        assert friday["date"] not in report.read_period()["missing_dates"]
        after_friday = active.read_bytes()
        job.refresh(date(2026, 11, 29), client, None)
        with pytest.raises(RuntimeError, match="provider unavailable"):
            job.refresh(date(2026, 11, 30), client, None)
        assert priced == [friday, friday, monday]
        assert active.read_bytes() == after_friday
        assert legacy.read_bytes() == original


def test_clean_system_works_without_imported_files(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "history_cache_dir", str(tmp_path))
    session = {"date": "2024-02-01", "open": "09:30", "close": "16:00"}
    monkeypatch.setattr(job, "price_session", lambda *_: (None, "no_option_entry"))
    with httpx.Client(base_url="https://calendar.test",
                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[session]))) as client:
        job.refresh(date(2024, 2, 1), client, None)
    saved = report.read_period()
    assert saved["end"] == session["date"] and saved["missing_dates"] == [session["date"]]
    assert saved["summary"]["trades"] == 0 and saved["summary"]["win_rate_pct"] is None
