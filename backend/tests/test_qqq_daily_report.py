from datetime import date

from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import get_settings
from app.main import app
from app.services import qqq_backtest_report as report
from scripts import refresh_qqq_backtest as job
from scripts.daily_backtest import publish_rows


def row(day, status="traded", **kwargs):
    result = dict.fromkeys(report.DAILY_FIELDS, "")
    if status in ("traded", "unresolved"):
        result.update(side="put", short_strike="400", long_strike="399", entry_credit="0.50")
    if status == "traded":
        result.update(exit_method="bar", exit_debit="0.60", net_pnl_usd="-10.00")
    else:
        result["reason"] = status
    return dict(result, date=day, status=status) | kwargs


def test_daily_api_summary_coverage_and_download(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "history_cache_dir", str(tmp_path))
    api = TestClient(app)
    reports = f"/api/strategies/{report.KEY}/backtest/reports"
    assert api.get(reports).json() == {"reports": [], "can_run": False}
    rows = [row("2024-02-01"), row("2024-02-02", exit_debit="0.12", net_pnl_usd="38.00", exit_method="intrinsic"),
            row("2024-02-05", "unresolved"), row("2024-02-06", "skipped"), row("2024-02-07", "missing")]
    path = report.daily_csv_path()
    path.parent.mkdir()
    publish_rows(path, {r["date"]: r for r in rows}, report)
    assert api.get(reports).json()["reports"][0]["id"] == report.REPORT_ID
    detail = api.get(f"{reports}/{report.REPORT_ID}").json()
    assert detail["summary"] == {"trades": 2, "wins": 1, "win_rate_pct": 50,
                                 "total_pnl": 28, "max_drawdown": -10}
    assert [r["pnl"] for r in detail["sessions"]] == [-10, 38]
    assert detail["intrinsic_exits"] == 1
    for field, day in [("unresolved_dates", "05"), ("skipped_dates", "06"), ("missing_dates", "07")]:
        assert detail[field] == [f"2024-02-{day}"]
    download = detail["downloads"][0]["url"]
    assert api.get(download).content == path.read_bytes()
    assert api.get(download.replace(report.KEY, "spy_iron_fly")).status_code == 404
    assert api.get(download.replace(report.FILENAME, "private.csv")).status_code == 404
    original = path.read_bytes()
    with pytest.raises(ValueError):
        publish_rows(path, {"one": row("2024-02-01", net_pnl_usd="-999.00")}, report)
    assert path.read_bytes() == original
    path.write_text("corrupt CSV")
    assert api.get(download).status_code == 404


def test_catchup_retries_and_preserves_entered_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "history_cache_dir", str(tmp_path))
    friday = {"date": "2026-11-27", "open": "09:30", "close": "13:00"}
    monday = {"date": "2026-11-30", "open": "09:30", "close": "16:00"}

    def calendar(req):
        assert req.method == "GET" and req.url.path == "/v2/calendar"
        end = req.url.params["end"]
        return httpx.Response(200, json=[s for s in (friday, monday) if s["date"] <= end])

    prices = iter(["unresolved", "missing", "traded", "offline", "skipped"])
    calls = []

    def price(_client, s):
        calls.append(s)
        status = next(prices)
        if status == "offline":
            raise ConnectionError("offline")
        return row(s["date"], status)

    monkeypatch.setattr(job, "price_session", price)
    with httpx.Client(base_url="https://test", transport=httpx.MockTransport(calendar)) as client:
        for day in (26, 27, 28):
            job.refresh(date(2026, 11, day), client, None)
        assert report.read()["unresolved_dates"] == [friday["date"]]
        job.refresh(date(2026, 11, 29), client, None)
        original = report.daily_csv_path().read_bytes()
        with pytest.raises(ConnectionError):
            job.refresh(date(2026, 11, 30), client, None)
        assert report.daily_csv_path().read_bytes() == original
        job.refresh(date(2026, 11, 30), client, None)
        job.refresh(date(2026, 11, 30), client, None)
        assert calls == [friday, friday, friday, monday, monday]
        assert report.read()["skipped_dates"] == [monday["date"]]
