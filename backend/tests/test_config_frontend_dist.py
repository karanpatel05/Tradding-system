from app.config import Settings


def test_frontend_dist_default_empty():
    assert Settings().frontend_dist == ""


def test_frontend_dist_from_env(monkeypatch):
    monkeypatch.setenv("FRONTEND_DIST", "/tmp/dist")
    assert Settings().frontend_dist == "/tmp/dist"
