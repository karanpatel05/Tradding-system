// Shared pieces for the Strategies list and the strategy detail page.
import { useEffect, useState } from "react";
import { api } from "../api.js";

export const usd = (n) =>
  n == null || !Number.isFinite(Number(n))
    ? "—"
    : Number(n).toLocaleString("en-US", { style: "currency", currency: "USD" });
export const signedUsd = (n) =>
  n == null || !Number.isFinite(Number(n)) ? "—" : (n > 0 ? "+" : n < 0 ? "−" : "") + usd(Math.abs(n));
export const pct = (n, digits = 0) =>
  n == null || !Number.isFinite(Number(n)) ? "—" : `${(n * 100).toFixed(digits)}%`;
export const tone = (n) => (n == null ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "");
export const humanize = (s) => (s ? String(s).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "—");
export const isOptions = (s) => /^options/i.test(s?.category || "");

// One status vocabulary for the list, the selected panel and the detail page.
export function strategyStatus(s) {
  if (!s.ready) return { label: "Coming soon", cls: "soon" };
  if (s.status === "exiting") return { label: "Managing exits", cls: "live" };
  if (s.archived) return { label: "Archived", cls: "idle", title: s.activation_blocked };
  if (s.running) return { label: "Running", cls: "live" };
  if (s.external_runner) return { label: "External", cls: "external", title: s.activation_blocked || "Runs outside this app" };
  if (s.activation_blocked) return { label: "Blocked", cls: "blocked", title: s.activation_blocked };
  return { label: "Paused", cls: "idle" };
}
export const canToggle = (s) => s.ready && !s.archived && s.status !== "exiting" && !s.external_runner && (s.running || !s.activation_blocked);

export function StatusMark({ s }) {
  const st = strategyStatus(s);
  return (
    <span className={`st-mark ${st.cls}`} title={st.title}>
      <i aria-hidden="true" />
      {st.label}
    </span>
  );
}

// Start/Pause with an inline confirm; the backend still enforces auth and readiness.
export function ToggleButton({ s, confirming, busy, onAsk, onConfirm, onCancel, primary }) {
  if (!canToggle(s)) return null;
  const verb = s.running ? "Pause" : "Start";
  if (confirming)
    return (
      <span className="strat-confirm">
        <button className={"btn " + (primary ? "" : "tiny ") + (s.running ? "danger" : "primary")} disabled={busy} onClick={onConfirm}>
          {busy ? "Working…" : `Confirm ${verb.toLowerCase()}`}
        </button>
        <button className={"btn ghost" + (primary ? "" : " tiny")} disabled={busy} onClick={onCancel}>Cancel</button>
      </span>
    );
  return (
    <button className={primary ? "btn primary" : "btn ghost tiny"} onClick={onAsk} aria-label={`${verb} ${s.name}`}>
      {primary ? `${verb} strategy` : verb}
    </button>
  );
}

// ---- New York session clock ---------------------------------------------------

const OPEN = 9 * 60 + 30;
const CLOSE = 16 * 60;
const hhmm = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const span = (mins) => {
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m} min`;
};

// First clock time in a rule sentence ("10:30 ET: …", "hold to the 4:00 pm ET …"),
// as minutes after midnight ET. Only times inside the regular session count.
export function parseEtTime(text) {
  const m = /(\d{1,2}):(\d{2})(?:\s*(am|pm))?/i.exec(text || "");
  if (!m) return null;
  let h = Number(m[1]);
  const ap = m[3]?.toLowerCase();
  if (ap === "pm" && h < 12) h += 12;
  if (ap === "am" && h === 12) h = 0;
  const mins = h * 60 + Number(m[2]);
  return mins >= OPEN && mins <= CLOSE ? mins : null;
}
export const scheduleOf = (ov) => ({ entry: parseEtTime(ov?.entry_logic), exit: parseEtTime(ov?.exit_logic) });

function nyNow() {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
      .formatToParts(new Date())
      .map((p) => [p.type, p.value]),
  );
  return { minutes: Number(parts.hour) * 60 + Number(parts.minute), weekend: parts.weekday === "Sat" || parts.weekday === "Sun" };
}
function useNyClock() {
  const [now, setNow] = useState(nyNow);
  // The server's market calendar knows exchange holidays; fall back to weekdays.
  const [tradingDay, setTradingDay] = useState(null);
  useEffect(() => {
    let alive = true;
    const tick = () => setNow(nyNow());
    const check = () =>
      api
        .marketStatus()
        .then((st) => alive && setTradingDay(typeof st?.is_trading_day === "boolean" ? st.is_trading_day : null))
        .catch(() => alive && setTradingDay(null));
    check();
    const t1 = setInterval(tick, 30_000);
    const t2 = setInterval(check, 300_000);
    return () => {
      alive = false;
      clearInterval(t1);
      clearInterval(t2);
    };
  }, []);
  return { minutes: now.minutes, tradingDay: tradingDay ?? !now.weekend };
}

const at = (m) => `${((m - OPEN) / (CLOSE - OPEN)) * 100}%`;
const TICKS = [OPEN, 660, 720, 780, 840, 900, CLOSE];

// The regular session drawn to scale: when the strategy enters, when it is out,
// and where New York is right now. Holidays and half days are not modelled.
export function SessionRail({ entry, exit, compact = false }) {
  const { minutes, tradingDay } = useNyClock();
  if (entry == null && exit == null) return null;
  const open = tradingDay && minutes >= OPEN && minutes < CLOSE;
  const hold = entry != null && exit != null && exit > entry;
  let next;
  if (!tradingDay) next = "Market closed today";
  else if (!open) next = "Market closed";
  else if (entry != null && minutes < entry) next = `Entry at ${hhmm(entry)}, in ${span(entry - minutes)}`;
  else if (exit != null && minutes < exit) next = `Exit at ${hhmm(exit)}, in ${span(exit - minutes)}`;
  else next = "Done for the session";

  return (
    <figure className={"rail" + (compact ? " compact" : "")} aria-label={`Session schedule. ${next}`}>
      <figcaption className="rail-head">
        <span>New York session, regular hours</span>
        <span className="rail-next">{next}</span>
      </figcaption>
      <div className="rail-plot">
        <div className="rail-track" />
        {open && <div className="rail-elapsed" style={{ width: at(minutes) }} />}
        {hold && <div className="rail-hold" style={{ left: at(entry), width: `calc(${at(exit)} - ${at(entry)})` }} />}
        {entry != null && (
          <div className={"rail-mark entry" + (entry <= OPEN + 30 ? " start" : "")} style={{ left: at(entry) }}>
            <span>Entry {hhmm(entry)}</span>
          </div>
        )}
        {exit != null && (
          <div className={"rail-mark exit" + (exit >= CLOSE - 20 ? " end" : "")} style={{ left: at(exit) }}>
            <span>Exit {hhmm(exit)}</span>
          </div>
        )}
        {open && (
          <div className="rail-now" style={{ left: at(minutes) }}>
            <span>{hhmm(minutes)}</span>
          </div>
        )}
      </div>
      {!compact && (
        <div className="rail-axis" aria-hidden="true">
          {TICKS.map((t) => (
            <span key={t} style={{ left: at(t) }}>{hhmm(t)}</span>
          ))}
        </div>
      )}
    </figure>
  );
}

export function ChevronLeft() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m15 18-6-6 6-6" />
    </svg>
  );
}
