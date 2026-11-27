"""Historical SPY iron fly replay using the production runner and a local execution port."""
from bisect import bisect_right
from datetime import date, datetime, timedelta
import math
from statistics import mean
from zoneinfo import ZoneInfo

from app.services.backtest_reports import SPY_SOURCE
from app.strategies.xsp_iron_fly import calendar, logic
from app.strategies.xsp_iron_fly.feed import Contract, Quote, Spot

ET = ZoneInfo("America/New_York")
REPLAY_DATABASE = "sqlite+aiosqlite:///:memory:"
KEY = "spy_iron_fly"


def body_at(price):
    lower = math.floor(price)
    return logic.select_body_strike(price, [lower, lower + 1])


def option_symbols(day, strikes, wing_width):
    out = {}
    for body in strikes:
        for leg in logic.build_fly(body, body + wing_width, body - wing_width):
            right, strike = leg["right"], leg["strike"]
            symbol = f"SPY{day:%y%m%d}{right}{int(strike * 1000):08d}"
            out[symbol] = {"right": right, "strike": strike}
    return out


class PrintSeries:
    """Timestamped prices, in microseconds. A later observation cannot fill an earlier order."""

    def __init__(self, rows, day):
        self.times = []
        self.prices = []
        for stamp, price in rows:
            if (not isinstance(stamp, int) or datetime.fromtimestamp(stamp / 1e6, ET).date() != day
                    or not math.isfinite(price) or price <= 0):
                raise ValueError(f"Invalid historical SPY observation for {day}")
            if self.times and stamp < self.times[-1]:
                raise ValueError(f"Unsorted historical SPY observations for {day}")
            self.times.append(stamp)
            self.prices.append(price)

    def latest(self, now, max_age):
        stamp = round(now.timestamp() * 1e6)
        i = bisect_right(self.times, stamp) - 1
        if i < 0 or stamp - self.times[i] > max_age * 1e6:
            return None
        return self.prices[i], datetime.fromtimestamp(self.times[i] / 1e6, ET)


class ReplayClock:
    def __init__(self, now):
        self.t = now

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.t += timedelta(seconds=max(0.0, seconds))


class SpyPrintFeed:
    def __init__(self, record, params, clock, width):
        self.day = date.fromisoformat(record["date"])
        if (record.get("ticker"), record.get("source"), params.ticker) != ("SPY", SPY_SOURCE, "SPY"):
            raise ValueError(f"SPY replay provenance mismatch for {self.day}")
        self.spots = PrintSeries(record["spot"], self.day)
        self.clock, self.max_age, self.width = clock, params.max_quote_age, width
        expected = option_symbols(self.day, {body_at(p) for p in self.spots.prices}, params.wing_width)
        if set(expected) != set(record["contracts"]):
            raise ValueError(f"SPY contract identity or expiration mismatch for {self.day}")
        self.series = {}
        self.chain = []
        for symbol, leg in record["contracts"].items():
            if any(leg[k] != v for k, v in expected[symbol].items()):
                raise ValueError(f"Contract metadata does not match {symbol}")
            self.series[symbol] = PrintSeries(leg["prints"], self.day)
            self.chain.append(Contract(leg["right"], leg["strike"], symbol, symbol, calendar.close_time(self.day)))

    def data_ready(self):
        return True

    async def contracts(self, ticker, expiry):
        return self.chain if ticker == "SPY" and expiry == self.day else []

    async def subscribe(self, symbols):
        pass

    def spot(self, ticker):
        latest = self.spots.latest(self.clock.now(), self.max_age)
        return Spot(*latest) if ticker == "SPY" and latest else None

    def quote(self, symbol):
        series = self.series.get(symbol)
        latest = series.latest(self.clock.now(), self.max_age) if series else None
        if latest is None:
            return None
        price, stamp = latest
        return Quote(max(0.0, price - self.width / 2), price + self.width / 2, stamp)

    async def settlement_price(self, *_):
        raise AssertionError("A SPY replay cannot cash-settle options")


