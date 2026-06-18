"""Risk engine tests (pure, deterministic).

Run: python -m tests.test_risk

Covers risk-fractional sizing, the shared buying-power pool, the portfolio-risk
cap, max-positions and per-day trade caps, the daily-loss halt, duplicate-symbol
rejection, ET day rollover, and restart reconstruction (seed_day + count_trade).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.engine.risk import RiskEngine, RiskLimits

ET = ZoneInfo("America/New_York")


def _limits(**kw) -> RiskLimits:
    base = dict(risk_per_trade_pct=0.01, max_positions=5, max_portfolio_risk_pct=0.05,
                max_daily_loss_pct=0.03, max_trades_per_day=20)
    base.update(kw)
    return RiskLimits(**base)


def _run() -> None:
    # --- risk-fractional sizing -----------------------------------------
    e = RiskEngine(10_000.0, _limits())
    s = e.check_and_size("AAA", entry=50.0, stop=48.0)  # per-share risk 2; budget 100
    assert s.quantity == 50, s            # floor(100 / 2)
    assert s.notional == 2500.0, s
    assert s.risk_amount == 100.0, s
    print("PASS: risk-fractional sizing = floor(equity·1% / per-share risk)")

    # --- duplicate symbol rejected --------------------------------------
    e.register_open("AAA", s.quantity, 50.0, 48.0)
    dup = e.check_and_size("AAA", entry=50.0, stop=48.0)
    assert dup.quantity == 0 and "already" in dup.rejection, dup
    print("PASS: duplicate-symbol entry rejected")

    # --- shared buying-power pool ---------------------------------------
    # Small allocation; one position consumes most of the cash, the next is capped.
    pool = RiskEngine(1_000.0, _limits(max_portfolio_risk_pct=1.0))
    a = pool.check_and_size("AAA", entry=90.0, stop=80.0)  # risk 10; budget 10 -> 1 share
    assert a.quantity == 1 and a.notional == 90.0, a
    pool.register_open("AAA", a.quantity, 90.0, 80.0)
    # 910 cash left; a 100-priced share with per-share risk 10 -> budget 10 -> 1 share, affordable
    b = pool.check_and_size("BBB", entry=100.0, stop=90.0)
    assert b.quantity == 1, b
    pool.register_open("BBB", b.quantity, 100.0, 90.0)
    assert round(pool.remaining_cash, 2) == 810.0, pool.remaining_cash
    print("PASS: positions draw on one shared buying-power pool")

    # --- buying power exhausted -> reject -------------------------------
    # Generous risk budget so cash (not risk) is the binding constraint.
    broke = RiskEngine(100.0, _limits(risk_per_trade_pct=1.0, max_portfolio_risk_pct=1.0,
                                      max_positions=10))
    broke.register_open("AAA", 1, 90.0, 80.0)  # 90 of 100 cash used
    none = broke.check_and_size("BBB", entry=50.0, stop=49.0)  # only 10 cash left
    assert none.quantity == 0 and "buying power" in none.rejection, none
    print("PASS: insufficient buying power rejects the entry")

    # --- portfolio-risk cap (independent of position count/cash) --------
    # risk_per_trade 1% = 100; portfolio cap 2% = 200 -> only 2 such trades admitted.
    rc = RiskEngine(10_000.0, _limits(max_portfolio_risk_pct=0.02, max_positions=10))
    rc.register_open("AAA", 50, 50.0, 48.0)  # risk 100
    rc.register_open("BBB", 50, 50.0, 48.0)  # risk 100 -> 200 used = cap
    capped = rc.check_and_size("CCC", entry=50.0, stop=48.0)
    assert capped.quantity == 0 and "portfolio risk" in capped.rejection, capped
    print("PASS: aggregate portfolio-risk cap blocks further entries")

    # --- max positions cap ----------------------------------------------
    mp = RiskEngine(1_000_000.0, _limits(max_positions=2, max_portfolio_risk_pct=1.0))
    mp.register_open("AAA", 1, 50.0, 49.0)
    mp.register_open("BBB", 1, 50.0, 49.0)
    over = mp.check_and_size("CCC", entry=50.0, stop=49.0)
    assert over.quantity == 0 and "max positions" in over.rejection, over
    print("PASS: max-positions cap enforced")

    # --- per-day trade cap ----------------------------------------------
    tc = RiskEngine(1_000_000.0, _limits(max_trades_per_day=1, max_portfolio_risk_pct=1.0))
    tc.register_open("AAA", 1, 50.0, 49.0)         # counts as 1 trade today
    tc.register_close("AAA", pnl=5.0)              # flat again, but the day's count stands
    capped2 = tc.check_and_size("BBB", entry=50.0, stop=49.0)
    assert capped2.quantity == 0 and "max trades/day" in capped2.rejection, capped2
    print("PASS: per-day trade cap enforced (survives a close)")

    # --- daily-loss halt -------------------------------------------------
    dl = RiskEngine(10_000.0, _limits(max_portfolio_risk_pct=1.0))  # 3% = -300 halts
    dl.register_open("AAA", 1, 50.0, 49.0)
    dl.register_close("AAA", pnl=-350.0)
    halted = dl.check_and_size("BBB", entry=50.0, stop=49.0)
    assert halted.quantity == 0 and "daily loss" in halted.rejection, halted
    assert dl.snapshot()["halted"] is True
    print("PASS: daily-loss halt stops new entries")

    # --- ET day rollover resets daily counters --------------------------
    # Thread explicit ``now`` everywhere so the test is wall-clock-independent
    # (a fixed absolute day-1, then a timestamp on day-2 that triggers the roll).
    d1 = date(2024, 6, 4)
    day1 = datetime(2024, 6, 4, 17, 0, tzinfo=timezone.utc)   # 1pm ET, day 1
    next_day = datetime(2024, 6, 5, 17, 0, tzinfo=timezone.utc)  # 1pm ET, day 2
    roll = RiskEngine(10_000.0, _limits(), today=d1)
    roll.register_open("AAA", 1, 50.0, 49.0, now=day1)
    roll.register_close("AAA", pnl=-400.0, now=day1)
    assert roll.snapshot()["halted"] is True
    # A timestamp on the next ET day rolls the counters back to zero.
    ok = roll.check_and_size("BBB", entry=50.0, stop=49.0, now=next_day)
    assert ok.quantity > 0, ok
    assert roll.trades_today == 0 and roll.realized_today == 0.0
    print("PASS: ET day rollover resets trades/realized")

    # --- restart reconstruction (count_trade=False + seed_day) ----------
    rs = RiskEngine(10_000.0, _limits())
    rs.register_open("AAA", 50, 50.0, 48.0, count_trade=False)  # rebuilt, not a new trade
    rs.seed_day(trades_today=3, realized_today=-120.0)
    assert rs.trades_today == 3 and rs.open_count == 1, (rs.trades_today, rs.open_count)
    assert rs.used_risk == 100.0, rs.used_risk
    print("PASS: restart reconstruction seeds counters without double-counting")

    print("\nALL RISK ENGINE TESTS PASSED")


if __name__ == "__main__":
    _run()
