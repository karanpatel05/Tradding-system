"""Options data provider factory (Polygon)."""
from __future__ import annotations

from app.config import get_settings
from app.options.base import OptionsDataProvider, PolygonNotConfigured
from app.options.polygon import PolygonOptionsProvider

__all__ = ["get_options_provider", "OptionsDataProvider", "PolygonNotConfigured"]


def get_options_provider() -> OptionsDataProvider:
    s = get_settings()
    if not s.polygon_api_key:
        raise PolygonNotConfigured("POLYGON_API_KEY (or MASSIVE_API_KEY) is not set.")
    return PolygonOptionsProvider(s.polygon_api_key, s.polygon_base_url)
