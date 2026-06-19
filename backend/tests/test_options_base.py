from datetime import date

from app.options.base import (OptionContract, OptionsProviderError,
                              PolygonNotAuthorized, PolygonNotConfigured)


def test_option_contract_fields():
    c = OptionContract(ticker="O:SPY260622C00747000", underlying="SPY",
                       expiration=date(2026, 6, 22), strike=747.0, right="C",
                       shares_per_contract=100)
    assert c.underlying == "SPY" and c.right == "C" and c.strike == 747.0


def test_error_hierarchy():
    assert issubclass(PolygonNotConfigured, OptionsProviderError)
    assert issubclass(PolygonNotAuthorized, OptionsProviderError)
