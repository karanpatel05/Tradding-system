"""Order routing defaults to the Alpaca PAPER account, but stays refused without paper keys
or with a non-paper host; and no test run can read or write the real backend/.env."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.broker import alpaca_client as ac
from app.broker import orders
from app.config import Settings, get_settings


def test_routing_defaults_to_alpaca_paper():
    assert Settings.model_fields["enable_live_orders"].default is True


@pytest.fixture
def no_network(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("no Alpaca request may be made")
    monkeypatch.setattr(ac, "request", boom)


@pytest.mark.asyncio
async def test_routing_on_without_paper_keys_is_refused_before_any_request(monkeypatch,
                                                                           no_network):
    s = get_settings()
    monkeypatch.setattr(s, "enable_live_orders", True)
    monkeypatch.setattr(s, "apca_api_key_id", "")
    monkeypatch.setattr(s, "apca_api_secret_key", "")
    monkeypatch.setattr(s, "apca_api_base_url", "https://paper-api.alpaca.markets")

    async def not_safe():
        return False
    monkeypatch.setattr(orders, "is_safe_mode", not_safe)
    blocked = orders._paper_lock_blocked()
    assert blocked and blocked["transmitted"] is False and "keys" in blocked["detail"]
    out = await orders.submit_option_order("XSP991231C00600000", "buy", 1, limit_price=1.0,
                                           position_intent="buy_to_open")
    assert out["transmitted"] is False and "keys" in out["detail"]


@pytest.mark.asyncio
async def test_routing_on_with_a_non_paper_host_is_refused(monkeypatch, no_network):
    s = get_settings()
    monkeypatch.setattr(s, "enable_live_orders", True)
    monkeypatch.setattr(s, "apca_api_key_id", "k")
    monkeypatch.setattr(s, "apca_api_secret_key", "x")
    monkeypatch.setattr(s, "apca_api_base_url", "https://api.alpaca.markets")
    assert orders._paper_lock_blocked()["detail"] == orders.BLOCKED_DETAIL


def test_tests_never_see_real_credentials_or_routing():
    s = get_settings()
    assert s.apca_api_key_id == "" and s.apca_api_secret_key == ""
    assert s.tt_provider_secret == "" and s.tt_refresh_token == ""
    assert s.enable_live_orders is False          # tests opt in explicitly


def test_env_writers_are_redirected_during_tests():
    from app.auth import routes as auth_routes
    from app.routers import system
    real = (Path(__file__).resolve().parents[1] / ".env").resolve()
    assert Path(system._ENV_PATH).resolve() != real
    assert Path(auth_routes._ENV_PATH).resolve() != real


# --- the guard that refuses a test run next to a real backend/.env -----------------------
def _layout(tmp_path: Path, *, git_dir: bool, env: str | None) -> Path:
    repo = tmp_path / "repo"
    backend = repo / "backend"
    backend.mkdir(parents=True)
    if git_dir:
        (repo / ".git").mkdir()                       # the main checkout
    else:
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")   # a worktree
    if env == "file":
        (backend / ".env").write_text("X=1\n")
    elif env == "symlink":
        target = tmp_path / "real.env"
        target.write_text("X=1\n")
        os.symlink(target, backend / ".env")
    return backend


def test_guard_refuses_a_symlinked_env(tmp_path):
    from tests._env_guard import env_problem
    assert "symlink" in env_problem(_layout(tmp_path, git_dir=False, env="symlink"))


def test_guard_refuses_the_main_checkout_env(tmp_path):
    from tests._env_guard import env_problem
    assert "main checkout" in env_problem(_layout(tmp_path, git_dir=True, env="file"))


def test_guard_allows_a_worktree_with_its_own_or_no_env(tmp_path):
    from tests._env_guard import env_problem
    assert env_problem(_layout(tmp_path / "a", git_dir=False, env="file")) is None
    assert env_problem(_layout(tmp_path / "b", git_dir=False, env=None)) is None
    assert env_problem(_layout(tmp_path / "c", git_dir=True, env=None)) is None
