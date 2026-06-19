from app.config import Settings


def test_polygon_base_url_default():
    assert Settings().polygon_base_url == "https://api.polygon.io"


def test_polygon_key_from_POLYGON_API_KEY(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "pk_test")
    assert Settings().polygon_api_key == "pk_test"


def test_polygon_key_falls_back_to_MASSIVE_API_KEY(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.setenv("MASSIVE_API_KEY", "legacy_key")
    assert Settings().polygon_api_key == "legacy_key"
