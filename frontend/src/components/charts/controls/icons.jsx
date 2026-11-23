// Small stroke icons for the chart toolbar and menus (no icon dependency).
const P = {
  chevron: <path d="M6 9l6 6 6-6"/>,
  candles: <><path d="M7 3v18M17 3v18"/><rect x="4.5" y="7" width="5" height="9" fill="currentColor"/><rect x="14.5" y="5" width="5" height="8" fill="currentColor"/></>,
  hollow: <><path d="M7 3v18M17 3v18"/><rect x="4.5" y="7" width="5" height="9"/><rect x="14.5" y="5" width="5" height="8" fill="currentColor"/></>,
  bars: <path d="M7 4v16M4 8h3M7 15h3M17 4v16M14 12h3M17 7h3"/>,
  line: <path d="M3 17l5-6 4 3 8-9"/>,
  area: <><path d="M3 17l5-6 4 3 8-9v16H3z" fill="currentColor" opacity=".25" stroke="none"/><path d="M3 17l5-6 4 3 8-9"/></>,
  ha: <><path d="M7 3v18M17 3v18"/><rect x="4.5" y="8" width="5" height="8" rx="2" fill="currentColor"/><rect x="14.5" y="5" width="5" height="9" rx="2" fill="currentColor"/></>,
  layout: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16M3 12h18"/></>,
  gear: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></>,
  bell: <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0"/>,
  search: <><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></>,
  hline: <path d="M3 12h18"/>,
  external: <path d="M14 4h6v6M10 14L20 4M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>,
  reset: <><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></>,
  image: <><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/></>,
  eye: <><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></>,
  trash: <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6"/>,
  copy: <><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></>,
  lock: <><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></>,
  unlock: <><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.8-1.2"/></>,
  alert: <path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/>,
};
export const TYPE_ICON = {"Candles":"candles","Hollow candles":"hollow","OHLC bars":"bars","Line":"line","Area":"area","Heikin Ashi":"ha"};

export default function Icon({name, size=16}) {
  return <svg className="cx-ico" viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{P[name]}</svg>;
}
