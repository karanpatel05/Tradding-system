from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import spy_research_report as research


SUMMARY = """SPY 0DTE ATM IRON FLY (short straddle + long wings)
Days traded: 2   Period: 2025-01-02 -> 2025-01-03
=== Wing width $10  (max risk per fly <= $1000) ===
    mode half-spread |   total P&L |  avg/day |  median |  win% |  std/day | Sharpe* |     maxDD |    best |   worst
 stop150        0.01 |       1,000 |    500.0 |   500.0 |  50.0 |    600.0 |   13.23 |      -100 |    1100 |    -100
Wing entry source (all widths x both wings):
=== same days, hs=0.02: iron fly vs. short straddle ===
           fly $10 (stop 1.5x) |    2 |         992 |    496.0 |  50.0 |    13.12 |      -104 |    -104
"""


def test_copied_research_is_scoped_and_preserves_source_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "history_cache_dir", str(tmp_path))
    root = tmp_path / "spy_iron_fly_research" / "results"
    root.mkdir(parents=True)
    summary = root / "summary.txt"
    summary.write_text(SUMMARY)
    daily = root / "daily_pnl_W10.csv"
    daily.write_bytes(b"date,total_pnl_stop150\r\n2025-01-02,1096\r\n2025-01-03,-104\r\n")
    (root / "raw_days.json").write_text("[]")
    client = TestClient(app)  # no app lifespan or trading workers
    base = "/api/strategies/spy_iron_fly/backtest/reports"
    url = f"{base}/research-iron-fly"
    meta, = client.get(base).json()["reports"]
    assert meta["kind"] == "research" and meta["id"] == "research-iron-fly"
    assert (meta["start"], meta["end"]) == ("2025-01-02", "2025-01-03")
    detail = client.get(url).json()
    scenario, = detail["scenarios"]
    assert detail["days_traded"] == 2
    assert (scenario["mode"], scenario["wing_width"], scenario["half_spread"]) == ("stop150", 10, 0.01)
    assert (scenario["total_pnl"], scenario["win_rate_pct"], scenario["max_drawdown"]) == (1000, 50, -100)
    downloads = {d["filename"]: d["url"] for d in detail["downloads"]}
    assert set(downloads) == {"summary.txt", daily.name}
    assert client.get(downloads[daily.name]).content == daily.read_bytes()
    assert client.get(downloads["summary.txt"]).content == summary.read_bytes()
    assert client.get(f"{url}/files/raw_days.json").status_code == 404
    assert client.get(f"{url}/files/.env").status_code == 404
    assert client.get(url.replace("spy_iron_fly", "xsp_iron_fly")).status_code == 404
    assert client.get(downloads[daily.name].replace("spy_iron_fly", "xsp_iron_fly")).status_code == 404
    summary.write_text("incomplete export")
    assert client.get(url).status_code == 404
    assert client.get(base).json()["reports"] == []


def test_period_report_uses_saved_daily_rows_and_offers_exact_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "history_cache_dir", str(tmp_path))
    root = tmp_path / "spy_iron_fly_research"
    (root / "results").mkdir(parents=True)
    (root / "results/summary.txt").write_text(SUMMARY)
    (root / "derived").mkdir()
    path = root / "derived" / research.PERIOD_CSV
    path.write_text("date,contracts,wing_width,stop_multiple,full_bid_ask_spread,half_spread_per_fill,"
                    "fee_per_contract,net_pnl_usd\n"
                    "2024-02-01,1,10,1.5,0.01,0.005,0.035,-4.123\n"
                    "2024-02-05,1,10,1.5,0.01,0.005,0.035,5.121\n")
    client = TestClient(app)
    base = "/api/strategies/spy_iron_fly/backtest/reports"
    assert [s["id"] for s in client.get(base).json()["reports"]] == [research.PERIOD_ID, research.REPORT_ID]
    url = f"{base}/{research.PERIOD_ID}"
    detail = client.get(url).json()
    assert detail["missing_dates"] == ["2024-02-02"]
    assert detail["sessions"][0]["pnl"] == -4.123
    assert detail["summary"] == {"trades": 2, "wins": 1, "win_rate_pct": 50.0, "total_pnl": 1.0,
                                 "max_drawdown": -4.12}
    download, = detail["downloads"]
    assert client.get(download["url"]).content == path.read_bytes()
    assert client.get(f"{url}/files/summary.txt").status_code == 404
    path.write_text(path.read_text().replace("0.005", "0.01"))
    assert client.get(url).status_code == 404
    assert client.get(download["url"]).status_code == 404
    assert [s["id"] for s in client.get(base).json()["reports"]] == [research.REPORT_ID]
