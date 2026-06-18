"""Daily selection-history aggregation test (pure, deterministic).

Run: python -m tests.test_ema_selection
"""

from __future__ import annotations

from app.strategies.ema_scalper.selection import aggregate_selection, select_daily_trades


def _trade(side, entry, exit_, etype, *, symbol=None, date=None):
    t = {"side": side, "entry_price": entry, "exit_price": exit_,
         "entry_type": etype, "r_multiple": 2.0}
    if symbol:
        t["symbol"] = symbol
    if date:
        t["date"] = date
    return t


def _run() -> None:
    # 3 symbols, day "2024-06-03": NVDA (score 95, long, win), AMD (91, short, loss),
    # PLTR (89, passes but no setup that day).
    analyses = [
        {"symbol": "NVDA", "pass_days": {"2024-06-03": 95.2},
         "trades_by_day": {"2024-06-03": _trade("long", 100.0, 104.0, "engulfing")}},
        {"symbol": "AMD", "pass_days": {"2024-06-03": 91.7},
         "trades_by_day": {"2024-06-03": _trade("short", 50.0, 51.0, "pin")}},
        {"symbol": "PLTR", "pass_days": {"2024-06-03": 89.4}, "trades_by_day": {}},
    ]

    rows = aggregate_selection(universe_size=40, analyses=analyses, capital=2000.0)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["stocks_scanned"] == 40
    assert row["stocks_passing"] == 3, row
    assert row["valid_setups"] == 2, row            # NVDA + AMD traded; PLTR didn't
    assert row["selected"] == "NVDA", row           # highest score among setups
    assert row["selection_reason"] == "Highest Scanner Score"
    assert row["outcome"] == "WIN", row             # NVDA long 100→104
    print(f"PASS: day row — scanned 40, passing 3, setups 2, selected NVDA ({row['outcome']})")

    # Setups ranked best-first; AMD short 50→51 is a loss.
    setups = {s["symbol"]: s for s in row["setups"]}
    assert [s["symbol"] for s in row["setups"]] == ["NVDA", "AMD"], row["setups"]
    assert setups["AMD"]["outcome"] == "LOSS", setups["AMD"]
    assert setups["NVDA"]["shares"] == 20, setups["NVDA"]  # floor(2000/100)
    print("PASS: setups ranked by score, outcomes + sizing correct")

    # Date filtering + empty universe.
    assert aggregate_selection(40, analyses, 2000.0, start="2024-07-01") == []
    assert aggregate_selection(0, [], 2000.0) == []
    print("PASS: date filter + empty inputs")

    # --- select_daily_trades: the universe backtest's daily picks ---
    # Two days. Day 1: NVDA (95, setup) beats AMD (91, setup) and PLTR (89, no setup).
    # Day 2: only AMD has both a pass score and a setup → it's picked.
    bt_analyses = [
        {"symbol": "NVDA",
         "pass_days": {"2024-06-03": 95.2},
         "trades_by_day": {"2024-06-03": _trade("long", 100.0, 104.0, "engulfing",
                                                symbol="NVDA", date="2024-06-03")}},
        {"symbol": "AMD",
         "pass_days": {"2024-06-03": 91.7, "2024-06-04": 88.0},
         "trades_by_day": {"2024-06-03": _trade("short", 50.0, 51.0, "pin",
                                                symbol="AMD", date="2024-06-03"),
                           "2024-06-04": _trade("long", 60.0, 63.0, "reversal",
                                                symbol="AMD", date="2024-06-04")}},
        {"symbol": "PLTR",
         "pass_days": {"2024-06-03": 89.4, "2024-06-04": 99.0},  # tops day 2 but no setup
         "trades_by_day": {}},
    ]
    chosen = select_daily_trades(bt_analyses)
    assert [t["symbol"] for t in chosen] == ["NVDA", "AMD"], chosen      # chronological, one/day
    assert [t["date"] for t in chosen] == ["2024-06-03", "2024-06-04"], chosen
    # Day 2: PLTR scored highest (99) but had no setup, so AMD is taken — not skipped.
    assert chosen[1]["symbol"] == "AMD", chosen[1]
    print("PASS: select_daily_trades picks best-with-setup per day, chronological")

    # Range filter trims by ISO day.
    only2 = select_daily_trades(bt_analyses, start="2024-06-04")
    assert [t["symbol"] for t in only2] == ["AMD"], only2
    assert select_daily_trades([]) == []
    print("PASS: select_daily_trades date filter + empty input")

    print("\nALL SELECTION-HISTORY TESTS PASSED")


if __name__ == "__main__":
    _run()
