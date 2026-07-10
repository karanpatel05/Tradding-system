import { useEffect, useMemo, useRef } from "react";

// Month-grouped expiry picker: month segments on top, day numbers underneath,
// selected day highlighted. Horizontal scroll; selected day auto-scrolled into view.
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export default function ExpiryTabs({ expirations = [], value, onChange }) {
  const ref = useRef(null);
  const groups = useMemo(() => {
    const out = [];
    const thisYear = new Date().getFullYear();
    let cur = null;
    for (const iso of expirations) {
      const [y, m] = iso.split("-").map(Number);
      const key = `${y}-${m}`;
      if (!cur || cur.key !== key) {
        cur = { key, label: `${MON[m - 1]}${y !== thisYear ? ` '${String(y).slice(2)}` : ""}`, days: [] };
        out.push(cur);
      }
      cur.days.push(iso);
    }
    return out;
  }, [expirations]);

  useEffect(() => {
    ref.current?.querySelector(".xt-day.active")
      ?.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
  }, [value]);

  if (!expirations.length) return null;
  return (
    <div className="xtabs" ref={ref}>
      {groups.map((g) => (
        <div className="xt-month" key={g.key}>
          <div className={`xt-month-label ${g.days.includes(value) ? "active" : ""}`}>{g.label}</div>
          <div className="xt-days">
            {g.days.map((iso) => (
              <button key={iso} type="button"
                className={`xt-day ${iso === value ? "active" : ""}`}
                onClick={() => onChange(iso)}>
                {Number(iso.slice(8))}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
