import { useState } from "react";
import { api } from "../api.js";

// Prominent emergency control. Cancels all strategy workers and flattens all
// positions. Shows the structured report so the user sees whether exposure
// actually reached zero (fully_flat) — not just "done".
export default function KillSwitch() {
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState(null);
  const [confirming, setConfirming] = useState(false);

  const fire = async () => {
    setBusy(true);
    setReport(null);
    try {
      setReport(await api.kill());
    } catch (err) {
      setReport({ error: err.message });
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <div className="killswitch">
      {!confirming ? (
        <button
          className="btn danger big"
          onClick={() => setConfirming(true)}
          disabled={busy}
        >
          Emergency stop
        </button>
      ) : (
        <div className="kill-confirm">
          <span>Stop all strategies and flatten app-owned positions?</span>
          <button className="btn danger" onClick={fire} disabled={busy}>
            {busy ? "Killing…" : "Confirm"}
          </button>
          <button
            className="btn ghost"
            onClick={() => setConfirming(false)}
            disabled={busy}
          >
            Cancel
          </button>
        </div>
      )}

      {report && !report.error && (
        <div className={`banner ${report.fully_flat ? "ok" : "warn"}`}>
          {report.fully_flat
            ? "✓ Zero exposure — fully flat."
            : "⚠ Partial: not fully flat."}{" "}
          Cancelled {report.tasks_cancelled.length} worker(s).
          {report.flatten?.attempted && (
            <> Flatten: {report.flatten.detail || report.flatten.error}.</>
          )}
          {Object.keys(report.tasks_failed || {}).length > 0 && (
            <> Failed: {Object.keys(report.tasks_failed).join(", ")}.</>
          )}
        </div>
      )}
      {report?.error && (
        <div className="banner error">Kill failed: {report.error}</div>
      )}
    </div>
  );
}
