from datetime import date

from app.options.contracts import occ_ticker, parse_occ


def test_occ_ticker_call():
    assert occ_ticker("SPY", date(2026, 6, 22), "C", 747.0) == "O:SPY260622C00747000"


def test_occ_ticker_put_and_fractional_strike():
    assert occ_ticker("F", date(2025, 1, 17), "P", 7.5) == "O:F250117P00007500"


def test_parse_occ_round_trip():
    t = "O:SPY260622C00747000"
    p = parse_occ(t)
    assert p == {"underlying": "SPY", "expiry": date(2026, 6, 22), "right": "C", "strike": 747.0}
    assert occ_ticker(p["underlying"], p["expiry"], p["right"], p["strike"]) == t