class BacktestPort:
    """Alpaca stand-in for the replay, behind the REAL ``AlpacaFlyExecutor``: an order
    is marketable against the replayed print quotes (combo: limit credit <= natural =
    shorts at the bid, wings at the ask; single leg: limit through the touch) and then
    fills in full at the touch; otherwise it rests until the executor cancels it.
    Never used outside backtests (it can't place anything anywhere)."""

    def __init__(self, feed) -> None:
        self.feed = feed
        self.orders: dict[str, dict] = {}
        self._n = 0

    def _touch(self, sym: str):
        q = self.feed.quote(sym)
        return None if q is None else (q.bid, q.ask)

    async def submit(self, body: dict, intent: str, qty: int) -> dict:
        self._n += 1
        oid = f"bt{self._n}"
        self.orders[oid] = {"id": oid, "status": "new", "filled_qty": "0", "body": body,
                            "legs": []}
        return {"transmitted": True, "order_id": oid, "ref": oid}

    async def find_by_ref(self, ref):
        o = self.orders.get(ref)
        return ("found", o) if o else ("missing", None)

    def _try_fill(self, o: dict) -> None:
        b = o["body"]
        if b.get("order_class") == "mleg":
            t = {x["symbol"]: self._touch(x["symbol"]) for x in b["legs"]}
            if any(v is None for v in t.values()):
                return
            nat = (sum(t[x["symbol"]][0] for x in b["legs"] if x["side"] == "sell")
                   - sum(t[x["symbol"]][1] for x in b["legs"] if x["side"] == "buy"))
            if nat + 1e-9 < -float(b["limit_price"]):
                return
            o["legs"] = [{"symbol": x["symbol"], "side": x["side"], "filled_qty": b["qty"],
                          "filled_avg_price": str(t[x["symbol"]][0 if x["side"] == "sell"
                                                                 else 1])}
                         for x in b["legs"]]
        else:
            t = self._touch(b["symbol"])
            if t is None:
                return
            bid, ask = t
            lim = float(b["limit_price"])
            if (b["side"] == "buy" and lim + 1e-9 < ask) or (b["side"] == "sell"
                                                             and (bid <= 0 or lim > bid)):
                return
            o["filled_avg_price"] = str(ask if b["side"] == "buy" else bid)
        o["filled_qty"], o["status"] = b["qty"], "filled"

    async def get_order(self, oid: str):
        o = self.orders[oid]
        if o["status"] == "new":
            self._try_fill(o)
        return {k: v for k, v in o.items() if k != "body"}

    async def cancel(self, oid: str) -> None:
        o = self.orders[oid]
        if o["status"] == "new":
            o["status"] = "canceled"

    async def positions(self, *, include_expired: bool = False) -> dict[str, int]:
        net: dict[str, int] = {}
        for o in self.orders.values():
            b = o["body"]
            if b.get("order_class") == "mleg":
                for x in o["legs"]:
                    q = int(x["filled_qty"]) * (1 if x["side"] == "buy" else -1)
                    net[x["symbol"]] = net.get(x["symbol"], 0) + q
            elif int(o["filled_qty"]):
                q = int(o["filled_qty"]) * (1 if b["side"] == "buy" else -1)
                net[b["symbol"]] = net.get(b["symbol"], 0) + q
        return {k: v for k, v in net.items() if v}

    async def open_orders(self) -> list[dict]:
        return [{"id": o["id"], "symbol": o["body"].get("symbol"),
                 "legs": o["body"].get("legs") or []}
                for o in self.orders.values() if o["status"] == "new"]


def session_row(session):
    reason = session.get("reason") or ""
    status = session["status"]
    if status == "closed" and session.get("realized_pnl") is not None:
        outcome = "closed"
    elif status == "skipped" and not session.get("legs"):
        outcome = "skipped"
    else:
        outcome = "unresolved"
        if "close unconfirmed at expiry" in reason:
            reason = "The replay could not confirm every closing fill from SPY option prints before expiry."
    return {"date": session["session_date"].isoformat(), "status": outcome, "reason": reason,
            "data_gap": outcome == "skipped" and any(
                clue in reason.lower() for clue in ("quote", "spot", "no 0dte", "data not ready")),
            "contracts": max((leg.get("qty", 0) for leg in session.get("legs", [])), default=0),
            "body": session.get("body_strike"),
            "stops": [leg["role"] for leg in session.get("legs", []) if leg.get("exit_reason") == "stop"],
            "pnl": session.get("realized_pnl") if outcome == "closed" else None,
            "fees": session.get("fees") or 0.0}


