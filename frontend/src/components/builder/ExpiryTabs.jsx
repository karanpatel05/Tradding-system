import { useEffect, useMemo, useRef } from "react";

// Sleek horizontal date rail: day pills with floating month labels, snap scrolling,
// accent glow on the active pill.
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export default function ExpiryTabs({ expirations = [], value, onChange }) {
  const ref = useRef(null);
  const items = useMemo(() => {
    const thisYear = new Date().getFullYear();
    let lastKey = "";
    return expirations.map((iso) => {
      const [y, m, d] = iso.split("-").map(Number);
      const key = `${y}-${m}`;
      const label = key !== lastKey
        ? `${MON[m - 1]}${y !== thisYear ? ` '${String(y).slice(2)}` : ""}` : null;
      lastKey = key;
      return { iso, day: d, month: label };
    });
  }, [expirations]);

  useEffect(() => {
    ref.current?.querySelector(".xr-pill.active")
      ?.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
  }, [value]);

  if (!expirations.length) return null;
  return (
    <div className="xrail" ref={ref}>
      {items.map(({ iso, day, month }) => (
        <div className="xr-cell" key={iso}>
          <div className="xr-month">{month || " "}</div>
          <button type="button" className={`xr-pill ${iso === value ? "active" : ""}`}
            onClick={() => onChange(iso)}>{day}</button>
        </div>
      ))}
    </div>
  );
}
