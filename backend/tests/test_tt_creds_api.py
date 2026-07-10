"""PUT /auth/tastytrade persists creds to the .env file, updates live settings, and
resets the cached TT session; GET reports configured-ness without echoing secrets."""
import pytest

from app.auth import routes as auth_routes
from app.config import get_settings


@pytest.mark.asyncio
async def test_put_persists_and_get_masks(tmp_path, monkeypatch):
    envfile = tmp_path / ".env"
    monkeypatch.setattr(auth_routes, "_ENV_PATH", envfile)
    reset_calls = []
    monkeypatch.setattr(auth_routes, "reset_tt_session", lambda: reset_calls.append(1))
    monkeypatch.setattr(get_settings(), "tt_provider_secret", "")
    monkeypatch.setattr(get_settings(), "tt_refresh_token", "")

    out = await auth_routes.tt_save(auth_routes.TTCredsBody(
        provider_secret="sek", refresh_token="reftok"))
    assert out["configured"] is True and out["has_secret"] and out["has_refresh"]
    assert "sek" not in str(out) and "reftok" not in str(out)
    text = envfile.read_text()
    assert "TT_PROVIDER_SECRET=sek" in text and "TT_REFRESH_TOKEN=reftok" in text
    assert get_settings().tt_provider_secret == "sek"
    assert reset_calls, "session cache must be reset after creds change"

    # upsert (not append-duplicate) on second save
    await auth_routes.tt_save(auth_routes.TTCredsBody(provider_secret="sek2"))
    text = envfile.read_text()
    assert text.count("TT_PROVIDER_SECRET=") == 1 and "sek2" in text
    assert "TT_REFRESH_TOKEN=reftok" in text          # untouched field kept

    status = await auth_routes.tt_status()
    assert status["configured"] is True
