"""Kill switch: cancel-then-close ordering; equity vs option close actions; paper mode
raises (honest not-flat) and touches no order API; live failures raise with the list.

Note on enum-string assertions (same deviation as test_broker_combo.py): the brief's
test recorded str(l.action) and asserted e.g. "BUY_TO_CLOSE" in acts[...]. Verified
against the installed SDK (v12, tastytrade.order.OrderAction, a StrEnum):
str(OrderAction.SELL) == "Sell", str(OrderAction.BUY_TO_CLOSE) == "Buy to Close" —
human-readable phrases, not identifiers — so the brief's raw substring check would fail
case-sensitively against the real SDK. Fixed by recording `l.action.name` instead
(e.g. "SELL", "BUY_TO_CLOSE"), which is what the test actually intends to check. No
implementation code depends on this — orders.py never calls str() on an OrderAction."""
from types import SimpleNamespace

import pytest

from app.broker import orders as ord_mod
from app.broker.account import NormalizedPosition
from app.config import get_settings


def _pos(symbol, qty, itype="Equity"):
    return NormalizedPosition(symbol=symbol, quantity=qty,
                              quantity_direction="Long" if qty > 0 else "Short",
                              instrument_type=itype, avg_price=1.0, account="X")


@pytest.fixture
def wired(monkeypatch):
    calls = SimpleNamespace(cancelled=[], placed=[])
    async def live_orders():
        return [SimpleNamespace(id=1, legs=[SimpleNamespace(symbol="AAPL")])]
    async def cancel(oid):
        calls.cancelled.append(oid)
        return True
    monkeypatch.setattr(ord_mod, "_live_orders", live_orders)
    monkeypatch.setattr(ord_mod, "_cancel_order", cancel)
    async def poss():
        return [_pos("AAPL", 10), _pos("QQQ   260807C00725000", -2, "Equity Option")]
    monkeypatch.setattr(ord_mod, "_positions", poss)
    async def sess():
        return object()
    monkeypatch.setattr(ord_mod, "_session", sess)
    async def place_order(session, order, dry_run=True):
        calls.placed.append((dry_run, [(l.symbol, l.action.name, int(l.quantity))
                                       for l in order.legs]))
        return SimpleNamespace(order=SimpleNamespace(id=9))
    async def acct():
        return SimpleNamespace(place_order=place_order)
    monkeypatch.setattr(ord_mod, "_account", acct)
    # Fake instruments return real SDK Leg objects (InstrumentType.EQUITY /
    # EQUITY_OPTION) so NewOrder's pydantic validation stays in force — a bare
    # SimpleNamespace leg is rejected by `list[Leg]` (see test_broker_combo.py /
    # test_broker_equity_orders.py / test_broker_bracket.py for the same pattern).
    from tastytrade.order import InstrumentType, Leg
    async def fake_equity(session, symbols):
        sym = list(symbols)[0]
        return [SimpleNamespace(symbol=sym, build_leg=lambda qty, action: Leg(
            instrument_type=InstrumentType.EQUITY, symbol=sym, action=action,
            quantity=qty))]
    monkeypatch.setattr(ord_mod, "_equities", fake_equity)
    async def fake_option(session, occ):
        return SimpleNamespace(symbol=occ, build_leg=lambda qty, action: Leg(
            instrument_type=InstrumentType.EQUITY_OPTION, symbol=occ, action=action,
            quantity=qty))
    monkeypatch.setattr(ord_mod, "_option", fake_option)
    return calls


@pytest.mark.asyncio
async def test_live_flatten_cancels_then_closes(wired, monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_live_orders", True)
    msg = await ord_mod.flatten_all()
    assert wired.cancelled == [1]
    # closes are market orders placed for real (dry_run False), correct directions:
    placed = [p for p in wired.placed if p[0] is False]
    acts = {p[1][0][0]: p[1][0][1] for p in placed}
    assert "SELL" in acts["AAPL"]                                # long equity -> SELL
    assert "BUY_TO_CLOSE" in acts["QQQ   260807C00725000"]       # short option -> BUY_TO_CLOSE
    assert "Flattened 2" in msg


@pytest.mark.asyncio
async def test_paper_flatten_raises_honest(monkeypatch, wired):
    monkeypatch.setattr(get_settings(), "enable_live_orders", False)
    with pytest.raises(RuntimeError) as ei:
        await ord_mod.flatten_all()
    assert "not flat" in str(ei.value)
    assert wired.placed == []            # no order API in paper mode
