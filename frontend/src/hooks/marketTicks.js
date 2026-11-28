import { wsUrl } from "../api.js";

// The market endpoint accepts up to 20 symbols. All panes on a page share these
// sockets, including two charts showing the same symbol at different intervals.
const MAX_SYMBOLS = 20;
const listeners = new Set();
const channels = new Map();
let reconcilePending = false;

function publishStatus() {
  const ready = new Set();
  for (const channel of channels.values()) {
    if (channel.connected) for (const symbol of channel.symbols) ready.add(symbol);
  }
  for (const listener of listeners) {
    const connected = listener.symbols.every(symbol => ready.has(symbol));
    if (connected !== listener.connected) {
      listener.connected = connected;
      listener.onStatus(connected);
    }
  }
}

function openChannel(key, symbols) {
  const channel = { symbols, connected: false, socket: null, timer: null };
  channels.set(key, channel);
  let backoff = 1000;
  let pending = [];
  let frame = null;

  function flush() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    const batch = pending;
    pending = [];
    if (!batch.length) return;
    for (const listener of listeners) {
      const matches = batch.filter(tick => listener.wanted.has(tick.symbol));
      if (matches.length) listener.onTicks(matches);
    }
  }

  function connect() {
    const socket = new WebSocket(wsUrl() + "/market?symbols=" + encodeURIComponent(key));
    channel.socket = socket;
    socket.onmessage = ({ data }) => {
      if (channels.get(key) !== channel) return;
      let message;
      try { message = JSON.parse(data); } catch { return; }
      if (message?.type === "status") {
        flush();
        channel.connected = message.connected === true;
        if (channel.connected) backoff = 1000;
        publishStatus();
      } else if (channel.connected && message?.type === "ticks" && Array.isArray(message.ticks)) {
        for (const tick of message.ticks) {
          if (tick && symbols.includes(tick.symbol) && Number.isSafeInteger(tick.time)
              && tick.time > 0 && Number.isFinite(tick.price) && tick.price > 0
              && Number.isFinite(tick.size) && tick.size > 0) pending.push(tick);
        }
        // Keep every print but update React at display speed. Bound the buffer
        // when the browser suspends animation frames in a background tab.
        if (pending.length >= 1024) flush();
        else if (pending.length && frame === null) frame = requestAnimationFrame(flush);
      }
    };
    socket.onclose = () => {
      if (channels.get(key) !== channel) return;
      flush();
      channel.connected = false;
      publishStatus();
      channel.timer = setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 15000);
    };
    socket.onerror = () => socket.close();
  }
  channel.stop = () => {
    clearTimeout(channel.timer);
    flush();
    channel.socket.close();
  };
  connect();
}

function scheduleSubscriptions() {
  if (reconcilePending) return;
  reconcilePending = true;
  queueMicrotask(() => {
    reconcilePending = false;
    const symbols = [...new Set([...listeners].flatMap(listener => listener.symbols))].sort();
    const groups = new Map();
    for (let i = 0; i < symbols.length; i += MAX_SYMBOLS) {
      const group = symbols.slice(i, i + MAX_SYMBOLS);
      groups.set(group.join(","), group);
    }
    for (const [key, channel] of channels) {
      if (!groups.has(key)) {
        channels.delete(key);
        channel.stop();
      }
    }
    for (const [key, group] of groups) {
      if (!channels.has(key)) openChannel(key, group);
    }
    publishStatus();
  });
}

export function subscribeMarketTicks(symbols, onTicks, onStatus) {
  if (!symbols.length) {
    onStatus(false);
    return () => {};
  }
  const listener = { symbols, wanted: new Set(symbols), onTicks, onStatus, connected: undefined };
  listeners.add(listener);
  scheduleSubscriptions();
  return () => {
    listeners.delete(listener);
    scheduleSubscriptions();
  };
}
