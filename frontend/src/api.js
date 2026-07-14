// Central API client. All requests send the session cookie (credentials:
// "include") so the server-side auth gate sees it. The session token itself is
// never exposed to JS — only the opaque HttpOnly cookie the browser holds.

// Vite dev server (port 5173) must reach the API on :8000; a production build is
// served by the backend itself (packaged desktop app), so it's same-origin ("").
// `import.meta.env.DEV` is statically replaced at build time, so the dev URL is
// dead-code-eliminated from the bundle.
const API_BASE = import.meta.env.DEV ? "http://localhost:8000" : "";

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
  // IBKR Gateway connection state: {connected, host, port, client_id, account, accounts, detail}.
  authStatus: () => request("/api/auth/status"),
  connect: () => request("/api/auth/connect", { method: "POST" }),
  disconnect: () => request("/api/auth/disconnect", { method: "POST" }),
  // Tastytrade market-data creds (OAuth personal grant).
  ttStatus: () => request("/api/auth/tastytrade"),
  ttSave: (body) =>
    request("/api/auth/tastytrade", { method: "PUT", body: JSON.stringify(body) }),
  resume: () => request("/api/system/resume", { method: "POST" }),

  account: () => request("/api/account"),
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
  // How many of the whole spread to open per entry (Triple Calendar).
  setContracts: (key, contracts) =>
    request(`/api/strategies/${key}/contracts`, {
      method: "PUT",
      body: JSON.stringify({ contracts }),
    }),
  // Multi-ticker Triple Calendar: set the full ticker list [{ticker, contracts}].
  setTickers: (key, tickers) =>
    request(`/api/strategies/${key}/tickers`, {
      method: "PUT",
      body: JSON.stringify({ tickers }),
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
  // Options-aware detail for a multi-leg strategy (Triple Calendar): legs, DTE, net
  // debit, live value/P&L, target/stop progress, breach distances + operational status.
  optionsPosition: (key) => request(`/api/strategies/${key}/options-position`),
  // Options Strategy Builder (tracked paper trades — no orders transmitted).
  builderChain: (underlying) => request(`/api/builder/chain?underlying=${encodeURIComponent(underlying)}`),
  // Header quote: last price + day change vs previous close.
  builderSpot: (underlying) => request(`/api/builder/spot?underlying=${encodeURIComponent(underlying)}`),
  // Strikes available for ONE expiry (the ruler shows only these).
  builderChainStrikes: (underlying, expiry) =>
    request(`/api/builder/chain-strikes?underlying=${encodeURIComponent(underlying)}&expiry=${expiry}`),
  builderChainQuotes: (underlying, expiry, count) =>
    request(`/api/builder/chain-quotes?underlying=${encodeURIComponent(underlying)}&expiry=${expiry}&count=${count}`),
  builderQuote: (underlying, legs, spot, iv) =>
    request(`/api/builder/quote`, { method: "POST", body: JSON.stringify({ underlying, legs, spot, iv: Number(iv) || 0.25 }) }),
  builderPayoff: (body) =>
    request(`/api/builder/payoff`, { method: "POST", body: JSON.stringify(body) }),
  builderSaveTrade: (body) =>
    request(`/api/builder/trades`, { method: "POST", body: JSON.stringify(body) }),
  builderTrades: (status) =>
    request(`/api/builder/trades${status ? `?status=${status}` : ""}`),
  builderTrade: (id) => request(`/api/builder/trades/${id}`),
  builderCloseTrade: (id) =>
    request(`/api/builder/trades/${id}/close`, { method: "POST" }),
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
  // Packaged build uses VITE_API_BASE="" (same-origin) -> derive from the page.
  if (!API_BASE) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws`;
  }
  return API_BASE.replace(/^http/, "ws") + "/ws";
}
