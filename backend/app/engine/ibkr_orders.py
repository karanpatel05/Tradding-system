"""Pure IBKR order construction (no network): intent->action and bracket assembly.

A bracket is a MARKET parent (transmit=False) plus an OCO pair — take-profit LIMIT
and stop STP — that share the parent's id; the last child carries transmit=True so
IBKR releases the whole group atomically. Order ids are assigned by the caller (the
live path) via ib.client.getReqId(); here they default to 0 so the structure is
unit-testable."""
from __future__ import annotations

from ib_async import LimitOrder, MarketOrder, StopOrder

_INTENT_TO_ACTION = {
    "enter_long": "BUY", "exit_short": "BUY",
    "exit_long": "SELL", "enter_short": "SELL",
}


def intent_to_action(intent: str) -> str:
    try:
        return _INTENT_TO_ACTION[intent]
    except KeyError:
        raise ValueError(f"Unknown order intent: {intent}")


def build_bracket(side: str, quantity: int, *, target: float, stop: float,
                  parent_id: int = 0, tp_id: int = 0, stop_id: int = 0):
    if side == "long":
        entry_action, close_action = "BUY", "SELL"
    elif side == "short":
        entry_action, close_action = "SELL", "BUY"
    else:
        raise ValueError(f"Unknown side: {side!r}")

    parent = MarketOrder(entry_action, quantity)
    parent.orderId = parent_id
    parent.transmit = False

    tp = LimitOrder(close_action, quantity, target)
    tp.orderId = tp_id
    tp.parentId = parent_id
    tp.transmit = False

    stp = StopOrder(close_action, quantity, stop)
    stp.orderId = stop_id
    stp.parentId = parent_id
    stp.transmit = True
    return parent, tp, stp
