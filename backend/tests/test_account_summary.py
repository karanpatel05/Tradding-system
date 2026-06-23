from app.ibkr.account import normalize_account_summary


class _V:  # mimics ib_async AccountValue
    def __init__(self, tag, value, currency="USD", account="DU1"):
        self.tag, self.value, self.currency, self.account = tag, value, currency, account


def test_normalize_maps_tags_and_parses_floats():
    rows = [
        _V("NetLiquidation", "2000.50"), _V("TotalCashValue", "1500.25"),
        _V("BuyingPower", "8000"), _V("AvailableFunds", "1900"),
        _V("GrossPositionValue", "500"), _V("UnrealizedPnL", "-12.5"),
        _V("RealizedPnL", "7.0"), _V("InitMarginReq", "100"),
        _V("MaintMarginReq", "80"), _V("AccountType", "INDIVIDUAL"),  # ignored
    ]
    s = normalize_account_summary(rows)
    assert s["net_liquidation"] == 2000.50 and s["total_cash"] == 1500.25
    assert s["buying_power"] == 8000.0 and s["available_funds"] == 1900.0
    assert s["gross_position_value"] == 500.0
    assert s["unrealized_pnl"] == -12.5 and s["realized_pnl"] == 7.0
    assert s["init_margin"] == 100.0 and s["maint_margin"] == 80.0
    assert s["account"] == "DU1" and s["currency"] == "USD"


def test_normalize_missing_tags_default_none():
    s = normalize_account_summary([_V("NetLiquidation", "100")])
    assert s["net_liquidation"] == 100.0
    assert s["buying_power"] is None and s["realized_pnl"] is None


def test_normalize_empty_input():
    s = normalize_account_summary([])
    assert s["account"] is None and s["currency"] is None
    assert all(s[k] is None for k in ("net_liquidation", "total_cash", "buying_power"))
