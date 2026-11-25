"""XSP 0DTE iron fly runner: one session per trading day.

    09:45 ET  guards -> size -> persist "entering" -> 4-leg combo (or wings-first leg-in)
    intraday  per-short-leg 1.5x stop on the buy-to-close price, legs independent,
              wings never stopped, no profit target, no adjustment
    close     remaining legs cash-settle at intrinsic vs the XSP close (optional
              flatten at close - 2 min), then end-of-day reconciliation

State lives in the ``xsp_fly_sessions`` row (one per day, unique) and in one
TradeRecord per filled leg, saved after every change, so a restart never re-enters a
day and resumes open legs with their stop levels. Clock, feed and executor are
injected, so the whole day runs in tests on simulated time.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.strategies.xsp_iron_fly import calendar as cal
from app.strategies.xsp_iron_fly import logic as L
from app.strategies.xsp_iron_fly import store
from app.strategies.xsp_iron_fly.params import IronFlyParams

logger = logging.getLogger("trading.xsp_fly")
ET = ZoneInfo("America/New_York")

OPEN_STATES = ("open", "stopping")
TERMINAL_LEG = ("stopped", "settled", "closed", "abandoned")


class RealClock:
    def now(self) -> datetime:
        return datetime.now(ET)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class IronFlyRunner:
    def __init__(self, key: str, params: IronFlyParams, feed, executor, clock, *,
                 equity_fn, safe_mode_fn, alert_fn=None) -> None:
        self.key = key
        self.p = params
        self.feed = feed
        self.ex = executor
        self.clock = clock
        self.equity_fn = equity_fn          # starting equity + realized P&L
        self.safe_mode_fn = safe_mode_fn
        self.alert_fn = alert_fn
        self._contracts: dict[date, list] = {}
        # (session_date, role) -> clock time a short leg was first seen past its stop on
        # a usable quote; the stop fires only if it is still past on a check >= the
        # confirm window later (a one-tick stub/spike never fires).
        self._stop_seen: dict[tuple, datetime] = {}
        self._reject_alerted: set = set()
        self._last_miss = ""          # why the latest entry order did not fill

    # ------------------------------------------------------------------ utilities
    async def _log(self, msg: str, category: str = "decision", level: str = "info",
                   data: dict | None = None) -> None:
        getattr(logger, "warning" if level == "warning" else
                "error" if level == "error" else "info")("[%s] %s", self.key, msg)
        from app.services.journal import log_event
        await log_event(msg, category=category, level=level, strategy_key=self.key,
                        symbol=self.p.ticker, data=data)

    async def _alert(self, kind: str, msg: str, data: dict | None = None) -> None:
        if self.alert_fn is not None:
            try:
                await self.alert_fn(kind, msg, data)
            except Exception:  # noqa: BLE001
                logger.exception("alert failed")

    async def _sleep_until(self, dt: datetime) -> None:
        while (rem := (dt - self.clock.now()).total_seconds()) > 0:
            await self.clock.sleep(min(rem, 300.0))

    def _legs_open(self, s: dict) -> list[dict]:
        return [l for l in s["legs"] if l.get("status") in OPEN_STATES
                and int(l.get("qty") or 0) > int(l.get("exit_qty") or 0)]

    def _record_fill(self, s: dict, leg: dict, side: str, qty: int, price: float,
                     mid: float | None, kind: str, fee: bool = True,
                     order_id: str = "") -> None:
        slip = None
        if mid is not None:
            slip = round((price - mid) if side == "BUY" else (mid - price), 4)
        s["fills"].append({"ts": _iso(self.clock.now()), "role": leg["role"],
                           "symbol": leg["symbol"], "side": side, "qty": qty,
                           "price": price, "mid": mid, "slippage_vs_mid": slip,
                           "kind": kind, "fee": self.p.fees_per_contract * qty if fee else 0.0,
                           "order_id": order_id or None})
        if fee:
            s["fees"] = round(float(s.get("fees") or 0) + self.p.fees_per_contract * qty, 4)

    def _add_exit(self, leg: dict, qty: int, price: float, by_order: bool) -> None:
        prev_q = int(leg.get("exit_qty") or 0)
        prev_px = float(leg.get("exit_price") or 0.0)
        leg["exit_qty"] = prev_q + qty
        leg["exit_price"] = (prev_px * prev_q + price * qty) / leg["exit_qty"]
        if by_order:
            leg["order_exit_qty"] = int(leg.get("order_exit_qty") or 0) + qty

    async def _finish_leg(self, s: dict, leg: dict, status: str, reason: str) -> None:
        now = self.clock.now()
        qty = int(leg["qty"])
        gross = L.leg_gross_pnl(leg["action"], leg["entry_price"], leg["exit_price"], qty,
                                self.p.multiplier)
        fees = self.p.fees_per_contract * (qty + int(leg.get("order_exit_qty") or 0))
        leg.update(status=status, exit_reason=reason, exit_time=_iso(now),
                   fees=round(fees, 4), pnl=round(gross - fees, 2))
        await store.close_leg_trade(leg.get("trade_id"), leg["exit_price"], leg["pnl"], now)

    # ------------------------------------------------------------------ main loop
    async def run_forever(self) -> None:
        errs = self.p.validate()
        if errs:
            await self._log(f"invalid parameters, not running: {errs}", level="error")
            return
        await self._log(f"XSP iron fly worker started (mode={self.p.mode}, executor="
                        f"{self.ex.name}; real Alpaca paper orders and fills)",
                        category="action", data={"params": _params_dict(self.p)})
        try:
            while True:
                d = self.clock.now().date()
                try:
                    await self.recover_stale(d)
                    await self.run_session(d)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep the worker alive
                    logger.exception("XSP fly session error")
                    await self._log(f"session error: {exc!r}", level="error")
                    await self._alert("xsp_fly_error", f"XSP fly session error: {exc!r}")
                    await self.clock.sleep(30.0)
                    continue
                await self._sleep_until(datetime.combine(d + timedelta(days=1), time(8, 0),
                                                         tzinfo=ET))
        except asyncio.CancelledError:
            try:
                n = await asyncio.wait_for(self.ex.cancel_all(), 10.0)
            except BaseException:  # noqa: BLE001
                n = "unknown"
            await self._log(f"worker stopped; working orders cancelled ({n}). Open legs "
                            "stay recorded and resume on the next start.",
                            category="action")
            raise

    async def run_session(self, d: date) -> dict | None:
        reason = cal.skip_reason(d)
        if reason:
            if reason.startswith("exchange calendar"):
                await self._log(f"{d}: skipped, {reason}", level="warning")
            return None
        close_dt = cal.close_time(d)
        s = await store.get_session(self.key, d)
        if s is not None:
            s = await self._recover_today(s)
        else:
            now = self.clock.now()
            if now >= close_dt:
                return None
            entry_dt = datetime.combine(d, self.p.entry_time, tzinfo=ET)
            if now < entry_dt:
                await self._sleep_until(entry_dt)
            now = self.clock.now()
            if now > entry_dt + timedelta(seconds=self.p.entry_grace_seconds):
                s = await store.create_session(
                    self.key, d, status="missed", mode=self.p.mode,
                    reason=f"worker started at {now:%H:%M:%S} ET, after the "
                           f"{self.p.entry_time:%H:%M} entry window")
                if s is not None:
                    await self._log(f"{d}: entry missed ({s['reason']})")
                return s
            s = await self._enter(d, close_dt)
        if s is None or s["status"] != "open":
            return s
        await self._manage(s)
        return s

    # ------------------------------------------------------------------ recovery
    def _expected_broker(self, s: dict) -> dict[str, int]:
        return store.expected_broker(s)

    async def _broker_check_on_restart(self, s: dict) -> bool:
        """Real-order executors only: cancel anything a crash left working on today's
        symbols, then require Alpaca's app-owned fills to match the session's. A
        mismatch (e.g. a fill that landed after the crash) trips SAFE_MODE. True = ok."""
        if not hasattr(self.ex, "check_positions") or s.get("mode") != "alpaca_paper":
            return True
        n, problem = await self.ex.cancel_working_for(s.get("legs") or [])
        if problem:
            await self._fatal(s, f"restart: {problem}")
            return False
        if n:
            await self._log(f"restart: cancelled {n} working Alpaca order(s) on today's legs",
                            level="warning")
        issues = await self.ex.check_positions(self._expected_broker(s))
        if issues:
            await self._fatal(s, "restart: Alpaca positions differ from the session: "
                                 + "; ".join(issues))
            return False
        return True

    async def _recover_today(self, s: dict) -> dict:
        if s["status"] in ("entering", "open") and (s.get("mode") or "sim") != self.p.mode:
            # Never manage a fly with a different executor than the one that opened it
            # (a sim fly must not send closes to Alpaca, an Alpaca fly must not be
            # "closed" by simulated fills). Leave it for a human; nothing is changed.
            msg = (f"{s['session_date']}: open session was opened in mode "
                   f"{s.get('mode') or 'sim'} but the strategy now runs in {self.p.mode}; "
                   "not managing it. Switch the mode back or close it by hand.")
            await self._log(msg, level="error")
            await self._alert("xsp_fly_mode_mismatch", msg)
            return {**s, "status": "mode_mismatch"}
        if s["status"] == "entering":
            if not await self._broker_check_on_restart(s):
                return s
            return await self._resume_entry(s)
        if s["status"] != "open":
            return s
        await self._detect_killed(s)
        if s["status"] == "open" and not await self._broker_check_on_restart(s):
            return s
        if s["status"] == "open":
            await self._log(f"{s['session_date']}: resuming open fly x{s['contracts']} "
                            f"(stops {[(l['role'], l.get('stop_level')) for l in s['legs'] if l['action'] == 'SELL']})",
                            category="action")
        return s

    async def _detect_killed(self, s: dict) -> None:
        """Legs whose TradeRecord was closed outside the runner (the kill switch's
        close_open_trades) are done for the day, exit price unknown."""
        ids = [l["trade_id"] for l in s["legs"] if l.get("trade_id")]
        states = await store.trade_states(ids)
        hit = [l for l in self._legs_open(s)
               if states.get(l.get("trade_id"), {}).get("status") == "closed"]
        if not hit:
            return
        for l in hit:
            l.update(status="abandoned", exit_reason="kill_switch",
                     exit_time=_iso(self.clock.now()))
        if not self._legs_open(s):
            s["status"] = "killed"
            s["reason"] = "legs closed by the kill switch; exit prices unknown"
            s["closed_time"] = self.clock.now()
        await store.save(s)
        await self._log(f"{s['session_date']}: {len(hit)} leg(s) closed by the kill switch; "
                        "not managed further today", level="warning")

    async def _resume_entry(self, s: dict) -> dict:
        """A restart found the day mid-entry. Every fill was saved as it happened (and the
        restart check has already confirmed Alpaca agrees), so filled legs are positions. Before the
        close: unwind whatever does not pair up (excess body first) and manage the
        matched fly. At or after the close: open the filled legs as they are so they
        settle at the close print. Never re-enters."""
        d = s["session_date"]
        legs = s.get("legs") or []
        if not any(int(l.get("qty") or 0) for l in legs):
            s.update(status="failed", closed_time=self.clock.now(),
                     reason="interrupted during entry before any fill (any working order was "
                            "cancelled on restart); not re-entering today")
            await store.save(s)
            await self._log(f"{d}: {s['reason']}", level="warning")
            return s
        close = s.get("close_time") or cal.close_time(d)
        if self.clock.now() < close:
            f = min(int(l.get("qty") or 0) for l in legs)
            excess = [l for l in legs if int(l.get("qty") or 0) > f]
            if excess:
                await self._await_quotes(excess)
            for role in L.SHORT_ROLES + L.LONG_ROLES:
                leg = self._leg(s, role)
                await self._unwind(s, leg, int(leg.get("qty") or 0) - f)
            if s["status"] == "error":      # an unwind failed: SAFE_MODE is already tripped
                return s
            if f == 0:
                s.update(status="failed", contracts=0, closed_time=self.clock.now(),
                         realized_pnl=round(float(s["extra_pnl"]) - float(s["fees"]), 2),
                         reason="interrupted during entry; unmatched fills unwound, no fly "
                                "opened; not re-entering today")
                await store.save(s)
                await self._log(f"{d}: {s['reason']}", level="warning")
                return s
        await self._activate(s)
        await self._log(
            f"{d}: resumed an interrupted entry: opened "
            f"{[(l['role'], l['qty']) for l in legs if int(l.get('qty') or 0)]}"
            + ("" if self.clock.now() < close else "; settling at the close print"),
            level="warning", category="action")
        return s

    async def _await_quotes(self, legs: list[dict], timeout: float = 120.0) -> None:
        """After a restart nothing is subscribed yet and the quote cache is empty:
        subscribe the legs and give the feed time to deliver fresh quotes before any
        close order is sized off them."""
        await self.feed.subscribe([l["symbol"] for l in legs])
        deadline = self.clock.now() + timedelta(seconds=timeout)
        while self.clock.now() < deadline:
            now = self.clock.now()
            qs = [self.feed.quote(l["symbol"]) for l in legs]
            if self.feed.data_ready() and all(
                    q is not None and (q.age(now) or 1e9) <= self.p.feed_stale_alert_seconds
                    for q in qs):
                return
            await self.clock.sleep(1.0)
        await self._log("quotes for the legs to unwind did not arrive within "
                        f"{timeout:.0f}s; unwinding anyway", level="warning")

    async def recover_stale(self, today: date) -> None:
        for s in await store.unfinished_before(self.key, today):
            if s["status"] == "error":
                # A day stopped by SAFE_MODE: book its settlement only once Alpaca's
                # fills agree with the session (a human may have fixed it); else wait.
                if not hasattr(self.ex, "check_positions") or s.get("mode") != "alpaca_paper":
                    continue
                if await self.safe_mode_fn():
                    continue                   # a human hasn't cleared SAFE_MODE yet
                legs_q = [l for l in s.get("legs") or [] if int(l.get("qty") or 0) > 0]
                if not legs_q or any(l.get("status") in ("pending", None) for l in legs_q):
                    # The error hit during entry: the fly was never activated (no stops,
                    # no TradeRecords), so settling it automatically would book wrong
                    # P&L. Leave it to a human.
                    await self._log(f"{s['session_date']}: error during entry; not booked "
                                    "automatically, needs a human", level="warning")
                    continue
                issues = await self.ex.check_positions(self._expected_broker(s))
                if issues:
                    await self._log(f"{s['session_date']}: error session still differs from "
                                    f"Alpaca ({'; '.join(issues)}); needs a human",
                                    level="warning")
                    continue
                s["reason"] = f"resolved after error ({s.get('reason') or ''})"
                s["status"] = "open"
            if s["status"] == "entering":
                await self._resume_entry(s)
            else:
                await self._detect_killed(s)
            if s["status"] != "open":
                continue
            close_dt = s.get("close_time") or cal.close_time(s["session_date"])
            px = await self.feed.settlement_price(self.p.ticker, s["session_date"],
                                                  close_dt, self.clock.now())
            if px is None:
                await self._log(f"{s['session_date']}: open fly still unsettled; "
                                "settlement price unavailable, will retry", level="warning")
                continue
            await self._apply_settlement(s, px)

    # ------------------------------------------------------------------ entry
    async def _plan(self, d: date, close_dt: datetime) -> tuple[dict | None, str, bool]:
        """(plan, why_not, permanent). Permanent reasons skip the day immediately;
        the rest are re-checked until the entry grace window ends."""
        p = self.p
        now = self.clock.now()
        halt = await store.halt_reason()
        if halt:
            return None, f"halted until cleared: {halt}", True
        if await self.safe_mode_fn():
            return None, "engine SAFE_MODE is on", False
        if not self.feed.data_ready():
            return None, "market data not ready", False
        contracts = self._contracts.get(d)
        if contracts is None:
            contracts = await self.feed.contracts(p.ticker, d)
            if contracts is None:
                return None, "0DTE option chain lookup failed", False
            self._contracts[d] = contracts
        if not contracts:
            return None, f"no 0DTE {p.ticker} expiry listed for {d}", True
        stops = [c.stops_trading_at for c in contracts if c.stops_trading_at]
        close_eff = min([close_dt] + [st.astimezone(ET) for st in stops])
        mins_left = (close_eff - now).total_seconds() / 60
        if mins_left < p.min_minutes_to_close:
            return None, (f"only {mins_left:.0f} min to the {close_eff:%H:%M} ET close "
                          f"(< min_minutes_to_close {p.min_minutes_to_close:.0f}; early "
                          f"close)"), True
        await self.feed.subscribe([p.ticker])
        spot = self.feed.spot(p.ticker)
        if spot is None:
            return None, f"no {p.ticker} spot", False
        age = spot.age(now)
        if age is None or age > p.max_quote_age:
            return None, f"{p.ticker} spot stale (age {age}s > {p.max_quote_age}s)", False
        by_key = {(c.right, c.strike): c for c in contracts}
        calls = sorted(k for (r, k) in by_key if r == "C")
        puts = sorted(k for (r, k) in by_key if r == "P")
        both = sorted(set(calls) & set(puts))
        body = L.select_body_strike(spot.price, both)
        wings = L.select_wings(body, p.wing_width, calls, puts) if body else None
        if body is None or wings is None:
            return None, "no listed strikes for the body/wings", True
        legs = L.build_fly(body, *wings)
        for leg in legs:
            c = by_key[(leg["right"], leg["strike"])]
            leg.update(symbol=c.symbol, occ=c.occ)
        await self.feed.subscribe([l["symbol"] for l in legs])
        quotes = {}
        for leg in legs:
            q = self.feed.quote(leg["symbol"])
            if q is None or not L.quote_valid(q.bid, q.ask):
                return None, f"no valid quote for {leg['role']} {leg['symbol']}", False
            if q.ask - q.bid > p.max_leg_spread + L.EPS:
                return None, (f"{leg['role']} spread {q.ask - q.bid:.2f} > max_leg_spread "
                              f"{p.max_leg_spread:.2f}"), False
            quotes[leg["role"]] = (q.bid, q.ask)
        mid_c, nat_c = L.combo_credits(quotes)
        cw, pw = L.wing_widths(body, *wings)
        if mid_c <= 0 or mid_c >= max(cw, pw):
            return None, f"implausible mid credit {mid_c:.2f} for {cw:g}/{pw:g} wings", False
        max_loss_est = L.max_loss_per_fly(cw, pw, nat_c, p.multiplier)
        equity = float(await self.equity_fn() or 0.0)
        sz = L.size_flies(equity, max_loss_est, capital_per_fly=p.capital_per_fly,
                          max_daily_loss_per_contract=p.max_daily_loss_per_contract)
        if sz.quantity == 0:
            return None, f"sizing: {sz.reason}", True
        return {"spot": spot.price, "spot_age": age, "body": body, "wings": wings,
                "widths": (cw, pw), "legs": legs, "quotes": quotes, "mid_credit": mid_c,
                "natural_credit": nat_c, "max_loss_est": max_loss_est,
                "qty": sz.quantity, "sizing": sz.__dict__, "equity": equity,
                "close": close_eff}, "", False

    async def _enter(self, d: date, close_dt: datetime) -> dict | None:
        entry_dt = datetime.combine(d, self.p.entry_time, tzinfo=ET)
        deadline = entry_dt + timedelta(seconds=self.p.entry_grace_seconds)
        last = None
        while True:
            plan, why, permanent = await self._plan(d, close_dt)
            if plan:
                break
            if permanent or self.clock.now() >= deadline:
                if not permanent:
                    why = f"entry window closed: {why}"
                s = await store.create_session(self.key, d, status="skipped", reason=why,
                                               mode=self.p.mode)
                await self._log(f"{d}: entry skipped ({why})")
                return s
            if why != last:
                await self._log(f"{d}: entry waiting ({why})")
                last = why
            await self.clock.sleep(1.0)

        self._last_miss = ""
        legs = [dict(l, qty=0, exit_qty=0, status="pending") for l in plan["legs"]]
        s = await store.create_session(
            self.key, d, status="entering", mode=self.p.mode, ticker=self.p.ticker,
            expiry=d, close_time=plan["close"], spot_at_entry=plan["spot"],
            body_strike=plan["body"], contracts=plan["qty"], mid_credit=plan["mid_credit"],
            legs=legs, fills=[], extra_pnl=0.0, fees=0.0)
        if s is None:   # another writer created today's row: never double-enter
            return await store.get_session(self.key, d)
        await self._log(
            f"{d}: ENTRY signal: spot {plan['spot']:.2f} -> body {plan['body']:g}, wings "
            f"{plan['wings'][0]:g}C/{plan['wings'][1]:g}P, x{plan['qty']}, mid credit "
            f"{plan['mid_credit']:.2f} (natural {plan['natural_credit']:.2f})",
            category="signal",
            data={k: v for k, v in plan.items() if k not in ("legs", "close")}
            | {"legs": [(l["role"], l["strike"], l["symbol"]) for l in legs]})

        if self.ex.supports_combo:
            await self._enter_combo(s, plan)
        else:
            await self._enter_legged(s, plan)

        if s["status"] == "error":      # an unwind failed: SAFE_MODE is already tripped
            return s
        qty = min(int(l["qty"]) for l in s["legs"])
        if qty == 0:
            s["status"] = "skipped" if s["status"] == "entering" else s["status"]
            s["reason"] = s.get("reason") or "entry not filled within max_entry_concession"
            if self._last_miss:
                s["reason"] += f" (last attempt: {self._last_miss})"
            s["contracts"] = 0
            s["closed_time"] = self.clock.now()
            s["realized_pnl"] = round(float(s["extra_pnl"]) - float(s["fees"]), 2)
            await store.save(s)
            await self._log(f"{d}: no fly opened ({s['reason']})", level="warning")
            return s

        await self._activate(s)
        net = s["net_credit"]
        await self._log(
            f"{d}: ENTERED x{qty}: net credit {net:.2f} (mid {plan['mid_credit']:.2f}, "
            f"slippage {plan['mid_credit'] - net:+.2f}); max loss ${s['max_loss']:.0f}; "
            f"stops C {s['legs'][0]['stop_level']:.2f} / P {s['legs'][1]['stop_level']:.2f}",
            category="trade",
            data={"net_credit": net, "mid_credit": plan["mid_credit"],
                  "slippage_vs_mid": round(plan["mid_credit"] - net, 4),
                  "max_loss": s["max_loss"], "legs": s["legs"]})
        return s

    async def _activate(self, s: dict) -> None:
        """Open every filled leg: a stop level per short, one open TradeRecord per leg,
        session 'open'. A balanced 4-leg fly also gets its net credit and max loss."""
        now = self.clock.now()
        filled = [l for l in s["legs"] if int(l.get("qty") or 0) > 0]
        for l in filled:
            l["status"] = "open"
            if l["action"] == "SELL":
                l["stop_level"] = round(L.stop_level(l["entry_price"], self.p.stop_mult), 4)
        qtys = {int(l["qty"]) for l in filled}
        s.update(status="open", contracts=min(qtys), entry_time=now)
        if len(filled) == len(s["legs"]) and len(qtys) == 1:
            net = L.net_credit({l["role"]: l["entry_price"] for l in filled})
            cw, pw = L.wing_widths(s["body_strike"], self._leg(s, "long_call")["strike"],
                                   self._leg(s, "long_put")["strike"])
            s.update(net_credit=round(net, 4), max_loss=round(
                L.max_loss_per_fly(cw, pw, net, self.p.multiplier) * min(qtys), 2))
        await store.finalize_entry(s, self.key, now)

    def _leg(self, s: dict, role: str) -> dict:
        return next(l for l in s["legs"] if l["role"] == role)

    def _add_entry(self, leg: dict, qty: int, price: float, mid: float | None) -> None:
        q0 = int(leg.get("qty") or 0)
        leg["entry_price"] = ((float(leg.get("entry_price") or 0) * q0 + price * qty)
                              / (q0 + qty))
        if mid is not None:
            leg["mid_at_entry"] = ((float(leg.get("mid_at_entry") or mid) * q0 + mid * qty)
                                   / (q0 + qty))
        leg["qty"] = q0 + qty

    def _quotes_for(self, legs: list[dict]) -> dict | None:
        out = {}
        for l in legs:
            q = self.feed.quote(l["symbol"])
            if q is None or not L.quote_valid(q.bid, q.ask):
                return None
            out[l["role"]] = (q.bid, q.ask)
        return out

    async def _enter_combo(self, s: dict, plan: dict) -> None:
        p = self.p
        target, filled, k = plan["qty"], 0, 0
        while filled < target and k * p.entry_reprice_step <= p.max_entry_concession + L.EPS:
            quotes = self._quotes_for(s["legs"])
            if quotes is not None:
                mid_c, _ = L.combo_credits(quotes)
                limit = L.round_to_tick(mid_c - k * p.entry_reprice_step, p.tick, "down")
                res = await self.ex.combo(s["legs"], target - filled, limit,
                                          max_age=p.max_quote_age)
                if res.status == "unknown":     # a real order may have filled unseen
                    await self._fatal(s, f"entry order state unknown: {res.detail}")
                    break
                if res.status == "rejected":
                    s["reason"] = f"combo rejected: {res.detail}"
                    break
                if not res.filled_qty:
                    self._last_miss = res.detail
                if res.filled_qty:
                    for l in s["legs"]:
                        px, m = res.prices[l["role"]], res.mids.get(l["role"])
                        self._add_entry(l, res.filled_qty, px, m)
                        self._record_fill(s, l, l["action"], res.filled_qty, px, m, "entry",
                                          order_id=res.order_id)
                    filled += res.filled_qty
                    await store.save(s)
                    await self._log(f"combo fill {res.filled_qty}/{target} at limit credit "
                                    f"{limit:.2f} ({res.detail})", category="trade",
                                    data={"limit": limit, "mid_credit": mid_c,
                                          "prices": res.prices})
            if filled < target:
                k += 1
                await self.clock.sleep(p.entry_reprice_seconds)
        await self.ex.cancel_all()
        if 0 < filled < target:
            await self._log(f"combo partially filled {filled}/{target}; remainder cancelled, "
                            f"trading {filled}", level="warning")

    async def _fill_single(self, s: dict, leg: dict, side: str, qty: int,
                           attempts: int) -> int:
        """Marketable-limit entry for one leg, repriced from the live touch each try."""
        got = 0
        for _ in range(max(1, attempts)):
            q = self.feed.quote(leg["symbol"])
            if q is not None and L.quote_valid(q.bid, q.ask):
                lim = L.marketable_limit(side, q.bid, q.ask, self.p.limit_through, self.p.tick)
                if lim is not None:
                    res = await self.ex.single(leg, side, qty - got, lim,
                                               max_age=self.p.max_quote_age)
                    if res.status == "unknown":
                        await self._fatal(s, f"leg-in order state unknown: {res.detail}")
                        break
                    if not res.filled_qty:
                        self._last_miss = res.detail
                    if res.filled_qty:
                        px, m = res.prices[leg["role"]], res.mids.get(leg["role"])
                        self._add_entry(leg, res.filled_qty, px, m)
                        self._record_fill(s, leg, side, res.filled_qty, px, m, "entry",
                                          order_id=res.order_id)
                        got += res.filled_qty
                        await store.save(s)     # a restart must see every fill
            if got >= qty:
                break
            await self.clock.sleep(self.p.entry_reprice_seconds)
        await self.ex.cancel_all()
        return got

    async def _unwind(self, s: dict, leg: dict, qty: int) -> None:
        """Close ``qty`` of an entry leg that has no matching partner. Moves the qty out
        of the leg into extra_pnl so only balanced flies remain."""
        if qty <= 0:
            return
        entry = leg["entry_price"]
        tmp = dict(leg, qty=qty, exit_qty=0, exit_price=None)
        until = self.clock.now() + timedelta(seconds=60)
        got, px, no_bid = await self._close_qty(s, tmp, qty, "unwind", until)
        if got:
            s["extra_pnl"] = round(float(s["extra_pnl"]) + L.leg_gross_pnl(
                leg["action"], entry, px, got, self.p.multiplier), 2)
        leg["qty"] = int(leg["qty"]) - qty
        short = qty - got
        if short and leg["action"] == "BUY" and no_bid:
            # A long wing nobody bids for: worthless, harmless (defined risk); book it at 0.
            s["extra_pnl"] = round(float(s["extra_pnl"]) + L.leg_gross_pnl(
                "BUY", entry, 0.0, short, self.p.multiplier), 2)
            self._record_fill(s, tmp, "SELL", short, 0.0, None, "abandon", fee=False)
        elif short:
            await self._fatal(s, f"could not unwind {short}x {leg['role']} {leg['symbol']}")
        await store.save(s)

    async def _enter_legged(self, s: dict, plan: dict) -> None:
        """Defensive leg-in: BOTH wings first, then the body, never more body than wings.
        Anything that does not pair up is unwound (excess body before excess wings)."""
        attempts = 1 + int(self.p.max_entry_concession / self.p.entry_reprice_step)
        q = plan["qty"]
        lc, lp = self._leg(s, "long_call"), self._leg(s, "long_put")
        sc, sp = self._leg(s, "short_call"), self._leg(s, "short_put")
        await self._fill_single(s, lc, "BUY", q, attempts)
        await self._fill_single(s, lp, "BUY", q, attempts)
        w = min(lc["qty"], lp["qty"])
        await self._unwind(s, lc, lc["qty"] - w)
        await self._unwind(s, lp, lp["qty"] - w)
        if w == 0:
            s["reason"] = "wings did not fill; no body sold"
            return
        await self._fill_single(s, sc, "SELL", w, attempts)
        await self._fill_single(s, sp, "SELL", w, attempts)
        f = min(sc["qty"], sp["qty"])
        await self._unwind(s, sc, sc["qty"] - f)      # excess short body first
        await self._unwind(s, sp, sp["qty"] - f)
        await self._unwind(s, lc, lc["qty"] - f)      # then excess protection
        await self._unwind(s, lp, lp["qty"] - f)
        if f == 0:
            s["reason"] = "body did not fill; wings unwound"
        await store.save(s)

    async def _fatal(self, s: dict, msg: str) -> None:
        from app.engine.safe_mode import trip_safe_mode
        s["status"] = "error"
        s["reason"] = msg
        await store.save(s)
        await trip_safe_mode(f"XSP fly: {msg}")
        cleanup = ""
        if hasattr(self.ex, "cancel_working_for"):
            # Best effort: an order behind an "unknown" result has no id, so cancel by
            # symbol whatever app order still works on the day's legs.
            try:
                n, problem = await self.ex.cancel_working_for(s.get("legs") or [])
                cleanup = (f"; working orders on the legs: cancelled {n}"
                           + (f", PROBLEM: {problem}" if problem else ""))
            except Exception as exc:  # noqa: BLE001 - never mask the original error
                cleanup = f"; cancel of working orders failed: {exc!r}"
        await self._log(f"FATAL: {msg}; SAFE_MODE tripped{cleanup}", level="error")
        await self._alert("safe_mode", f"XSP fly: {msg}{cleanup}")

    # ------------------------------------------------------------------ exits
    async def _close_qty(self, s: dict, leg: dict, qty: int, kind: str,
                         until: datetime) -> tuple[int, float, bool]:
        """Marketable-limit close of ``qty`` (BUY back a short / SELL a long), repriced
        from the live touch until filled or ``until`` (``until=now`` = one attempt).
        A quote older than feed_stale_alert_seconds does not fill. Returns (filled,
        avg, no_bid)."""
        side = "BUY" if leg["action"] == "SELL" else "SELL"
        got, notional, no_bid = 0, 0.0, False
        while got < qty:
            q = self.feed.quote(leg["symbol"])
            if q is not None and L.quote_valid(q.bid, q.ask):
                if kind == "stop" and q.bid > 0:
                    # capped near the mid, stepping up per unfilled attempt: never pay a
                    # stub offer outright (2026-11-25: a 9.99 ask -> 10.05 limit)
                    lim = L.stop_close_limit(q.bid, q.ask, through=self.p.limit_through,
                                             tick=self.p.tick,
                                             attempt=int(leg.get("stop_attempts") or 0),
                                             cap_abs=self.p.stop_close_cap_abs,
                                             cap_pct=self.p.stop_close_cap_pct,
                                             max_spread_abs=self.p.stop_max_spread_abs,
                                             max_spread_pct=self.p.stop_max_spread_pct)
                else:
                    lim = L.marketable_limit(side, q.bid, q.ask, self.p.limit_through,
                                             self.p.tick)
                if lim is None:
                    no_bid = True
                    break
                res = await self.ex.single(leg, side, qty - got, lim,
                                           max_age=self.p.feed_stale_alert_seconds)
                if res.status == "unknown":     # a real order may have filled unseen
                    await self._fatal(s, f"{kind} close for {leg['role']} state unknown: "
                                         f"{res.detail}")
                    break
                if res.status == "rejected":
                    await self._log(f"{kind} close rejected for {leg['role']}: {res.detail}",
                                    level="error")
                    key = (s.get("session_date"), leg["role"], kind)
                    if key not in self._reject_alerted:     # an unclosed short: tell a human
                        self._reject_alerted.add(key)
                        await self._alert("xsp_fly_close_rejected",
                                          f"XSP {kind} close for {leg['role']} rejected: "
                                          f"{res.detail}")
                    break
                if res.filled_qty:
                    px, m = res.prices[leg["role"]], res.mids.get(leg["role"])
                    self._record_fill(s, leg, side, res.filled_qty, px, m, kind,
                                      order_id=res.order_id)
                    got += res.filled_qty
                    notional += px * res.filled_qty
                    continue
            if self.clock.now() >= until:
                break
            await self.clock.sleep(self.p.close_retry_seconds)
        await self.ex.cancel_all()
        return got, (notional / got if got else 0.0), no_bid

    async def _stop_leg(self, s: dict, leg: dict, trigger_quote=None) -> None:
        """Mark the leg 'stopping' (persisted first), then make ONE buy-to-close attempt.
        _manage calls this again every tick while the leg stays 'stopping', so a close
        that cannot fill never blinds the other short's stop or the daily-loss check.
        Whatever is still open at the close is flattened or settles."""
        first = leg["status"] != "stopping"
        if first:
            leg["status"] = "stopping"
            leg["stop_triggered_at"] = _iso(self.clock.now())
            if trigger_quote is not None:
                leg["stop_trigger_quote"] = [trigger_quote.bid, trigger_quote.ask]
            await store.save(s)        # persisted BEFORE the close order
            await self._log(
                f"STOP {leg['role']} {leg['symbol']}: buy-to-close "
                f"{(trigger_quote.ask if trigger_quote else float('nan')):.2f} >= stop "
                f"{leg['stop_level']:.2f} (entry {leg['entry_price']:.2f})", category="signal")
        remaining = int(leg["qty"]) - int(leg.get("exit_qty") or 0)
        got, px, _ = await self._close_qty(s, leg, remaining, "stop", self.clock.now())
        if got:
            self._add_exit(leg, got, px, by_order=True)
        if int(leg.get("exit_qty") or 0) < int(leg["qty"]):
            leg["stop_attempts"] = int(leg.get("stop_attempts") or 0) + 1   # step the cap up
        if int(leg.get("exit_qty") or 0) >= int(leg["qty"]):
            await self._finish_leg(s, leg, "stopped", "stop")
            px = float(leg["exit_price"])
            await self._log(
                f"STOPPED {leg['role']} x{leg['qty']} @ {px:.2f} (stop "
                f"{leg['stop_level']:.2f}, slippage vs stop {px - leg['stop_level']:+.2f}); "
                f"leg P&L ${leg['pnl']:.2f}. Other legs unchanged.", category="trade",
                data={"role": leg["role"], "fill": px, "stop_level": leg["stop_level"],
                      "slippage_vs_stop": round(px - leg["stop_level"], 4),
                      "pnl": leg["pnl"]})
        elif first:
            await self._log(f"stop close for {leg['role']} not filled yet "
                            f"({leg.get('exit_qty') or 0}/{leg['qty']}); retrying every tick",
                            level="warning")
        await store.save(s)

    async def _flatten(self, s: dict, reason: str) -> None:
        """Close every open leg, shorts first, one attempt per leg per round until all
        are closed or the session close, so a leg with no usable quote never holds up
        the others. A leg nobody bids for is left to settle."""
        left = sorted(self._legs_open(s), key=lambda l: l["action"] != "SELL")
        while left:
            for leg in list(left):
                remaining = int(leg["qty"]) - int(leg.get("exit_qty") or 0)
                got, px, no_bid = await self._close_qty(s, leg, remaining, reason,
                                                        self.clock.now())
                if got:
                    self._add_exit(leg, got, px, by_order=True)
                if s["status"] == "error":     # SAFE_MODE tripped: no more orders
                    await store.save(s)
                    return
                if int(leg.get("exit_qty") or 0) >= int(leg["qty"]):
                    await self._finish_leg(s, leg, "closed", reason)
                    left.remove(leg)
                elif no_bid:
                    await self._log(f"{reason}: {leg['role']} has no bid; left to settle")
                    left.remove(leg)
            if not left or self.clock.now() >= s["close_time"]:
                break
            await self.clock.sleep(self.p.close_retry_seconds)
        await store.save(s)
        await self._log(f"flattened remaining legs ({reason})", category="trade")

    def _daily_loss_limit(self, s: dict) -> float:
        """$ limit for the day: per-contract limit x the flies sized at entry."""
        return self.p.max_daily_loss_per_contract * max(1, int(s.get("contracts") or 1))

    async def _breach(self, s: dict, net: float) -> None:
        s["daily_loss_breached"] = True
        msg = (f"max_daily_loss breached on {s['session_date']}: P&L {net:.2f} <= "
               f"-{self._daily_loss_limit(s):.2f} (${self.p.max_daily_loss_per_contract:.0f} "
               f"x {s['contracts']} fly); new entries halted until cleared")
        await store.set_halt(msg)
        await store.save(s)
        await self._log(msg, level="error")
        await self._alert("xsp_fly_daily_loss", msg, {"pnl": net})

    async def _manage(self, s: dict) -> None:
        p = self.p
        close = s["close_time"]
        flatten_at = (close - timedelta(minutes=p.flatten_minutes_before_close)
                      if p.close_before_settlement else None)
        for leg in [l for l in self._legs_open(s) if l["status"] == "stopping"]:
            await self._stop_leg(s, leg)       # resume a stop interrupted by a restart
        last_sub = None
        stale = False
        while True:
            confirm_soon = False
            if s["status"] == "error":         # SAFE_MODE tripped: a human takes over
                return
            now = self.clock.now()
            if now >= close or (flatten_at and now >= flatten_at):
                break
            open_legs = self._legs_open(s)
            if not open_legs:
                break
            if last_sub is None or (now - last_sub).total_seconds() >= 15:
                await self.feed.subscribe([l["symbol"] for l in open_legs] + [p.ticker])
                last_sub = now
            marks: dict[str, float] = {}
            newest = None
            for leg in open_legs:
                q = self.feed.quote(leg["symbol"])
                if q is not None:
                    if q.ts is not None and (newest is None or q.ts > newest):
                        newest = q.ts
                    if L.quote_valid(q.bid, q.ask):
                        marks[leg["role"]] = L.mid(q.bid, q.ask)
                if leg["status"] == "stopping":
                    await self._stop_leg(s, leg)        # retry the unfilled stop close
                elif leg["action"] == "SELL" and leg["status"] == "open":
                    key = (s.get("session_date"), leg["role"])
                    past = q is not None and L.stop_check(
                        q.bid, q.ask, q.age(self.clock.now()), leg["stop_level"],
                        p.stop_trigger, max_age=p.stop_max_quote_age,
                        max_spread_abs=p.stop_max_spread_abs,
                        max_spread_pct=p.stop_max_spread_pct)[0]
                    if not past:
                        self._stop_seen.pop(key, None)
                    elif key not in self._stop_seen:
                        self._stop_seen[key] = self.clock.now()   # confirm on a later check
                        confirm_soon = True
                    elif ((self.clock.now() - self._stop_seen[key]).total_seconds()
                          >= p.stop_confirm_seconds - 1e-9):
                        self._stop_seen.pop(key, None)
                        await self._stop_leg(s, leg, q)
                    else:
                        confirm_soon = True
            age = None if newest is None else (self.clock.now() - newest).total_seconds()
            if age is None or age > p.feed_stale_alert_seconds:
                if not stale:
                    stale = True
                    msg = f"option quotes stale (age {age}s); stops are blind until data resumes"
                    await self._log(msg, level="warning")
                    await self._alert("xsp_fly_stale", msg)
            elif stale:
                stale = False
                await self._log("option quotes fresh again; stop monitoring resumed")
            if not s.get("daily_loss_breached"):
                mtm = L.mark_to_market(s["legs"], marks, p.multiplier)
                if mtm is not None:
                    net = mtm + float(s["extra_pnl"]) - float(s["fees"])
                    if net <= -self._daily_loss_limit(s):
                        await self._breach(s, net)
                        if p.flatten_on_daily_loss:
                            await self._flatten(s, "daily_loss")
                            break
            await self.clock.sleep(min(p.monitor_interval, p.stop_confirm_seconds)
                                   if confirm_soon else p.monitor_interval)
        if s["status"] == "error":             # SAFE_MODE: no flatten, no settlement
            return
        for leg in self._legs_open(s):
            if leg["status"] == "stopping":
                await self._log(f"stop close for {leg['role']} incomplete at the end of "
                                f"monitoring ({leg.get('exit_qty') or 0}/{leg['qty']}); the "
                                "remainder is flattened or settles", level="warning")
        if flatten_at and self.clock.now() >= flatten_at and self._legs_open(s):
            await self._flatten(s, "pre_settlement")
        if s["status"] == "error":
            return
        await self._settle(s)

    # ------------------------------------------------------------------ settlement
    async def _settle(self, s: dict) -> None:
        close = s["close_time"]
        if not self._legs_open(s):
            await self._complete(s, None)
            return
        await self._sleep_until(close + timedelta(seconds=self.p.settlement_delay_seconds))
        deadline = close + timedelta(minutes=self.p.settlement_timeout_minutes)
        while True:
            px = await self.feed.settlement_price(self.p.ticker, s["session_date"], close,
                                                  self.clock.now())
            if px is not None:
                break
            if self.clock.now() >= deadline:
                s["reason"] = "settlement price unavailable; will settle on a later start"
                await store.save(s)
                await self._log(f"{s['session_date']}: {s['reason']}", level="error")
                await self._alert("xsp_fly_settlement", s["reason"])
                return
            await self.clock.sleep(15.0)
        await self._apply_settlement(s, px)

    async def _apply_settlement(self, s: dict, px: float) -> None:
        for leg in self._legs_open(s):
            remaining = int(leg["qty"]) - int(leg.get("exit_qty") or 0)
            iv = L.intrinsic(leg["right"], leg["strike"], px)
            self._record_fill(s, leg, "BUY" if leg["action"] == "SELL" else "SELL",
                              remaining, iv, None, "settle", fee=False)
            self._add_exit(leg, remaining, iv, by_order=False)
            await self._finish_leg(s, leg, "settled", "settlement")
        s["settlement_price"] = px
        await self._complete(s, px)

    async def _complete(self, s: dict, px: float | None) -> None:
        legs = [l for l in s["legs"] if int(l.get("qty") or 0) > 0]
        leg_pnl = sum(float(l.get("pnl") or 0) for l in legs)
        leg_fees = sum(float(l.get("fees") or 0) for l in legs)
        other_fees = float(s["fees"]) - leg_fees
        s["realized_pnl"] = round(leg_pnl + float(s["extra_pnl"]) - other_fees, 2)
        s["status"] = "settled" if px is not None else "closed"
        s["closed_time"] = self.clock.now()
        states = await store.trade_states([l["trade_id"] for l in legs if l.get("trade_id")])
        s["reconciliation"] = reconcile_session(s, states)
        if hasattr(self.ex, "check_positions") and s.get("mode") == "alpaca_paper":
            issues = await self.ex.check_positions(self._expected_broker(s))
            rec = s["reconciliation"]
            rec["broker"] = {"checked": "Alpaca app-owned fills vs session order fills",
                             "issues": issues,
                             "note": "cash settlement posts ~21:00 ET (OPCSH/OPEXP; "
                                     "visible the next day on paper)"}
            rec["issues"] = rec["issues"] + [f"broker: {i}" for i in issues]
            rec["ok"] = rec["ok"] and not issues
        await store.save(s)
        rec = s["reconciliation"]
        await self._log(
            f"{s['session_date']}: {s['status'].upper()}"
            + (f" at XSP {px:.2f}" if px is not None else "")
            + f": realized ${s['realized_pnl']:.2f} (fees ${s['fees']:.2f}); "
            f"reconciliation {'OK' if rec['ok'] else 'MISMATCH'}", category="trade",
            level="info" if rec["ok"] else "error",
            data={"realized_pnl": s["realized_pnl"], "settlement_price": px,
                  "legs": [{k: l.get(k) for k in ("role", "strike", "qty", "entry_price",
                                                  "exit_price", "exit_reason", "pnl")}
                           for l in legs], "reconciliation": rec})
        if not rec["ok"]:
            await self._alert("xsp_fly_recon", "XSP fly end-of-day reconciliation mismatch",
                              rec)


def reconcile_session(s: dict, trade_states: dict[int, dict]) -> dict:
    """End-of-day check of the session against its own fill ledger and TradeRecords.
    In sim mode there is no broker statement to compare against, so this is internal
    consistency only (stated in ``scope``)."""
    issues = []
    legs = [l for l in s["legs"] if int(l.get("qty") or 0) > 0]
    for l in legs:
        if l.get("status") not in TERMINAL_LEG:
            issues.append(f"{l['role']} not closed (status {l.get('status')})")
    net: dict[str, int] = {}
    for f in s.get("fills") or []:
        sign = -1 if f["side"] == "SELL" else 1
        net[f["symbol"]] = net.get(f["symbol"], 0) + sign * int(f["qty"])
    for sym, q in net.items():
        if q != 0:
            issues.append(f"fill ledger not flat for {sym}: {q:+d}")
    fee_sum = round(sum(float(f.get("fee") or 0) for f in s.get("fills") or []), 4)
    if abs(fee_sum - float(s.get("fees") or 0)) > 0.005:
        issues.append(f"fees {s.get('fees')} != fill ledger {fee_sum}")
    for l in legs:
        st = trade_states.get(l.get("trade_id"))
        if st is None:
            issues.append(f"{l['role']} has no TradeRecord")
        elif st["status"] != "closed":
            issues.append(f"{l['role']} TradeRecord still {st['status']}")
        elif st["pnl"] is not None and abs(float(st["pnl"]) - float(l.get("pnl") or 0)) > 0.01:
            issues.append(f"{l['role']} TradeRecord pnl {st['pnl']} != leg pnl {l.get('pnl')}")
    return {"ok": not issues, "issues": issues,
            "scope": "internal: legs vs fill ledger vs TradeRecords"}


def _params_dict(p: IronFlyParams) -> dict:
    from dataclasses import asdict
    d = asdict(p)
    d["entry_time"] = p.entry_time.strftime("%H:%M")
    return d
