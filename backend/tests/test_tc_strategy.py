from datetime import date

from app.strategies.triple_calendar.params import TripleCalendarParams
from app.strategies.triple_calendar.strategy import decide_entry, manage


class StubSource:
    """Deterministic source: option price rises with time-to-expiry, so the longer-dated
    buy leg is always worth more than the short sell leg -> a positive calendar debit."""
    def __init__(self, spot, strikes):
        self.spot, self.strikes = spot, strikes

    def underlying_close(self, symbol, day):
        return self.spot

    def chain_strikes(self, underlying, expiry):
        return self.strikes

    def option_close(self, underlying, expiry, strike, right, day):
        return 1.0 + 0.1 * max((expiry - day).days, 0)


def test_decide_entry_builds_three_calendars():
    src = StubSource(445.0, [float(r) for r in range(400, 491, 5)])
    p = TripleCalendarParams(underlying="QQQ")
    pos = decide_entry(date(2026, 6, 25), src, p, events=[], vix=None)
    assert pos is not None and len(pos.calendars) == 3
    assert pos.net_debit > 0


def test_vix_gate_skips_entry_above_ceiling():
    """Hard VIX gate: no entry when VIX is known and above vix_entry_max (16)."""
    src = StubSource(445.0, [float(r) for r in range(400, 491, 5)])
    p = TripleCalendarParams()  # vix_entry_max defaults to 16.0
    assert decide_entry(date(2026, 6, 25), src, p, events=[], vix=17.0) is None
    # At the boundary (<= 16) and below, entry is allowed.
    assert decide_entry(date(2026, 6, 25), src, p, events=[], vix=16.0) is not None
    assert decide_entry(date(2026, 6, 25), src, p, events=[], vix=12.0) is not None
    # vix=None keeps the pure layer permissive (backtest/tests unaffected); the live
    # loop enforces fail-closed separately.
    assert decide_entry(date(2026, 6, 25), src, p, events=[], vix=None) is not None


def test_event_filter_blocks_entry():
    src = StubSource(445.0, [float(r) for r in range(400, 491, 5)])
    p = TripleCalendarParams()
    from app.strategies.triple_calendar.legs import target_expiry
    ev = target_expiry(date(2026, 6, 25), 21)  # event on/before the sell expiry
    assert decide_entry(date(2026, 6, 25), src, p, events=[ev], vix=None) is None


def test_manage_exits_on_profit_target():
    p = TripleCalendarParams(profit_target=0.10, roll_on_exit=False)
    from app.strategies.triple_calendar.legs import Calendar, Position
    se, be = date(2026, 7, 17), date(2026, 7, 24)
    pos = Position("QQQ", date(2026, 6, 25), [Calendar(445.0, se, be)], net_debit=1.0)

    class S:
        def underlying_close(self, *a): return 445.0
        def chain_strikes(self, *a): return [445.0]
        def option_close(self, u, e, k, r, d): return 3.0 if e == be else 1.9  # value 1.1 = +10%
    action, _ = manage(pos, date(2026, 7, 1), S(), p)
    assert action == "exit"
