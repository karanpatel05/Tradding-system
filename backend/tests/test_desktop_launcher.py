import os
import sys
from pathlib import Path

# Make the repo root importable so `import desktop.app` works from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from desktop import app as launcher


def test_resolve_data_dir_creates_app_support(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = launcher.resolve_data_dir()
    assert d.exists() and d.is_dir()
    assert d == tmp_path / "Library" / "Application Support" / "TradingSystem"


def test_set_runtime_env_points_db_and_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HISTORY_CACHE_DIR", raising=False)
    monkeypatch.delenv("FRONTEND_DIST", raising=False)
    launcher.set_runtime_env(tmp_path, "/some/dist")
    assert os.environ["DATABASE_URL"] == f"sqlite+aiosqlite:///{tmp_path / 'trading.db'}"
    assert os.environ["HISTORY_CACHE_DIR"] == str(tmp_path / "data_cache")
    assert os.environ["FRONTEND_DIST"] == "/some/dist"


def test_wait_for_server_false_on_dead_port():
    assert launcher.wait_for_server("http://127.0.0.1:8766/health", timeout=1.0) is False
