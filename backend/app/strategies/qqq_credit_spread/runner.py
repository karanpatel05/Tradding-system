"""The in-app QQQ first-hour credit spread, one session at a time, on Alpaca PAPER.

Timeline (XNYS hours; half days shift the afternoon to 12:30 / 12:50 / 12:55):
  10:30  entry      read the completed first hour, sell the $1 vertical at the mid,
                    re-price every 15 s, cancel if unfilled 10 min after 10:30
  15:30  checkpoint the model predicts P(short strike finishes ITM); strictly above its
                    threshold -> close now, otherwise hold for the sweep
  15:50  final      close whatever is left, walking the limit from mid to marketable,
                    aiming to be flat by 15:55 (the order is left working after that)

Safety rules:
* the day's session row is written BEFORE any order (idempotent entry across restarts);
* every open/closed decision reads what the APP'S OWN tagged Alpaca orders filled, never
  what this process believes it sent;
* an action only fires inside its grace window; a late start records ``missed``;
* stopping the worker cancels its working order; positions are left to the operator
  (the kill switch flattens app-owned positions).
All collaborators are injected so the whole day runs in tests on simulated time.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta

from app.strategies.qqq_credit_spread import logic as L
from app.strategies.qqq_credit_spread.broker import (TERMINAL_UNFILLED, filled_qty,
                                                     leg_fill_prices, net_fill_price,
                                                     order_status)
from app.strategies.qqq_credit_spread.model import ModelUnavailable, check_metadata, decide_close
from app.strategies.qqq_credit_spread.params import QqqSpreadParams

logger = logging.getLogger("trading.qqq_spread")

SLICE = 30.0                   # max single sleep, so stop/cancel lands promptly
ERROR_BACKOFF = 30.0
BAR_WAIT_STEP = 5.0
BAR_SETTLE = 15.0              # wait up to this long after 10:30 for the 10:30 bar
HOLDING = ("entering", "open", "closing")


class RealClock:
    def now(self) -> datetime:
        return datetime.now(L.ET)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class QqqSpreadRunner:
    def __init__(self, key: str, params: QqqSpreadParams, feed, broker, clock, *,
                 store, capital_fn, model_loader, safe_mode_fn, alert_fn, log_fn,
                 entry_guard_fn=None) -> None:
        self.key, self.p = key, params
        self.entry_guard_fn = entry_guard_fn      # extra NEW-entry checks (daemon copy...)
        self.feed, self.broker, self.clock, self.store = feed, broker, clock, store
        self.capital_fn, self.model_loader = capital_fn, model_loader
        self.safe_mode_fn, self.alert_fn, self.log_fn = safe_mode_fn, alert_fn, log_fn

    # ------------------------------------------------------------------ helpers
    def plan(self, d: date) -> L.SessionPlan | None:
        return L.session_plan(d, self.p.final_close_seconds, self.p.close_deadline_seconds)

    async def _log(self, msg: str, level: str = "info", category: str = "action",
                   data: dict | None = None) -> None:
        getattr(logger, "warning" if level in ("warning", "error") else "info")(msg)
        try:
            await self.log_fn(msg, level=level, category=category, strategy_key=self.key,
                              data=data)
        except Exception:  # noqa: BLE001 - journaling never breaks trading
            logger.exception("journal write failed")

    async def _alert(self, msg: str, data: dict | None = None) -> None:
        await self._log(msg, level="error", category="system", data=data)
        try:
            await self.alert_fn("qqq_spread", msg, data or {})
        except Exception:  # noqa: BLE001
            logger.exception("alert failed")

    async def _sleep_until(self, target: datetime) -> None:
        while True:
            remaining = (target - self.clock.now()).total_seconds()
            if remaining <= 0:
                return
            await self.clock.sleep(min(SLICE, remaining))

    async def _finish(self, sess: dict, status: str, reason: str) -> None:
        sess["status"], sess["reason"] = status, reason
        await self.store.save(sess)
        await self._log(f"QQQ spread {sess['session_date']}: {status} - {reason}",
                        category="decision")

    @staticmethod
    def _syms(sess: dict) -> tuple[str, str]:
        sig = sess["data"]["signal"]
        return sig["short_symbol"], sig["long_symbol"]

    # ------------------------------------------------------------------ main loop
    async def run_forever(self) -> None:
        await self._log(f"QQQ spread worker started ({self.broker.name})")
        started = False
        while True:
            try:
                if not started:
                    await self.startup()
                    started = True
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad step must not end the day
                logger.exception("QQQ spread step failed")
                await self._alert(f"QQQ spread step failed: {type(exc).__name__}: {exc}")
                await self.clock.sleep(ERROR_BACKOFF)

    async def startup(self) -> None:
        """Resolve days left holding by an earlier run before acting on today."""
        today = self.clock.now().date()
        for old in await self.store.unresolved_before(self.key, today):
            await self._resolve_stale(old)
        sess = await self.store.get_session(self.key, today)
        if sess and sess["status"] == "entering":
            await self._resolve_interrupted_entry(sess)

    async def step(self) -> None:
        now = self.clock.now()
        plan = self.plan(now.date())
        if plan is None or now >= plan.close + timedelta(minutes=5):
            await self._after_close(plan)
            await self._sleep_until(self._next_morning(now))
            return
        sess = await self.store.get_session(self.key, plan.day)
        status = sess["status"] if sess else "pending"
        if status == "entering":
            # do_entry runs within one step; meeting it here means that step failed
            # after the session row was written. Trust the broker, never re-enter.
            await self._resolve_interrupted_entry(sess)
            return
        if status == "pending":
            if now < plan.entry:
                await self._sleep_until(plan.entry)
                return
            await self.do_entry(plan, sess)
            return
        if status not in HOLDING:
            await self._sleep_until(plan.close + timedelta(minutes=5))   # day done
            return
        data = sess["data"]
        if now < plan.checkpoint:
            await self._sleep_until(plan.checkpoint)
            return
        if "checkpoint" not in data and now < plan.final_close:
            await self.do_checkpoint(plan, sess)
            return
        if now < plan.final_close:
            await self._sleep_until(plan.final_close)
            return
        if not (data.get("final_close") or {}).get("done"):
            await self.do_final_close(plan, sess)    # retried after an error until it ran
            return
        await self._watch_after_deadline(plan, sess)

    def _next_morning(self, now: datetime) -> datetime:
        return datetime.combine(now.date() + timedelta(days=1), time(9, 0), tzinfo=L.ET)

    # ------------------------------------------------------------------ entry
    def entry_block(self) -> str | None:
        if (errs := self.p.validate()):
            return "invalid settings: " + "; ".join(errs)
        if (why := self.broker.routing_block()) is not None:
            return why
        if self.entry_guard_fn is not None and (why := self.entry_guard_fn()) is not None:
            return why
        try:
            check_metadata(self.p.model_dir, self.p.max_model_age_days)
        except ModelUnavailable as exc:
            return f"checkpoint model unavailable: {exc}"
        return None

    async def do_entry(self, plan: L.SessionPlan, sess: dict | None) -> None:
        sess = (sess or await self.store.create_session(self.key, plan.day)
                or await self.store.get_session(self.key, plan.day))
        if sess["status"] != "pending":
            return
        now = self.clock.now()
        if not L.within_grace(plan.entry, now, self.p.grace_minutes):
            late = (now - plan.entry).total_seconds() / 60
            await self._finish(sess, "missed",
                               f"started {late:.0f} min after the {plan.entry:%H:%M} entry; "
                               f"a late entry no longer matches its signal")
            return
        if (why := self.entry_block()) is not None:
            await self._finish(sess, "skipped", why)
            return
        if await self.safe_mode_fn():
            await self._finish(sess, "skipped", "SAFE_MODE is on")
            return
        capital = float(await self.capital_fn() or 0.0)
        ladder = L.ladder_quantity(capital, self.p.capital_per_spread, self.p.first_spread_capital)
        qty = min(ladder, int(self.p.max_contracts))
        data = sess["data"]
        data.update(capital=capital, ladder_quantity=ladder, max_contracts=self.p.max_contracts)
        if qty < 1:
            await self._finish(sess, "skipped", "no capital allocated" if capital <= 0 else
                               f"allocation ${capital:,.0f} is below the "
                               f"${self.p.first_spread_capital:,.0f} first-spread minimum")
            return
        deadline = plan.entry + timedelta(minutes=self.p.grace_minutes)
        bars = await self._first_hour_bars(plan, deadline)
        signal, reason = L.first_hour_signal(bars, plan)
        if signal is None:
            await self._finish(sess, "skipped", f"no_trade:{reason}")
            return
        s, l = signal.short_symbol, signal.long_symbol
        quotes = await self.feed.option_quotes([s, l], plan.day)
        credit, reason = L.spread_limit_credit(quotes, s, l)
        data.update(signal=signal.to_dict(), quantity=qty, entry_quotes=quotes)
        if credit is None:
            await self._finish(sess, "skipped", f"no_trade:{reason}")
            return
        if credit < self.p.min_credit:
            data["observed_credit"] = credit
            await self._finish(sess, "skipped", f"no_trade:credit {credit:.4f} below the "
                                                f"{self.p.min_credit:.2f} minimum")
            return
        data.update(observed_credit=credit, limit_credit=credit)
        sess["status"] = "entering"
        await self.store.save(sess)              # durable BEFORE the order
        side = "put" if signal.right == "P" else "call"
        await self._log(f"QQQ entry: sell {qty} {side} spread {s}/{l} @ {credit:.2f} credit "
                        f"(Alpaca paper)", category="signal", data=signal.to_dict())

        async def reprice():
            q = await self.feed.option_quotes([s, l], plan.day)
            return L.spread_limit_credit(q, s, l)[0]

        work = await self._work_order(s, l, qty, credit, True, deadline, reprice,
                                      cancel_at_deadline=True)
        data["entry_order"] = work
        await self._settle_entry(sess, work)

    async def _first_hour_bars(self, plan: L.SessionPlan, deadline: datetime) -> list[dict]:
        """Bars through the 10:29 minute; wait briefly for the last one to arrive."""
        last_needed = int((plan.entry - timedelta(minutes=1)).timestamp() * 1000)
        after = int(plan.entry.timestamp() * 1000)
        bars: list[dict] = []
        while True:
            bars = await self.feed.minute_bars(plan.open, plan.entry + timedelta(minutes=1)) or []
            have_last = any(int(b["time"]) >= last_needed for b in bars)
            # The 10:29 close sets colour and strike: only trust it once the 10:30 bar
            # has started (or a short settle time has passed).
            settled = (any(int(b["time"]) >= after for b in bars)
                       or self.clock.now() >= plan.entry + timedelta(seconds=BAR_SETTLE))
            if have_last and settled:
                return [b for b in bars if int(b["time"]) < after]
            if self.clock.now() + timedelta(seconds=BAR_WAIT_STEP) >= deadline:
                return bars
            await self.clock.sleep(BAR_WAIT_STEP)

    async def _settle_entry(self, sess: dict, work: dict) -> None:
        data = sess["data"]
        s, l = self._syms(sess)
        sq, lq = await self.broker.leg_quantities(s, l)
        qty = min(sq, lq)
        if sq != lq:
            await self._alert(f"QQQ entry left unmatched legs: short {sq}, long {lq} "
                              f"(Alpaca paper) - check the account", {"short": sq, "long": lq})
        if qty < 1:
            detail = work.get("detail") or ""
            await self._finish(sess, "skipped", f"entry_unfilled ({work.get('status')}"
                                                f"{': ' + detail if detail else ''})")
            return
        legs = work.get("leg_prices") or {}
        net = work.get("fill_price")
        if net is None:
            net = data.get("limit_credit")
        short_px = legs.get(s, net)
        long_px = legs.get(l, 0.0 if s not in legs else max(0.0, legs[s] - net))
        ids = await self.store.open_leg_trades(self.key, s, l, qty, short_px, long_px,
                                               self.clock.now())
        data.update(trade_ids=ids, open_qty=qty, filled_credit=round(net, 4),
                    entry_time=self.clock.now().isoformat())
        sess["status"], sess["reason"] = "open", f"sold {qty} @ {net:.2f} credit"
        await self.store.save(sess)
        await self._log(f"QQQ entry FILLED: {qty} x {s}/{l} @ {net:.2f} credit (Alpaca paper)",
                        category="trade", data={"order": work})

    async def _resolve_interrupted_entry(self, sess: dict) -> None:
        """A restart found the day mid-entry: cancel the resting opening order, then
        trust the broker's fills."""
        s, l = self._syms(sess)
        for o in await self.broker.working_orders({s, l}):
            await self.broker.cancel_confirmed(o["id"])
        await self._log("QQQ entry was interrupted by a restart; working order cancelled, "
                        "reconciling with Alpaca", level="warning")
        await self._settle_entry(sess, {"status": "interrupted", "detail": "restart"})

    # ------------------------------------------------------------------ order work
    async def _work_order(self, s: str, l: str, qty: int, price: float, opening: bool,
                          deadline: datetime, reprice, *, cancel_at_deadline: bool) -> dict:
        """Submit and chase a limit until filled, terminal, or the deadline. Re-price
        (cancel-confirmed + fresh tagged resubmit) when the new limit moves >= 1 cent."""
        out = {"status": "rejected", "filled": 0, "order_ids": [], "reprices": [],
               "fill_price": None, "leg_prices": {}, "last_limit": round(price, 2)}
        fills: list[tuple[int, float | None, dict]] = []

        def record(o: dict | None) -> int:
            n = filled_qty(o)
            if n > 0:
                fills.append((n, net_fill_price(o, opening), leg_fill_prices(o)))
            return n

        def done(status: str, detail: str = "", working: str | None = None) -> dict:
            total = sum(n for n, _, _ in fills)
            priced = [(n, px) for n, px, _ in fills if px is not None]
            out.update(status=status, filled=total, detail=detail)
            if priced:
                out["fill_price"] = round(sum(n * px for n, px in priced) /
                                          sum(n for n, _ in priced), 4)
            legs: dict[str, float] = {}
            for n, _, lp in fills:
                for sym, px in lp.items():
                    legs[sym] = px          # one-lot strategy: last fill's legs
            out["leg_prices"] = legs
            if working:
                out["working_order_id"] = working
            return out

        remaining = qty
        sent = await self.broker.submit_spread(s, l, remaining, price, opening)
        if not sent.get("transmitted"):
            return done("rejected", sent.get("detail", "not transmitted"))
        oid = sent["order_id"]
        out["order_ids"].append(oid)
        try:
            while True:
                left = (deadline - self.clock.now()).total_seconds()
                await self.clock.sleep(max(0.0, min(self.p.reprice_seconds, left)))
                o = await self.broker.get_order(oid)
                st = order_status(o)
                if st == "filled":
                    record(o)
                    return done("filled")
                if st in TERMINAL_UNFILLED:
                    record(o)
                    return done(f"order_{st}", str((o or {}).get("status")))
                if self.clock.now() >= deadline:
                    if not cancel_at_deadline:
                        return done("working_at_deadline", working=oid)
                    final = await self.broker.cancel_confirmed(oid)
                    record(final)
                    if order_status(final) == "filled" or sum(n for n, _, _ in fills) >= qty:
                        return done("filled")
                    return done("unfilled_by_deadline", order_status(final))
                new = await reprice()
                if self.clock.now() >= deadline:
                    continue                     # never start a late replacement
                if new is None or round(new, 2) == round(price, 2):
                    continue
                final = await self.broker.cancel_confirmed(oid)
                if final is None or order_status(final) not in TERMINAL_UNFILLED | {"filled"}:
                    continue                     # cancel unconfirmed: never send a second order
                remaining -= record(final)
                if remaining <= 0 or order_status(final) == "filled":
                    return done("filled")
                out["reprices"].append({"at": self.clock.now().isoformat(),
                                        "from": round(price, 2), "to": round(new, 2)})
                price = new
                out["last_limit"] = round(price, 2)
                sent = await self.broker.submit_spread(s, l, remaining, price, opening)
                if not sent.get("transmitted"):
                    return done("rejected_on_reprice", sent.get("detail", ""))
                oid = sent["order_id"]
                out["order_ids"].append(oid)
        except BaseException:
            # Worker stopped or a step failed: never leave an untracked order resting.
            try:
                await asyncio.shield(self.broker.cancel_confirmed(oid))
            except Exception:  # noqa: BLE001
                logger.exception("cancel after interruption failed for %s", oid)
            raise

    # ------------------------------------------------------------------ checkpoint
    async def do_checkpoint(self, plan: L.SessionPlan, sess: dict) -> None:
        data = sess["data"]
        now = self.clock.now()
        if not L.within_grace(plan.checkpoint, now, self.p.grace_minutes):
            data["checkpoint"] = {"status": "missed", "at": now.isoformat()}
            await self.store.save(sess)
            await self._log("QQQ checkpoint missed (outside its window); the 15:50 sweep "
                            "will close", level="warning")
            return
        s, l = self._syms(sess)
        sq, lq = await self.broker.leg_quantities(s, l)
        if sq == 0 and lq == 0:
            data["checkpoint"] = {"status": "nothing_open"}
            await self._mark_flat(sess, "flat at the checkpoint (closed outside the strategy)")
            return
        signal = L.Signal.from_dict(data["signal"])
        bars = await self.feed.minute_bars(plan.checkpoint - timedelta(minutes=66),
                                           plan.checkpoint)
        feat, reason = L.checkpoint_features(bars or [], signal, plan.checkpoint,
                                             float(data.get("filled_credit") or 0.0))
        if feat is None:
            data["checkpoint"] = {"status": f"hold:{reason}", "at": now.isoformat()}
            await self.store.save(sess)
            await self._log(f"QQQ checkpoint: no decision ({reason}); holding for the sweep",
                            level="warning")
            return
        try:
            pipeline, meta = self.model_loader()
            close, p, thr = decide_close(pipeline, meta, feat)
        except ModelUnavailable as exc:
            data["checkpoint"] = {"status": "hold:model_unavailable", "detail": str(exc)}
            await self.store.save(sess)
            await self._alert(f"QQQ checkpoint model unavailable ({exc}); holding for the sweep")
            return
        data["checkpoint"] = {"status": "close" if close else "hold", "p": round(p, 6),
                              "threshold": thr, "features": feat, "at": now.isoformat()}
        await self.store.save(sess)
        await self._log(f"QQQ checkpoint: P(short ITM)={p:.3f} vs {thr:.2f} -> "
                        f"{'CLOSE' if close else 'hold'}", category="decision",
                        data=data["checkpoint"])
        if close:
            await self._close(plan, sess, checkpoint=True,
                              deadline=L.checkpoint_stage_deadline(now, plan),
                              cancel_at_deadline=True)

    # ------------------------------------------------------------------ closes
    async def do_final_close(self, plan: L.SessionPlan, sess: dict) -> None:
        started = self.clock.now().isoformat()
        result = await self._close(plan, sess, checkpoint=False, deadline=plan.close_deadline,
                                   cancel_at_deadline=False)
        # Marked done only once the close actually ran; an exception retries it.
        sess["data"]["final_close"] = {"done": True, "status": result, "at": started}
        await self.store.save(sess)
        if sess["status"] in HOLDING:
            await self._alert(f"QQQ spread NOT flat at {plan.close_deadline:%H:%M} ET "
                              f"(Alpaca paper): the close order is left working - check "
                              f"the account")

    async def _close(self, plan: L.SessionPlan, sess: dict, *, checkpoint: bool,
                     deadline: datetime, cancel_at_deadline: bool) -> str:
        data = sess["data"]
        s, l = self._syms(sess)
        for o in await self.broker.working_orders({s, l}):     # never two closes at once
            await self.broker.cancel_confirmed(o["id"])
        sq, lq = await self.broker.leg_quantities(s, l)
        qty = min(sq, lq)
        if qty < 1:
            if sq or lq:
                await self._alert(f"QQQ residual leg (short {sq}, long {lq}) cannot be closed "
                                  f"as a spread - check the Alpaca paper account")
                return "residual_leg"
            await self._mark_flat(sess, "already flat at Alpaca")
            return "nothing_open"
        width = abs(data["signal"]["long_strike"] - data["signal"]["short_strike"])

        async def limit():
            q = await self.feed.option_quotes([s, l], plan.day)
            prog = L.close_progress(self.clock.now(), plan, checkpoint,
                                    self.p.final_close_seconds, self.p.close_deadline_seconds)
            return L.closing_limit_debit(q, s, l, width, prog)

        price = await limit()
        sess["status"] = "closing"
        await self.store.save(sess)
        label = "checkpoint" if checkpoint else "final"
        await self._log(f"QQQ {label} close: buy back {qty} x {s}/{l} @ {price:.2f} debit "
                        f"(Alpaca paper)", category="trade")
        work = await self._work_order(s, l, qty, price, False, deadline, limit,
                                      cancel_at_deadline=cancel_at_deadline)
        data.setdefault("closes", []).append({"stage": label, **work})
        return await self._settle_close(sess, work, label)

    async def _settle_close(self, sess: dict, work: dict, label: str) -> str:
        data = sess["data"]
        s, l = self._syms(sess)
        sq, lq = await self.broker.leg_quantities(s, l)
        legs = work.get("leg_prices") or {}
        net = work.get("fill_price")
        if net is None:
            net = work.get("last_limit")
        exit_prices = {"sell": legs.get(s, net), "buy": legs.get(l, 0.0 if s not in legs
                                                                 else max(0.0, legs[s] - net))}
        before = int(data.get("open_qty") or 0)
        now_open = min(sq, lq)
        if now_open < before:
            booked = await self.store.close_leg_trades(
                data.get("trade_ids") or {}, exit_prices, self.clock.now(),
                remaining={"sell": sq, "buy": lq} if (sq or lq) else None)
            sess["realized_pnl"] = round((sess.get("realized_pnl") or 0.0) + booked, 2)
            data["open_qty"] = now_open
        if sq == 0 and lq == 0:
            data["exit_debit"] = net
            sess["status"] = "closed"
            sess["reason"] = (f"closed at the {label} @ {net:.2f} debit; "
                              f"P&L ${sess['realized_pnl'] or 0:+,.2f}")
            await self.store.save(sess)
            await self._log(f"QQQ spread closed ({label}) @ {net:.2f} debit, "
                            f"P&L ${sess['realized_pnl'] or 0:+,.2f} (Alpaca paper)",
                            category="trade")
            return "closed"
        sess["status"] = "closing" if work.get("working_order_id") else "open"
        await self.store.save(sess)
        return work.get("status", "unfilled")

    async def _mark_flat(self, sess: dict, reason: str) -> None:
        """Broker shows nothing open (e.g. the kill switch flattened it). Close the
        leg records without a price; the realized P&L stays unknown."""
        await self.store.close_leg_trades_unpriced(sess["data"].get("trade_ids") or {},
                                                   self.clock.now())
        sess["status"], sess["reason"] = "closed", reason
        sess["data"]["open_qty"] = 0
        await self.store.save(sess)
        await self._log(f"QQQ spread {sess['session_date']}: {reason}", level="warning")

    async def _watch_after_deadline(self, plan: L.SessionPlan, sess: dict) -> None:
        """After 15:55 with a close still working: watch for its fill until the bell."""
        now = self.clock.now()
        if now >= plan.close + timedelta(minutes=4):
            await self._after_close(plan)
            return
        s, l = self._syms(sess)
        sq, lq = await self.broker.leg_quantities(s, l)
        if min(sq, lq) < int(sess["data"].get("open_qty") or 0) or (sq == 0 and lq == 0):
            work = (sess["data"].get("closes") or [{}])[-1]
            oid = work.get("working_order_id")
            o = await self.broker.get_order(oid) if oid else None
            await self._settle_close(sess, {**work, "fill_price": net_fill_price(o, False),
                                            "leg_prices": leg_fill_prices(o)}, "final")
            return
        if (min(sq, lq) > 0 and now < plan.close - timedelta(seconds=30)
                and not await self.broker.working_orders({s, l})):
            # Still open and nothing working (the last attempt was rejected or failed):
            # try again at the marketable price until the bell.
            await self._log("QQQ spread still open after 15:55 with no working close; "
                            "re-attempting", level="warning")
            await self._close(plan, sess, checkpoint=False,
                              deadline=plan.close - timedelta(seconds=20),
                              cancel_at_deadline=False)
            return
        await self.clock.sleep(self.p.reprice_seconds)

    async def _after_close(self, plan: L.SessionPlan | None) -> None:
        """Once the session is over, a day still holding is an incident."""
        if plan is None:
            return
        sess = await self.store.get_session(self.key, plan.day)
        if sess and sess["status"] in HOLDING and not sess["data"].get("after_close_alerted"):
            sess["data"]["after_close_alerted"] = True
            sess["status"], sess["reason"] = "error", "position still open after the close"
            await self.store.save(sess)
            await self._alert("QQQ spread still open after the close (Alpaca paper): it "
                              "expires/settles at the broker - check the account")

    async def _resolve_stale(self, old: dict) -> None:
        if not old["data"].get("signal"):
            old["status"], old["reason"] = "failed", "interrupted before any order"
            await self.store.save(old)
            return
        s, l = self._syms(old)
        sq, lq = await self.broker.leg_quantities(s, l)
        if sq == 0 and lq == 0:
            await self._mark_flat(old, "flat at Alpaca on a later start")
            return
        old["status"], old["reason"] = "error", f"legs still held after expiry day " \
                                                f"(short {sq}, long {lq})"
        await self.store.save(old)
        await self._alert(f"QQQ spread from {old['session_date']} still shows legs at "
                          f"Alpaca after its expiry day - check the account")
