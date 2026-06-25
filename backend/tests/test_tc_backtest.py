from datetime import date, timedelta

from app.strategies.triple_calendar.backtest import compute_metrics, run_backtest
from app.strategies.triple_calendar.legs import target_expiry
from app.strategies.triple_calendar.params import TripleCalendarParams


def _max_expiry():
    se = target_expiry(date(2026, 6, 2), 21)
    return target_expiry(se + timedelta(days=7), 0)


class RisingSource:
    """ATM calendar that gains value over time -> hits the 10% target."""
    def __init__(self):
        self.start = date(2026, 6, 1)
        self.buy_expiry = _max_expiry()

    def trading_days(self, s, e):
        d, out = s, []
        while d <= e:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    def underlying_close(self, sym, day):
        return 445.0

    def chain_strikes(self, u, e):
        return [float(r) for r in range(400, 491, 5)]

    def option_close(self, u, expiry, strike, right, day):
        # long (buy) leg appreciates daily so the debit grows past +10%
        if expiry == self.buy_expiry:
            return 3.0 + 0.05 * (day - self.start).days
        return 2.0


def test_backtest_runs_and_records_a_trade():
    p = TripleCalendarParams()
    trades = run_backtest(RisingSource(), p, date(2026, 6, 2), date(2026, 7, 20))
    assert len(trades) >= 1
    t = trades[0]
    assert {"entry_date", "exit_date", "pnl_pct", "hold_days", "reason"} <= set(t)


def test_compute_metrics_shape():
    trades = [{"pnl_pct": 0.1, "hold_days": 5, "reason": "target", "entry_iv": 0.2},
              {"pnl_pct": -0.05, "hold_days": 9, "reason": "time_stop", "entry_iv": 0.4}]
    m = compute_metrics(trades)
    assert m["total_trades"] == 2 and m["win_rate"] == 0.5
    assert "avg_hold_days" in m and "max_drawdown" in m and "iv_regime" in m
