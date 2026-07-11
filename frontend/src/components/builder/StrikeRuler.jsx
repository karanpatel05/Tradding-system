import { useEffect, useMemo, useRef } from "react";

// Horizontal strike ruler: one tick per strike, labels every Nth, scrollable and
// auto-centered on the selection. Badge above the active tick shows e.g. "725C"
// (green = BUY leg, red = SELL). Click or drag (pointer capture) to select; snaps
// to real strikes. Thin marker at spot when known.
const TICK_W = 14;

export default function StrikeRuler({ strikes = [], value, right = "C", action = "BUY",
                                      spot = null, onChange }) {
  const wrap = useRef(null);

  const idx = useMemo(() => {
    if (!strikes.length) return -1;
    if (value != null) {
      const i = strikes.indexOf(value);
      if (i >= 0) return i;
    }
    const target = spot ?? strikes[Math.floor(strikes.length / 2)];
    let best = 0;
    strikes.forEach((s, i) => { if (Math.abs(s - target) < Math.abs(strikes[best] - target)) best = i; });
    return best;
  }, [strikes, value, spot]);

  const spotIdx = useMemo(() => {
    if (spot == null || !strikes.length) return -1;
    let best = 0;
    strikes.forEach((s, i) => { if (Math.abs(s - spot) < Math.abs(strikes[best] - spot)) best = i; });
    return best;
  }, [strikes, spot]);

  // Dense labels like the reference UI: every 4th tick is a labeled major
  // (~$4 apart near the ATM dollar strikes, wider in the wings).
  const labelEvery = 4;

  useEffect(() => {
    const el = wrap.current;
    if (el && idx >= 0) el.scrollLeft = idx * TICK_W - el.clientWidth / 2;
  }, [idx, strikes.length]);

  const pick = (clientX) => {
    const el = wrap.current;
    if (!el) return;
    const x = clientX - el.getBoundingClientRect().left + el.scrollLeft;
    const i = Math.max(0, Math.min(strikes.length - 1, Math.round(x / TICK_W - 0.5)));
    if (strikes[i] !== value) onChange(strikes[i]);
  };

  if (!strikes.length) return null;
  return (
    <div className="sruler" ref={wrap}
      onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); pick(e.clientX); }}
      onPointerMove={(e) => { if (e.buttons & 1) pick(e.clientX); }}>
      <div className="sruler-track" style={{ width: strikes.length * TICK_W }}>
        {idx >= 0 && (
          <div className={`sruler-badge ${action === "BUY" ? "buy" : "sell"}`}
            style={{ left: idx * TICK_W + TICK_W / 2 }}>
            {strikes[idx]}{right}
          </div>
        )}
        {spotIdx >= 0 && <div className="sruler-spot" style={{ left: spotIdx * TICK_W + TICK_W / 2 }} />}
        {strikes.map((s, i) => (
          <div key={s} className={`sruler-tick ${i === idx ? "active" : ""}`}
            style={{ left: i * TICK_W }}>
            <div className={`sruler-line ${i % labelEvery === 0 ? "major" : ""}`} />
            {i % labelEvery === 0 && <span className="sruler-label">{s}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
