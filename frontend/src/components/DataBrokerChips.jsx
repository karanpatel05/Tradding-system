import { useEffect, useState } from "react";
import { api } from "../api.js";

// Dashboard header chips separating the two health signals that used to be conflated:
// Data (which market-data source is actually serving) and Broker (orders), plus the
// order-routing mode (paper vs LIVE — the ENABLE_LIVE_ORDERS master gate).
const DATA_LABEL = { tastytrade: "Tastytrade" };

export default function DataBrokerChips() {
  const [tt, setTt] = useState(null);
  const [auth, setAuth] = useState(null);
  const [health, setHealth] = useState(null);

  useEffect(() => {
    const load = () => {
      api.ttStatus().then(setTt).catch(() => setTt(null));
      api.authStatus().then(setAuth).catch(() => setAuth(null));
      api.health().then(setHealth).catch(() => setHealth(null));
    };
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  const dataTone = tt == null ? "idle"
    : tt.serving === "tastytrade" ? "live"
    : tt.configured ? "idle" : "down";
  const dataText = tt == null ? "…"
    : tt.serving === "tastytrade" ? DATA_LABEL[tt.serving]
    : tt.configured ? "Tastytrade · idle" : "not configured";

  const brokerReady = tt?.probe === "ok";
  const brokerText = tt == null ? "…"
    : auth?.conn_state === "safe_mode" ? "SAFE MODE"
    : brokerReady ? "Tastytrade verified"
    : "not verified";

  const liveOrders = !!health?.live_orders_enabled;

  return (
    <div className="src-chips">
      <span className={`pill tiny ${dataTone}`}>Data: {dataText}</span>
      <span className={`pill tiny ${brokerReady ? "live" : "down"}`}>Broker: {brokerText}</span>
      <span className={`pill tiny ${liveOrders ? "down" : "idle"}`}>
        Orders: {liveOrders ? "LIVE" : "PAPER"}
      </span>
    </div>
  );
}
