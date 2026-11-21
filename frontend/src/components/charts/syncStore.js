import { useSyncExternalStore } from "react";

// Shared crosshair time for synced panes. Publishing does not re-render the
// workspace; only panes that read it update, and only when the bar changes.
let time = null, source = null;
const listeners = new Set();

export function publishHover(nextTime, from) {
  if (nextTime === time && from === source) return;
  time = nextTime;
  source = from;
  listeners.forEach(l => l());
}

// Clear the shared time if this pane set it (called when a pane unmounts or remounts).
export function clearHover(from) {
  if (source === from) publishHover(null, null);
}

const subscribe = l => { listeners.add(l); return () => listeners.delete(l); };
const snapshot = () => time;
const sourceSnapshot = () => source;

export function useHoverTime() {
  return [useSyncExternalStore(subscribe, snapshot), useSyncExternalStore(subscribe, sourceSnapshot)];
}
