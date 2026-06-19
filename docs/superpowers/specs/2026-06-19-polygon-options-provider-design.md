# Polygon Options Data Provider — Design Spec

Date: 2026-06-19
Status: Approved — implement.

## Goal
Stand up an options market-data layer backed by **Polygon** (chains + historical price
bars), behind a clean seam, with tests. **Foundation only** — no consumer yet (no backtest,
no UI, no live greeks). Equities data stays on **IBKR**, untouched. This routes: stocks→IBKR,
options→Polygon.

## Why a separate seam
Options need *contract discovery* + *per-contract aggregates* — a different shape than the
stock `HistoricalDataProvider` (`get_bars(symbol, tf, start, end)`). So options get their own
small `OptionsDataProvider` interface rather than bending the stock seam or resurrecting the
deleted Massive stock provider (stocks deliberately moved to IBKR).

## Architecture — new package `app/options/`

### `app/options/contracts.py` (pure)
- `occ_ticker(underlying: str, expiry: date, right: str, strike: float) -> str` →
  Polygon OCC form, e.g. `O:SPY260622C00747000` (right ∈ {"C","P"}; strike encoded as
  int(strike*1000) zero-padded to 8).
- `parse_occ(ticker: str) -> dict` → `{underlying, expiry: date, right, strike}` (inverse).

### `app/options/base.py`
- `OptionsDataProvider` ABC: `list_contracts(...)`, `get_aggregates(...)`.
- `OptionContract` dataclass: `ticker, underlying, expiration (date), strike (float),
  right ("C"/"P"), shares_per_contract (int)`.
- Reuse the canonical `Bar` shape: `{time(ms,UTC), open, high, low, close, volume, symbol}`
  (here `symbol` = the option ticker).
- Errors: `PolygonNotConfigured` (no key), `PolygonNotAuthorized` (carries Polygon's 403
  message, e.g. the ~2yr plan/lookback limit), `OptionsProviderError` (base).

### `app/options/polygon.py` — `PolygonOptionsProvider(OptionsDataProvider)`
Async `httpx` client; follows `next_url` pagination; retries 429 with backoff.
- `async list_contracts(underlying, *, expired=False, expiration_date=None,
  contract_type=None, limit=1000) -> list[OptionContract]` →
  `GET /v3/reference/options/contracts` (includes expired contracts back to 2010).
- `async get_aggregates(option_ticker, timeframe, start, end) -> list[Bar]` →
  `GET /v2/aggs/ticker/{ticker}/range/{mult}/{span}/{from}/{to}?adjusted=true&sort=asc&limit=50000`,
  normalized oldest→newest. Timeframe map: `1m→(1,minute) 5m→(5,minute) 15m→(15,minute)
  1h→(1,hour) 1d→(1,day)`. Dates accept `date`/`datetime`; formatted `YYYY-MM-DD`.
- HTTP 403 with body `status=NOT_AUTHORIZED` → raise `PolygonNotAuthorized(message)`.

### `app/options/__init__.py`
- `get_options_provider() -> OptionsDataProvider` (lru_cached) → `PolygonOptionsProvider`
  from settings; raises `PolygonNotConfigured` if no key.

## Config (`app/config.py`)
Add:
```python
polygon_api_key: str | None = Field(default=None,
    validation_alias=AliasChoices("POLYGON_API_KEY", "MASSIVE_API_KEY"))
polygon_base_url: str = "https://api.polygon.io"
```
`AliasChoices` reuses the existing `MASSIVE_API_KEY` value already in `backend/.env`, so no
.env edit is required (a `POLYGON_API_KEY` may be set later to supersede it).

## Normalization
- Aggregate JSON `{t,o,h,l,c,v}` → `{time:t, open:o, high:h, low:l, close:c, volume:v,
  symbol:ticker}` (t already ms UTC from Polygon).
- Contract JSON → `OptionContract` (map `expiration_date`→date, `strike_price`→float,
  `contract_type` "call"/"put"→"C"/"P", `shares_per_contract`).

## Testing
- **Unit (TDD, no network):** `occ_ticker` build + `parse_occ` round-trip (incl. puts and
  fractional strikes); `_normalize_bar`; `_normalize_contract`; timeframe mapping; the 403→
  `PolygonNotAuthorized` mapping (feed a fake 403 response object to the parse helper).
- **Live smoke (uses the key):** `scripts/polygon_options_smoke.py` — prints SPY chain size +
  oldest expired contract + daily aggregates for a current contract and the authorized
  lookback boundary. Agent can run this (key works); it is not a unit test.

## Out of scope (YAGNI)
Backtest consumer, dashboard/UI, live snapshot/greeks, disk caching (the existing
`history_cache` can layer on later, namespaced `source="polygon"`), options execution/risk.

## Risks
- Plan lookback: current key authorizes only ~2yr of option aggregates (older → 403
  `PolygonNotAuthorized`); that's expected and surfaced as a typed error, not a crash.
- Polygon rate limits on lower tiers (sleep/backoff on 429).

## Notes
Repo under git. Spec on disk.
