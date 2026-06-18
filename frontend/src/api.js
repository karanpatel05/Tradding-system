// Central API client. All requests send the session cookie (credentials:
// "include") so the server-side auth gate sees it. The session token itself is
// never exposed to JS — only the opaque HttpOnly cookie the browser holds.

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch {
      detail = res.statusText;
    }
    const err = new Error(typeof detail === "string" ? detail : "Request failed");
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  base: API_BASE,
  authStatus: () => request("/api/auth/status"),
  login: (refreshToken, providerSecret) =>
    request("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        refresh_token: refreshToken,
        // Omit when empty so the server falls back to its configured secret.
        provider_secret: providerSecret?.trim() ? providerSecret.trim() : undefined,
      }),
    }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  // Current connection + stored credentials for the API settings tab.
  getCredentials: () => request("/api/auth/credentials"),
  // Persist secret + refresh token locally AND try to connect. Saves even when the
  // connection fails (so values aren't lost on reload); returns {connected, detail, ...}.
  saveCredentials: (providerSecret, refreshToken) =>
    request("/api/auth/credentials", {
      method: "POST",
      body: JSON.stringify({
        refresh_token: refreshToken,
        provider_secret: providerSecret?.trim() ? providerSecret.trim() : undefined,
      }),
    }),

  marketStatus: () => request("/api/market/status"),
  marketOverview: () => request("/api/market/overview"),
  positions: () => request("/api/positions"),
  kill: () => request("/api/kill", { method: "POST" }),

  strategies: () => request("/api/strategies"),
  strategy: (key) => request(`/api/strategies/${key}`),
  activate: (key) => request(`/api/strategies/${key}/activate`, { method: "POST" }),
  stop: (key) => request(`/api/strategies/${key}/stop`, { method: "POST" }),
  allocate: (key, amount) =>
    request(`/api/strategies/${key}/allocation`, {
      method: "PUT",
      body: JSON.stringify({ amount }),
    }),
  // Multi-position crossover: set the live trade-direction mode (long/short/both).
  setDirection: (key, direction) =>
    request(`/api/strategies/${key}/direction`, {
      method: "PUT",
      body: JSON.stringify({ direction }),
    }),
  // All open positions for a (multi-position) strategy, and the live risk snapshot.
  strategyPositions: (key) => request(`/api/strategies/${key}/positions`),
  risk: (key) => request(`/api/strategies/${key}/risk`),
  backtest: (key, cfg = {}) => {
    const q = new URLSearchParams();
    Object.entries(cfg).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") q.set(k, v);
    });
    const qs = q.toString();
    return request(`/api/strategies/${key}/backtest${qs ? `?${qs}` : ""}`);
  },
  // The last persisted backtest result (reloads after a page/server restart).
  backtestLast: (key) => request(`/api/strategies/${key}/backtest/last`),
  // Live detail/metrics for the strategy's open position (TP/SL/fill/live price).
  position: (key) => request(`/api/strategies/${key}/position`),
  // Streaming backtest: reads NDJSON progress lines, calls onProgress({scanned,
  // total, symbol}) as the universe is scanned, and resolves with the final result.
  backtestStream: async (key, cfg = {}, onProgress) => {
    const q = new URLSearchParams();
    Object.entries(cfg).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") q.set(k, v);
    });
    const qs = q.toString();
    const res = await fetch(`${API_BASE}/api/strategies/${key}/backtest/stream${qs ? `?${qs}` : ""}`, {
      credentials: "include",
      headers: { "Content-Type": "application/json" },
    });
    if (!res.ok || !res.body) {
      let detail;
      try { detail = (await res.json()).detail; } catch { detail = res.statusText; }
      const err = new Error(typeof detail === "string" ? detail : "Backtest failed");
      err.status = res.status;
      throw err;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let result = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        const msg = JSON.parse(line);
        if (msg.type === "progress") onProgress?.(msg);
        else if (msg.type === "result") result = msg.result;
        else if (msg.type === "error") throw new Error(msg.detail || "Backtest failed");
      }
    }
    return result;
  },
  // Streaming optimizer: POSTs a grid/objective, reads NDJSON progress lines while
  // the watchlist history is fetched, resolves with the final ranked result.
  optimizeStream: async (key, body = {}, onProgress) => {
    const res = await fetch(`${API_BASE}/api/strategies/${key}/optimize/stream`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) {
      let detail;
      try { detail = (await res.json()).detail; } catch { detail = res.statusText; }
      const err = new Error(typeof detail === "string" ? detail : "Optimization failed");
      err.status = res.status;
      throw err;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let result = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        const msg = JSON.parse(line);
        if (msg.type === "progress") onProgress?.(msg);
        else if (msg.type === "result") result = msg.result;
        else if (msg.type === "error") throw new Error(msg.detail || "Optimization failed");
      }
    }
    return result;
  },
  health: () => request("/health"),
  selectionHistory: (key, cfg = {}) => {
    const q = new URLSearchParams();
    Object.entries(cfg).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") q.set(k, v);
    });
    const qs = q.toString();
    return request(`/api/strategies/${key}/selection-history${qs ? `?${qs}` : ""}`);
  },

  calendar: (start, end) => {
    const q = new URLSearchParams();
    if (start) q.set("start", start);
    if (end) q.set("end", end);
    return request(`/api/journal/calendar?${q.toString()}`);
  },
  dayDetail: (day) => request(`/api/journal/day/${day}`),
  logs: (limit = 100) => request(`/api/logs?limit=${limit}`),
};

export function wsUrl() {
  return API_BASE.replace(/^http/, "ws") + "/ws";
}
