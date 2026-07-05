"""Triple Calendar manage() exits: +10% target, -25% stop-loss, NO time stop.
   Run: python -m tests.test_tc_exit
"""
from __future__ import annotations

from datetime import date, timedelta


def _run() -> None:
    from app.strategies.triple_calendar.legs import Calendar, Position
    from app.strategies.triple_calendar.params import TripleCalendarParams
    from app.strategies.triple_calendar.strategy import manage

    day = date(2026, 1, 1)
    params = TripleCalendarParams(roll_on_exit=False)  # classic exits: profit 0.10, stop 0.25

    def run_case(value: float, sell_dte: int):
        sell_exp = day + timedelta(days=sell_dte)
        buy_exp = day + timedelta(days=sell_dte + 7)
        cal = Calendar(strike=725.0, sell_expiry=sell_exp, buy_expiry=buy_exp)
        pos = Position(underlying="QQQ", entry_date=day, calendars=[cal], net_debit=10.0)

        class Src:
            # calendar value = buy - sell = `value`
            def option_close(self, u, expiry, strike, right, d):
                return value if expiry == buy_exp else 0.0
            def underlying_close(self, u, d):
                return None  # skip breach adjustment
        return manage(pos, day, Src(), params)

    # +10% target: value 11 on net_debit 10
    action, info = run_case(11.0, sell_dte=21)
    assert action == "exit" and info["reason"] == "target", (action, info)

    # -25% stop-loss: value 7.5 on net_debit 10
    action, info = run_case(7.5, sell_dte=21)
    assert action == "exit" and info["reason"] == "stop_loss", (action, info)

    # flat (value 10) and FAR from expiry → hold
    action, info = run_case(10.0, sell_dte=21)
    assert action == "hold", (action, info)

    # flat (value 10) but only 3 DTE to sell leg → still HOLD (time stop removed)
    action, info = run_case(10.0, sell_dte=3)
    assert action == "hold", f"time stop should be gone: {(action, info)}"

    print("PASS: target +10%, stop-loss -25%, no time stop")
    print("\nALL TC-EXIT TESTS PASSED")


if __name__ == "__main__":
    _run()
