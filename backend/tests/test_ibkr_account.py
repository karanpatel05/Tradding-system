from app.ibkr.account import NormalizedPosition, normalize_position


class _C:  # fake ib_async Contract
    def __init__(self, symbol, secType="STK"):
        self.symbol = symbol
        self.secType = secType


class _P:  # fake ib_async Position
    def __init__(self, account, contract, position, avgCost):
        self.account, self.contract, self.position, self.avgCost = account, contract, position, avgCost


def test_normalize_long_and_short():
    lp = normalize_position(_P("DU1", _C("NVDA"), 10, 100.0))
    assert isinstance(lp, NormalizedPosition)
    assert lp.symbol == "NVDA" and lp.quantity == 10
    assert lp.quantity_direction == "Long" and lp.instrument_type == "Equity"
    sp = normalize_position(_P("DU1", _C("AMD"), -5, 50.0))
    assert sp.quantity == -5 and sp.quantity_direction == "Short"


def test_normalize_zero_is_flat():
    zp = normalize_position(_P("DU1", _C("F"), 0, 0.0))
    assert zp.quantity_direction == "Flat"


def test_non_equity_sectype_passthrough():
    op = normalize_position(_P("DU1", _C("SPX", secType="OPT"), 1, 1.0))
    assert op.instrument_type == "OPT"