def aggregate(rows):
    completed = [row for row in rows if row["status"] == "closed"]
    profits = [row["pnl"] for row in completed]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    equity = peak = drawdown = 0.0
    for pnl in profits:
        equity += pnl
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    skips = [r for r in rows if r["status"] == "skipped"]
    return {"sessions": len(rows), "traded": len(profits),
            "unresolved": sum(r["status"] == "unresolved" for r in rows),
            "rule_skips": {r["date"]: r["reason"] for r in skips if not r["data_gap"]},
            "data_gap_skips": {r["date"]: r["reason"] for r in skips if r["data_gap"]},
            "total_pnl": round(equity, 2) if profits else None,
            "max_drawdown": round(drawdown, 2) if profits else None,
            "fees": round(sum(r["fees"] for r in completed), 2) if profits else None,
            "win_rate": round(len(wins) / len(profits), 4) if profits else None,
            "mean_pnl": round(mean(profits), 2) if profits else None,
            "avg_win": round(mean(wins), 2) if wins else None, "avg_loss": round(mean(losses), 2) if losses else None,
            "best_day": max(profits, default=None), "worst_day": min(profits, default=None)}


async def replay(records, params):
    from sqlalchemy import delete
    from app.db.base import engine, init_db, session_scope
    from app.db.models import SystemFlag, TradeRecord, XspFlySession
    from app.strategies.xsp_iron_fly.alpaca_executor import AlpacaFlyExecutor
    from app.strategies.xsp_iron_fly.runner import IronFlyRunner, _params_dict

    if str(engine.url) != REPLAY_DATABASE:
        raise RuntimeError("Run the replay CLI with its private in-memory database")
    if (params.ticker != "SPY" or not params.close_before_settlement
            or params.flatten_minutes_before_close != 30 or params.validate()):
        raise ValueError("A SPY replay requires a valid SPY configuration and a 30-minute pre-close exit")
    await init_db()

    async def starting_equity():
        return 5000.0

    async def no_safe_mode():
        return False

    scenarios = {}
    for width in (0.05, 0.10):
        rows = []
        for record in records:
            # Each historic day starts flat with independent sizing and risk flags.
            # All three tables belong to this CLI process's private in-memory engine.
            async with session_scope() as db:
                for table in (TradeRecord, XspFlySession, SystemFlag):
                    await db.execute(delete(table))
            day = date.fromisoformat(record["date"])
            clock = ReplayClock(datetime.combine(day, params.entry_time, ET))
            feed = SpyPrintFeed(record, params, clock, width)
            executor = AlpacaFlyExecutor(BacktestPort(feed), feed, clock)
            runner = IronFlyRunner(KEY, params, feed, executor, clock,
                                   equity_fn=starting_equity, safe_mode_fn=no_safe_mode)
            session = await runner.run_session(day)
            if session is None:
                raise ValueError(f"No scheduled SPY session on {day}")
            rows.append(session_row(session))
        scenarios[str(width)] = {"aggregate": aggregate(rows), "sessions": rows}
    await engine.dispose()
    return {"basis": "SPY SIP entry spot and timestamped SPY 0DTE option trades; modeled bid/ask width, "
                     "marketable-limit fills and $5,000 independent daily sizing. Closing orders start "
                     "30 minutes before the equity close. Unresolved positions have no final P&L.",
            "bars_minutes": 5, "days": [r["date"] for r in records],
            "params": _params_dict(params) | {"ticker": "SPY", "replay_source": SPY_SOURCE,
                                              "replay_daily_capital": 5000},
            "by_width": scenarios}
