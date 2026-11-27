from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


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
