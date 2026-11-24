import { useEffect, useRef, useState } from "react";
import { wsUrl } from "../api.js";

// Subscribes to the backend snapshot push channel. Auto-reconnects with backoff.
// Only connects when `enabled` (i.e. authenticated) — the server rejects
// unauthenticated sockets anyway, this just avoids noisy retries on the login screen.
// `chartSymbols` is sent to the server as a chart_symbols frame on every (re)open and when the list changes.
export function useSnapshot(enabled, chartSymbols = []) {
  const [snapshot, setSnapshot] = useState(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);
  const retryRef = useRef(null);
  const symbolsRef = useRef(chartSymbols);
  symbolsRef.current = chartSymbols;
  const symbolsKey = chartSymbols.join(",");
  const sendSymbols = (ws) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "chart_symbols", symbols: symbolsRef.current }));
  };

  useEffect(() => { sendSymbols(wsRef.current); }, [symbolsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!enabled) return;
    let closed = false;

    const connect = () => {
      const ws = new WebSocket(wsUrl());
      wsRef.current = ws;
      ws.onopen = () => {
        setConnected(true);
        sendSymbols(ws);
      };
      ws.onmessage = (ev) => {
        try {
          setSnapshot(JSON.parse(ev.data));
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retryRef.current = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, [enabled]);

  return { snapshot, connected };
}
