"""9/15 EMA Crossover strategy (stock-only, long + short).

Signals are generated entirely from stock data: an EMA9/EMA15 crossover confirmed
by close-vs-EMAs, a volume surge, and EMA-slope direction, with an optional 15m
higher-timeframe trend filter. Pure signal/replay logic lives in ``signals``;
parameters in ``params``. The ``EmaCrossoverStrategy`` class wires the core to the
engine's ``BaseStrategy`` interface (multi-position live trading + backtest); risk
sizing / portfolio limits live in :mod:`app.engine.risk`.
"""

from app.strategies.ema_crossover.strategy import EmaCrossoverStrategy

__all__ = ["EmaCrossoverStrategy"]
