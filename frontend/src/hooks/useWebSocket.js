import { useEffect, useRef, useState } from "react";
import { wsUrl } from "../api.js";

// Subscribes to the backend snapshot push channel. Auto-reconnects with backoff.
// Only connects when `enabled` (i.e. authenticated) — the server rejects
// unauthenticated sockets anyway, this just avoids noisy retries on the login screen.
export function useSnapshot(enabled) {
  const [snapshot, setSnapshot] = useState(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);
  const retryRef = useRef(null);

  useEffect(() => {
    if (!enabled) return;
    let closed = false;

    const connect = () => {
      const ws = new WebSocket(wsUrl());
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
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
