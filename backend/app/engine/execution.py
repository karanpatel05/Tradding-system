"""Order execution + flatten — now served by the Tastytrade broker layer.

This module keeps its historical import path and public API (the worker, Triple
Calendar executor, kill switch, and routers all import from here); the implementation
lives in app/broker/orders.py. ENABLE_LIVE_ORDERS gating, SAFE_MODE blocking, and
return-dict shapes are preserved there verbatim."""
from __future__ import annotations

from app.broker.orders import (cancel_symbol_orders, flatten_all,  # noqa: F401
                               poll_bracket_fill, submit_bracket_order,
                               submit_combo_order, submit_equity_order)

__all__ = ["submit_equity_order", "submit_bracket_order", "cancel_symbol_orders",
           "poll_bracket_fill", "flatten_all", "submit_combo_order"]
