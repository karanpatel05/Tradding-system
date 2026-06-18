from app.ibkr.contracts import stock


def test_stock_contract_shape():
    c = stock("NVDA")
    assert c.symbol == "NVDA"
    assert c.secType == "STK"
    assert c.exchange == "SMART"
    assert c.currency == "USD"


def test_stock_uppercases():
    assert stock("nvda").symbol == "NVDA"
