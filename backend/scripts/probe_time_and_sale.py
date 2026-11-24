"""Read-only probe: does this Tastytrade account receive DXLink TimeAndSale events?

    python -m scripts.probe_time_and_sale --symbols SPY,QQQ,TSLA,NVDA --seconds 60

Opens its own DXLink connection; touches no database and writes no file.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict

from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import TimeAndSale

from app.tastytrade.session import get_tt_session


def _accept_fractional_sizes() -> None:
    # Same SDK quirk as the app's streamer: the SDK types size as int, Tastytrade can
    # send fractions, and the SDK silently drops events that fail validation.
    f = TimeAndSale.model_fields.get("size")
    if f is not None and f.annotation is int:
        f.annotation = float
        TimeAndSale.model_rebuild(force=True)


async def main(symbols: list[str], seconds: float) -> int:
    _accept_fractional_sizes()
    session = await get_tt_session()
    if session is None:
        print("BLOCKED: no Tastytrade session (credentials not configured)")
        return 1
    events: dict[str, list] = defaultdict(list)
    async with DXLinkStreamer(session) as dx:
        await dx.subscribe(TimeAndSale, symbols)

        async def collect() -> None:
            async for ev in dx.listen(TimeAndSale):
                events[ev.event_symbol].append(ev)

        task = asyncio.create_task(collect())
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    for sym in symbols:
        evs = events.get(sym, [])
        ext = sum(1 for e in evs if e.extended_trading_hours)
        invalid = sum(1 for e in evs if not e.valid_tick)
        types = Counter(e.type for e in evs)
        keys = Counter((e.time, e.sequence) for e in evs)
        dup_keys = sum(1 for c in keys.values() if c > 1)
        zero_seq = sum(1 for e in evs if not e.sequence)
        print(f"\n== {sym}: count={len(evs)} extended={ext} invalid_tick={invalid} "
              f"types={dict(types)} per_min={len(evs) * 60 / seconds:.0f} "
              f"seq_zero_or_none={zero_seq} dup_(time,seq)={dup_keys}")
        for e in evs[:3]:
            print(f"   time={e.time} price={e.price} size={e.size} "
                  f"sequence={e.sequence} index={e.index} type={e.type}")
    return 0 if all(events.get(s) for s in symbols) else 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ,TSLA,NVDA")
    ap.add_argument("--seconds", type=float, default=60)
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main([s.strip() for s in a.symbols.split(",")], a.seconds)))
