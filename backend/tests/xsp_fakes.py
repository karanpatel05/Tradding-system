"""Test doubles for the XSP iron fly on Alpaca paper: a fake Alpaca port that fills
orders against a fake feed's quotes, and the real AlpacaFlyExecutor wired to it."""
from __future__ import annotations

import itertools

from app.broker import alpaca_client as ac
from app.strategies.xsp_iron_fly.alpaca_executor import AlpacaFlyExecutor


class FakeAlpaca:
    """Port double. Orders fill against the feed's quotes when marketable, at the touch
    (combo legs: shorts at the bid, wings at the ask). Knobs: ``reject`` (submit
    refused), ``fill_cap`` (max qty per order), ``open_polls`` (polls an order stays
    open before it can fill), ``fill_on_cancel`` (a resting order fills during the
    cancel), ``unknown`` (submit result lost; the order did land)."""

    def __init__(self, feed) -> None:
        self.feed = feed
        self.orders: dict[str, dict] = {}
        self.bodies: list[dict] = []
        self.ids = itertools.count(1)
        self.reject: str | None = None
        self.fill_cap: int | None = None
        self.open_polls = 0
        self.fill_on_cancel = False
        self.unknown = False
        self.fill_once = False                   # after a partial, nothing more fills
        self.unreadable_after = None             # order ids >= this read back as None
        self.lookup = "ok"                       # find_by_ref: ok | missing | error
        self.no_id = False                       # POST "transmitted" without an order id
        self.hold_status = None                  # e.g. "stopped": stays non-terminal
        self.list_error = False                  # open_orders() raises
        self.unreadable_intents: set = set()     # e.g. {"buy_to_close"}: reads back None
        self.unreadable_ids: set = set()
        self.extra: dict[str, int] = {}          # fills Alpaca has that the app didn't see

    def _touch(self, occ_sym):
        """Resolve an OCC symbol to the fake feed's contract and return its (bid, ask)."""
        for sym, (r, k, e) in self.feed.meta.items():
            if ac.occ_unpadded(f"XSP {e:%y%m%d}{r}{int(k):08d}") == occ_sym:
                q = self.feed.quote(sym)
                return q.bid, q.ask
        raise KeyError(occ_sym)

    def bind(self, legs):            # kept for older call sites; resolution uses the feed
        return None

    async def submit(self, body, intent, qty):
        self.bodies.append(body)
        if self.reject:
            return {"transmitted": False, "detail": f"rejected by Alpaca: {self.reject}"}
        oid = f"o{next(self.ids)}"
        self.orders[oid] = {"id": oid, "status": "new", "filled_qty": "0", "body": body,
                            "polls": 0, "client_order_id": f"tsys-{oid}", "legs": []}
        if self.unknown:
            return {"transmitted": False, "unknown_state": True, "ref": oid}
        if body.get("position_intent") in self.unreadable_intents:
            self.unreadable_ids.add(oid)
        if self.no_id:
            return {"transmitted": True, "order_id": "", "ref": oid}
        if self.hold_status is not None:
            self.orders[oid]["status"] = self.hold_status
        return {"transmitted": True, "order_id": oid, "ref": oid}

    async def find_by_ref(self, ref):
        if self.lookup == "error":
            return "error", None
        o = self.orders.get(ref) if self.lookup == "ok" else None
        return ("found", o) if o else ("missing", None)

    def _try_fill(self, o):
        b = o["body"]
        if self.fill_once and int(o["filled_qty"]):
            return
        want = int(b["qty"]) - int(o["filled_qty"])
        n = min(want, self.fill_cap) if self.fill_cap else want
        if b.get("order_class") == "mleg":
            touches = {x["symbol"]: self._touch(x["symbol"]) for x in b["legs"]}
            nat = (sum(touches[x["symbol"]][0] for x in b["legs"] if x["side"] == "sell")
                   - sum(touches[x["symbol"]][1] for x in b["legs"] if x["side"] == "buy"))
            if nat + 1e-9 < -float(b["limit_price"]):
                return
            o["legs"] = [{"symbol": x["symbol"], "side": x["side"],
                          "filled_qty": str(int(o["filled_qty"]) + n),
                          "filled_avg_price": str(touches[x["symbol"]][0 if x["side"] == "sell"
                                                                       else 1])}
                         for x in b["legs"]]
        else:
            bid, ask = self._touch(b["symbol"])
            lim = float(b["limit_price"])
            if b["side"] == "buy" and lim + 1e-9 < ask or b["side"] == "sell" and lim > bid:
                return
            o["filled_avg_price"] = str(ask if b["side"] == "buy" else bid)
        o["filled_qty"] = str(int(o["filled_qty"]) + n)
        o["status"] = "filled" if int(o["filled_qty"]) >= int(b["qty"]) else "partially_filled"

    async def get_order(self, oid):
        if self.unreadable_after and int(oid[1:]) >= self.unreadable_after:
            return None
        if oid in self.unreadable_ids:
            return None
        o = self.orders[oid]
        if o["status"] in ("new", "partially_filled"):
            o["polls"] += 1
            if o["polls"] > self.open_polls:
                self._try_fill(o)
        return {k: v for k, v in o.items() if k != "body"}

    async def cancel(self, oid):
        o = self.orders[oid]
        if o["status"] in ("new", "partially_filled"):
            if self.fill_on_cancel:
                cap, self.fill_cap = self.fill_cap, None
                self._try_fill(o)
                self.fill_cap = cap
            if o["status"] != "filled":
                o["status"] = "canceled"

    async def positions(self, *, include_expired=False):
        net = dict(self.extra)
        for o in self.orders.values():
            b = o["body"]
            if b.get("order_class") == "mleg":
                for x in o["legs"]:
                    s = 1 if x["side"] == "buy" else -1
                    net[x["symbol"]] = net.get(x["symbol"], 0) + s * int(x["filled_qty"])
            elif int(o["filled_qty"]):
                s = 1 if b["side"] == "buy" else -1
                net[b["symbol"]] = net.get(b["symbol"], 0) + s * int(o["filled_qty"])
        return {k: v for k, v in net.items() if v}

    async def open_orders(self):
        if self.list_error:
            raise RuntimeError("listing failed")
        return [{"id": o["id"], "symbol": o["body"].get("symbol"),
                 "legs": o["body"].get("legs") or []}
                for o in self.orders.values() if o["status"] in ("new", "partially_filled")]




CURRENT: "FakeAlpaca | None" = None   # one fake broker per test, shared across restarts


def reset() -> None:
    global CURRENT
    CURRENT = None


class TestExec(AlpacaFlyExecutor):
    """The production AlpacaFlyExecutor on a FakeAlpaca port, with short poll windows.
    ``max_fill_qty`` makes every order fill at most that many and then stop (partials);
    ``supports_combo=False`` exercises the wings-first leg-in."""

    __test__ = False                  # not a pytest class

    def __init__(self, feed, clock, *, supports_combo: bool = True,
                 max_fill_qty: int | None = None, port: "FakeAlpaca | None" = None) -> None:
        global CURRENT
        if port is None:
            if CURRENT is None:
                CURRENT = FakeAlpaca(feed)
            port = CURRENT
        port.feed = feed
        port.fill_cap, port.fill_once = max_fill_qty, bool(max_fill_qty)
        super().__init__(port, feed, clock, window=2.0, step=0.5, cancel_wait=2.0)
        self.supports_combo = supports_combo
