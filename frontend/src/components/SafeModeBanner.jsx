import { useState } from "react";
import { api } from "../api.js";

// App-level halt banner. Shown while the engine is in SAFE_MODE (an unreconcilable
// broker state). Resume clears safe mode and re-runs recovery server-side.
export default function SafeModeBanner({ auth, onResume }) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  if (!auth?.safe_mode) return null;

  const resume = async () => {
    setBusy(true);
    try {
      await api.resume();
      await onResume?.();
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <div className="safemode-banner">
      <span className="safemode-text">
        ⚠ SAFE MODE — trading halted: {auth.safe_mode_reason || "unreconcilable broker state"}
      </span>
      {!confirming ? (
        <button className="btn danger" disabled={busy} onClick={() => setConfirming(true)}>
          Resume trading
        </button>
      ) : (
        <>
          <span>Re-check broker state and resume?</span>
          <button className="btn danger" disabled={busy} onClick={resume}>
            {busy ? "Resuming…" : "Confirm resume"}
          </button>
          <button className="btn ghost" disabled={busy} onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </>
      )}
    </div>
  );
}
