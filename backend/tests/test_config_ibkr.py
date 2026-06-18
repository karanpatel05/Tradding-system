from app.config import Settings


def test_ibkr_defaults_present_and_old_removed():
    s = Settings()
    assert s.ibkr_host == "127.0.0.1"
    assert s.ibkr_port == 4002          # paper Gateway
    assert s.ibkr_client_id == 1
    assert s.ibkr_account == ""         # blank -> auto-detect first managed acct
    assert s.ibkr_market_data_type == 1
    assert s.enable_live_orders is False
    # old backends gone
    assert not hasattr(s, "massive_api_key")
    assert not hasattr(s, "tastytrade_provider_secret")
