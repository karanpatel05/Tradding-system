import pytest

from app.options import get_options_provider
from app.options.base import PolygonNotConfigured


def test_factory_raises_without_key(monkeypatch):
    # Force an empty key (POLYGON wins over MASSIVE in AliasChoices); empty string is
    # falsy so the factory must raise. (delenv wouldn't help: backend/.env still has a key.)
    from app import config
    monkeypatch.setenv("POLYGON_API_KEY", "")
    config.get_settings.cache_clear()
    with pytest.raises(PolygonNotConfigured):
        get_options_provider()
    config.get_settings.cache_clear()


def test_factory_builds_with_key(monkeypatch):
    from app import config
    monkeypatch.setenv("POLYGON_API_KEY", "pk_test")
    config.get_settings.cache_clear()
    p = get_options_provider()
    assert p.name == "polygon-options"
    config.get_settings.cache_clear()
