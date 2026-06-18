"""9/15 trending-EMA scalper (strategy_1).

A single pure signal core (:func:`signals.generate_signal`) drives **both**
backtest and live — the backtest is a replay loop feeding the same function
growing slices of history, and the live worker feeds it each freshly-closed
5-minute bar. Parity is true by construction: there is exactly one decision
function and one set of price levels (trigger / stop / target).
"""

from app.strategies.ema_scalper.strategy import EmaScalperStrategy

__all__ = ["EmaScalperStrategy"]
