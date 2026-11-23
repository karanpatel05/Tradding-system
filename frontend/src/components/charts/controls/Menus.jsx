import { Popover, Switch } from "./Overlay.jsx";
import Icon, { TYPE_ICON } from "./icons.jsx";

export const INTERVAL_LABELS = {"1m":"1 minute","5m":"5 minutes","15m":"15 minutes","30m":"30 minutes","1h":"1 hour","4h":"4 hours","1D":"1 day","1W":"1 week"};
const DERIVED = new Set(["30m","1h"]);

export function TimeframeMenu({open, onClose, anchorRef, value, favorites, onPick, onToggleFavorite}) {
  const row = t => <div key={t} className={"cx-menu-row"+(value===t?" current":"")}>
    <button role="menuitem" className="cx-menu-main" onClick={()=>{onPick(t);onClose();}}>{INTERVAL_LABELS[t]}{DERIVED.has(t) && <small>from 15m</small>}</button>
    <button className={"cx-star"+(favorites.includes(t)?" on":"")} aria-pressed={favorites.includes(t)} aria-label={(favorites.includes(t)?"Unpin ":"Pin ")+INTERVAL_LABELS[t]+" to toolbar"} onClick={()=>onToggleFavorite(t)}>★</button>
  </div>;
  return <Popover open={open} onClose={onClose} anchorRef={anchorRef} label="Timeframe" className="cx-menu w-220">
    <div className="cx-menu-label">Minutes</div>{["1m","5m","15m","30m"].map(row)}
    <div className="cx-menu-label sep">Hours and days</div>{["1h","4h","1D","1W"].map(row)}
  </Popover>;
}

export const CHART_TYPES = [["Candles","▮"],["Hollow candles","▯"],["OHLC bars","╫"],["Line","⟋"],["Area","◿"],["Heikin Ashi","≈"]];
export function ChartTypeMenu({open, onClose, anchorRef, value, onPick}) {
  return <Popover open={open} onClose={onClose} anchorRef={anchorRef} label="Chart type" className="cx-menu w-200">
    {CHART_TYPES.map(([t])=><button key={t} role="menuitemcheckbox" aria-checked={value===t} className={"cx-menu-item"+(value===t?" current":"")} onClick={()=>{onPick(t);onClose();}}>
      <span className="cx-menu-icon"><Icon name={TYPE_ICON[t]}/></span><span className="grow">{t}</span>{value===t && <span aria-hidden="true">✓</span>}</button>)}
  </Popover>;
}

const LAYOUT_CELLS = {1:[1,1],2:[2,1],4:[2,2],8:[4,2]};
export function LayoutMenu({open, onClose, anchorRef, count, panes, start, onCount, onLink, sync, onSync}) {
  return <Popover open={open} onClose={onClose} anchorRef={anchorRef} align="end" label="Layout" className="cx-menu cx-layout-menu">
    <div className="cx-layouts">{[1,2,4,8].map(n=>{const [c,r]=LAYOUT_CELLS[n];return <button key={n} role="menuitemcheckbox" aria-checked={count===n} className={count===n?"current":""} onClick={()=>onCount(n)}>
      <span className="cx-layout-preview" style={{gridTemplateColumns:`repeat(${c},1fr)`,gridTemplateRows:`repeat(${r},1fr)`}}>{Array.from({length:n},(_,i)=><i key={i}/>)}</span>
      {n} chart{n>1?"s":""}</button>;})}</div>
    {count>1 && <div className="cx-link-list">{Array.from({length:count},(_,k)=>{const i=start+k, p=panes[i];return <div key={i} className="cx-link-row">
      <b>Chart {k+1}</b><span>{p.symbol} · {p.interval}</span>
      <select aria-label={"Symbol link for chart "+(k+1)} value={p.group} onChange={e=>onLink(i,e.target.value)}><option value="">Unlinked</option><option value="A">Link A</option><option value="B">Link B</option></select></div>;})}</div>}
    <div className="cx-menu-switch"><span>Sync crosshair and time across panes</span><Switch checked={sync} onChange={onSync} label="Sync crosshair across panes"/></div>
  </Popover>;
}

export function ContextMenu({at, onClose, symbol, price, onAlert, onLine, onBuilder, onReset, onCopy, drawingsHidden, onToggleDrawings, hasDrawings, onRemoveDrawings, onSettings}) {
  const item = (label, fn, opts={}) => <button role="menuitem" className={"cx-menu-item"+(opts.danger?" danger":"")} disabled={opts.disabled} onClick={()=>{onClose();fn();}}>
    <span className="cx-menu-icon" aria-hidden="true">{opts.icon}</span><span className="grow">{label}</span>{opts.key && <kbd>{opts.key}</kbd>}</button>;
  return <Popover open={!!at} onClose={onClose} at={at} label="Chart menu" className="cx-menu w-260">
    <div className="cx-menu-label">{symbol} at {price?.toFixed(2)}</div>
    {item(`Add alert at ${price?.toFixed(2)}`, onAlert, {icon:"⏰", key:"Alt A"})}
    {item("Draw horizontal line here", onLine, {icon:"━"})}
    {item("Open in options builder", onBuilder, {icon:"⧉", disabled:!onBuilder})}
    <div className="cx-menu-sep"/>
    {item("Reset view", onReset, {icon:"↺", key:"Alt R"})}
    {item("Copy chart image", onCopy, {icon:"⎘"})}
    {item(drawingsHidden?"Show all drawings":"Hide all drawings", onToggleDrawings, {icon:"◉"})}
    <div className="cx-menu-sep"/>
    {item("Remove all drawings", onRemoveDrawings, {icon:"🗑", danger:true, disabled:!hasDrawings})}
    {item("Chart settings", onSettings, {icon:"⚙"})}
  </Popover>;
}
