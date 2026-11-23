"""Application configuration, loaded from environment / .env.

Design notes
------------
* ``provider_secret`` (the Tastytrade OAuth client secret) is **optional at boot**.
  The app must start and render the auth panel even when it is missing — in that
  case the auth flow reports an "OAuth not configured" state instead of crashing.
* Secrets (provider secret, refresh token, session token) live server-side only
  and are never serialized to any frontend-facing response model.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The backend package root (…/backend). Persistence paths are anchored here so the
# session store, candle cache, and SQLite DB are the SAME files regardless of the
# working directory uvicorn is launched from — otherwise a different CWD silently
# "loses" the saved secret, cached backtests, and trade history.
_BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Anchored to backend/.env regardless of the process CWD: the LaunchAgent runs
        # uvicorn from the repo root, so a relative ".env" silently reads the wrong file
        # (which is how saved Tastytrade creds vanished on every restart).
        env_file=str(_BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_name: str = "Multi-Strategy Trading System"
    environment: str = "development"
    # Secret used to sign the app session cookie. Override in production.
    app_secret_key: str = "dev-insecure-change-me"
    # Shared caller token for the dangerous mutating routes (arm live orders, kill,
    # activate, resume, credential-write). Sent by the client as the X-App-Token header
    # and compared to this value. FAIL CLOSED: while this is empty those routes deny all
    # callers — set APP_AUTH_TOKEN in backend/.env and paste it in the API tab to enable
    # them. Reads (dashboard/backtest/journal) stay open regardless (no-gate landing).
    app_auth_token: str = ""
    # Comma-separated list of allowed CORS origins (the Vite dev server by default).
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Database ---
    # Defaults to a local async SQLite file so the app runs with zero infra.
    # docker-compose overrides this with a Postgres URL.
    database_url: str = f"sqlite+aiosqlite:///{_BASE_DIR / 'trading.db'}"

    # --- Tastytrade (OAuth personal grant; my.tastytrade.com → API access) ---
    # Sole broker and data source: live quotes/streaming/accounts/orders AND
    # historical bars, all via the Tastytrade SDK. "Authenticated" means the OAuth
    # pair verifies (see app/broker/status.py). Bulk backtests still read cached
    # data_cache/*.
    tt_provider_secret: str = ""     # env TT_PROVIDER_SECRET
    tt_refresh_token: str = ""       # env TT_REFRESH_TOKEN
    tt_account_number: str = ""      # blank -> first account on the login

    # --- Alpaca (status display only; the app never routes orders via Alpaca) ---
    apca_api_key_id: str = ""        # env APCA_API_KEY_ID
    apca_api_secret_key: str = ""    # env APCA_API_SECRET_KEY
    apca_api_base_url: str = "https://paper-api.alpaca.markets"  # env APCA_API_BASE_URL
    # Mid sanity guard: reject crossed/wide/jumpy option mids (see marketdata/sanity.py).
    sanity_rel_spread: float = 0.35
    sanity_abs_spread: float = 2.50
    sanity_max_jump_pct: float = 0.50
    sanity_jump_window_seconds: int = 900

    # --- Historical data cache ---
    # Candle history (daily + 5-min) is cached to local disk after the first pull
    # so reopening the strategy page reads from the cache instead of re-hitting the
    # API. The cache dir is git-ignored. TTL controls how stale a cache entry may be
    # before a refetch (a failed refetch falls back to the stale cache).
    history_cache_enabled: bool = True
    history_cache_dir: str = str(_BASE_DIR / "data_cache")
    history_cache_ttl_hours: float = 12.0

    # When set (the web/hosted run mode sets FRONTEND_DIST), the API also serves the built
    # React app from this directory at "/" with SPA fallback. Empty in dev (Vite serves UI).
    frontend_dist: str = ""

    builder_monitor_interval_seconds: int = 60  # options-builder paper-trade revaluation cadence
    vix_wait_poll_seconds: int = 5   # while flat + VIX > entry ceiling, re-check VIX this often

    # --- Session persistence ---
    # When True, the login refresh token + app session id are written to
    # ``session_store_path`` (0600) so the session survives a server restart and
    # the user isn't re-prompted on every reopen. The file holds secrets at rest;
    # it is git-ignored. Set False to keep sessions memory-only (legacy behaviour).
    persist_session: bool = True
    session_store_path: str = str(_BASE_DIR / ".session_store.json")

    # --- Engine ---
    max_strategies: int = 5
    # How often (seconds) each active strategy worker re-scans the universe.
    scan_interval_seconds: int = 60

    # --- Order routing (SAFETY GATE) ---
    # Master switch for transmitting orders to the Alpaca PAPER account. Default False:
    # strategies run their full logic (signals, sizing, trade records) with simulated
    # fills and no order is sent until this is explicitly set True. Orders only ever go
    # to Alpaca paper — live (real-money) Alpaca is locked in code (broker/alpaca_client).
    # Tastytrade is market data only and never receives orders.
    enable_live_orders: bool = False

    # --- QQQ first-hour credit spread (runs OUTSIDE this app) ---
    # Folder of the external live runner (com.karan.spreadlive). Read-only: the app
    # reads its lock + live_state.json to show status and to refuse activation.
    qqq_spread_live_dir: str = str(Path.home() / "live_system_maintenance" / "live")
    # In-app run (orders to Alpaca PAPER only, Tastytrade for data). Mirrors the
    # external runner's defaults (live_strategy.py parse_args).
    qqq_spread_model_dir: str = str(
        Path.home() / "live_system_maintenance" / "live" / "models" / "current")
    qqq_spread_max_model_age_days: int = 120
    qqq_spread_max_contracts: int = 1            # TESTING PHASE pin, as in the runner
    qqq_spread_capital_per_spread: float = 1000.0
    qqq_spread_first_spread_capital: float = 500.0
    qqq_spread_min_credit: float = 0.02          # per share; below this, stand down
    qqq_spread_reprice_seconds: float = 15.0
    qqq_spread_grace_minutes: int = 10
    qqq_spread_final_close_seconds: int = 600    # 15:50 ET sweep
    qqq_spread_close_deadline_seconds: int = 300  # aim flat by 15:55 ET

    # --- XSP 0DTE iron fly (xsp_iron_fly) ---
    # Fills are SIMULATED against live Tastytrade NBBO ("sim"); "live" is refused
    # because there is no live XSP order route (see strategies/xsp_iron_fly/executor.py).
    # ENABLE_LIVE_ORDERS does not affect this strategy.
    xsp_fly_mode: str = "sim"
    xsp_fly_entry_time: str = "09:45"            # ET
    xsp_fly_wing_width: float = 10.0             # index points
    xsp_fly_stop_mult: float = 1.5               # per short leg
    xsp_fly_capital_per_fly: float = 5000.0
    xsp_fly_close_before_settlement: bool = False
    xsp_fly_max_contracts: int = 1               # keep small until live results validate
    xsp_fly_max_daily_loss: float = 1000.0       # $; halts entries and caps sizing
    xsp_fly_flatten_on_daily_loss: bool = False
    xsp_fly_max_leg_spread: float = 0.15         # index points
    xsp_fly_max_quote_age: float = 5.0           # seconds
    xsp_fly_min_minutes_to_close: float = 240.0  # skips 13:00 half days by default
    xsp_fly_entry_grace_seconds: float = 120.0
    xsp_fly_fees_per_contract: float = 1.00
    xsp_fly_stop_trigger: str = "ask"            # "ask" | "mark"

    # --- 9/15 EMA Scalping strategy (strategy_1) ---
    # Signal: EMA9/EMA15 on 5-min candles; ATR(14) for slope/stop scaling.
    ema_fast: int = 9
    ema_slow: int = 15
    ema_atr_length: int = 14
    ema_candle_interval: str = "5m"
    # Slope strength: EMA15 linear-regression slope over N candles must exceed
    # ema_slope_min * ATR (positive for longs, negative for shorts).
    ema_slope_lr_lookback: int = 10
    ema_slope_min: float = 0.20
    # Small lookback used for the daily-scanner trend slope + regime warmup.
    ema_slope_lookback: int = 3
    # Sideways/chop reject: stand down when |EMA9 − EMA15| < this * ATR.
    ema_sideways_min_sep_atr: float = 0.15
    # Entry validation: reject extended entries where |price − EMA15| > this * ATR.
    ema_max_entry_distance_atr: float = 2.0
    # Stop = beyond the entry candle's low/high by this * ATR (buffer). Target 1:2.
    ema_stop_buffer_atr: float = 0.10
    ema_risk_reward: float = 2.0
    # An armed entry trigger expires if not hit within this many bars.
    ema_entry_expiry_bars: int = 3
    # Max new entries per trading day (best-fit selection trades the top setup).
    ema_max_trades_per_day: int = 1
    # Modeled per-side slippage (fraction of price) applied to backtest fills.
    ema_slippage_pct: float = 0.0005
    # Scanner (daily) filters → ranked watchlist.
    ema_watchlist_size: int = 10
    ema_trend_ema_length: int = 50       # longer EMA for the daily momentum gate
    ema_min_atr_pct: float = 0.02        # ATR(14)/price floor (volatility)
    ema_min_rel_volume: float = 2.0      # relative volume floor (today vs avg)
    ema_min_gap_pct: float = 0.02        # |open vs prior close| gap floor
    ema_min_avg_volume: int = 1_000_000  # liquidity floor (avg daily shares)
    ema_min_price: float = 10.0          # liquidity floor (min price)
    # Premarket-volume filter is OPT-IN: standard daily/5m candles don't carry
    # premarket volume, so it's off by default (needs extended-hours data).
    ema_use_premarket_filter: bool = False
    ema_min_premarket_volume: int = 100_000
    # Regime filter: only long when SPY above its EMAs, only short when below.
    ema_regime_filter: bool = True
    ema_regime_symbol: str = "SPY"
    # Session window (US/Eastern, 24h "HH:MM"). Flat by the cutoff, no overnight.
    ema_session_start_et: str = "09:35"
    ema_session_cutoff_et: str = "15:45"

    # --- 9/15 EMA Crossover strategy (stock-only, long + short) ---
    # Configurable watchlist (comma-separated). The strategy operates only on these.
    watchlist: str = "TSLA,NVDA,AMD,PLTR,COIN,MSTR,META,NFLX,AMZN,SMCI"
    # Signal: EMA9/EMA15 crossover on 5-min candles.
    xover_fast: int = 9
    xover_slow: int = 15
    xover_atr_length: int = 14
    xover_candle_interval: str = "5m"
    # Entry confirmation: bar volume > multiplier × average volume over the window.
    xover_vol_avg_window: int = 20
    xover_vol_multiplier: float = 1.5
    # EMA slope sign gate: EMA[i] − EMA[i − slope_lookback] must be > 0 (long) / < 0 (short).
    xover_slope_lookback: int = 3
    # Stop = stop_atr_mult × ATR from entry; target = risk_reward × risk. Configurable.
    xover_stop_atr_mult: float = 1.5
    xover_risk_reward: float = 2.0
    # Higher-timeframe (15m) trend filter: long only when fastHTF>slowHTF. On by default.
    xover_use_htf_filter: bool = True
    xover_htf_interval: str = "15m"
    xover_htf_fast: int = 20
    xover_htf_slow: int = 50
    # Universe filters (eligibility): price floor, liquidity, in-play, volatility.
    xover_min_price: float = 10.0
    xover_min_avg_volume: int = 1_000_000
    xover_min_rel_volume: float = 2.0
    xover_min_atr_pct: float = 0.02
    # RTH session gate (ET). Entries only within [start, cutoff); any open position
    # is force-flat at/after close (no overnight/extended-hours holds). EMAs still
    # compute over all bars — only entries/exits are time-gated (parity-preserving).
    xover_use_rth_filter: bool = True
    xover_session_start_et: str = "09:30"
    xover_session_cutoff_et: str = "15:55"
    xover_session_close_et: str = "16:00"
    # Risk engine (multi-position). Sizing is risk-fractional: shares =
    # floor(equity × risk_per_trade_pct / per-share risk), then capped by the
    # remaining buying-power pool (allocation − Σ open notionals). The portfolio
    # limits gate how many positions / how much aggregate risk / loss is allowed
    # before the worker stands down. ``equity`` = the strategy's capital_allocation.
    xover_risk_per_trade_pct: float = 0.01   # risk 1% of allocation per trade
    xover_max_positions: int = 5             # max concurrent open positions
    xover_max_portfolio_risk_pct: float = 0.05  # max aggregate open risk (5%)
    xover_max_daily_loss_pct: float = 0.03   # halt new entries after a 3% day
    xover_max_trades_per_day: int = 20       # max new entries per trading day

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
