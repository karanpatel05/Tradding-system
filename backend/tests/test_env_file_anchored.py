"""Settings must read backend/.env by absolute path: the LaunchAgent runs uvicorn from
the repo root, so a CWD-relative env_file silently reads the wrong file (saved Tastytrade
creds vanished on every restart until this was anchored)."""
from pathlib import Path

from app.config import Settings, _BASE_DIR


def test_env_file_is_absolute_and_in_backend():
    env_file = Settings.model_config.get("env_file")
    assert env_file, "env_file must be configured"
    p = Path(env_file)
    assert p.is_absolute()
    assert p == _BASE_DIR / ".env"
    assert p.parent.name == "backend"
