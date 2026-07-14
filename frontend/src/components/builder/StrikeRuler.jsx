import { useEffect, useMemo, useRef } from "react";

// Horizontal strike ruler: one tick per strike, labels every Nth, scrollable and
// auto-centered on the selection. EVERY leg gets a badge at its strike ("725C",
// green = BUY, red = SELL); legs sharing a strike stack vertically; clicking a
// badge selects that leg. Click/drag on the ruler moves the SELECTED leg's strike
// (snapping). Thin marker at spot when known.
const TICK_W = 14;
const BADGE_H = 26;

export default function StrikeRuler({ strikes = [], value, right = "C", action = "BUY",
                                      spot = null, onChange, legs = [], selected = null,
                                      onSelect }) {
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

  // One badge per leg (fallback: a single badge from value/right/action when no legs
  // exist yet). Legs on the same strike stack upward; `level` is the stack row.
  const badges = useMemo(() => {
    const src = legs.length
      ? legs.map((l, i) => ({ strike: l.strike, right: l.right, action: l.action, i }))
      : (idx >= 0 ? [{ strike: strikes[idx], right, action, i: -1 }] : []);
    const perStrike = {};
    return src.map((b) => {
      let bi = strikes.indexOf(b.strike);
      if (bi < 0) {
        bi = 0;
        strikes.forEach((s, j) => { if (Math.abs(s - b.strike) < Math.abs(strikes[bi] - b.strike)) bi = j; });
      }
      const level = perStrike[bi] || 0;
      perStrike[bi] = level + 1;
      return { ...b, x: bi * TICK_W + TICK_W / 2, level };
    });
  }, [legs, strikes, idx, right, action]);
  const maxLevel = badges.reduce((m, b) => Math.max(m, b.level), 0);

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
    <div className="sruler" ref={wrap} style={{ paddingTop: 34 + maxLevel * BADGE_H }}
      onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); pick(e.clientX); }}
      onPointerMove={(e) => { if (e.buttons & 1) pick(e.clientX); }}>
      <div className="sruler-track" style={{ width: strikes.length * TICK_W }}>
        {badges.map((b) => (
          <div key={`${b.i}:${b.strike}${b.right}`}
            className={`sruler-badge ${b.action === "BUY" ? "buy" : "sell"} ${
              legs.length ? (b.i === selected ? "sel" : "dim") : ""}`}
            style={{ left: b.x, top: -30 - b.level * BADGE_H }}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => { e.stopPropagation(); if (b.i >= 0) onSelect?.(b.i); }}>
            {b.strike}{b.right}
          </div>
        ))}
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
