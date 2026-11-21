import { useEffect, useState } from "react";
import { Dialog, Segmented, Switch, Swatches } from "./Overlay.jsx";
import { CHART_TYPES } from "./Menus.jsx";
import { CONDITIONS, sessionCloseAfter, unlockSound } from "../alerts.js";

export const DEFAULT_SETTINGS = {
  type: "Candles", scale: "auto", up: "#2dd4a7", down: "#f0657a", wickUp: "#2dd4a7", wickDown: "#f0657a",
  grid: true, lastLine: true, countdown: true, sessionBreaks: false,
  positionLine: true, crossMarkers: true, syncCrosshair: true, dataWindow: "follow",
};

const Row = ({label, children}) => <div className="cx-form-row"><span>{label}</span>{children}</div>;

// Candles, scales, canvas and trading overlays. Edits a draft; Save applies.
export function ChartSettingsDialog({open, onClose, settings, onSave}) {
  const [tab,setTab]=useState("Candles");
  const [d,setD]=useState(settings);
  useEffect(()=>{ if(open) setD(settings); },[open]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = patch => setD(old=>({...old,...patch}));
  return <Dialog open={open} onClose={onClose} width={520} title="Chart settings"
    footer={<><button className="cx-link-btn grow" onClick={()=>setD(DEFAULT_SETTINGS)}>Restore dark defaults</button>
      <button onClick={onClose}>Cancel</button><button className="primary" onClick={()=>{onSave(d);onClose();}}>Save</button></>}>
    <div className="cx-split">
      <nav className="cx-split-nav" aria-label="Settings sections">{["Candles","Scales","Canvas","Trading"].map(t=><button key={t} aria-pressed={tab===t} onClick={()=>setTab(t)}>{t}</button>)}</nav>
      <div className="cx-split-body cx-form">
        {tab==="Candles" && <>
          <Row label="Chart type"><select aria-label="Chart type" value={d.type} onChange={e=>set({type:e.target.value})}>{CHART_TYPES.map(([t])=><option key={t}>{t}</option>)}</select></Row>
          <Row label="Up body"><Swatches label="Up body color" value={d.up} onChange={c=>set({up:c})}/></Row>
          <Row label="Down body"><Swatches label="Down body color" value={d.down} onChange={c=>set({down:c})}/></Row>
          <Row label="Up wick"><Swatches label="Up wick color" value={d.wickUp} onChange={c=>set({wickUp:c})}/></Row>
          <Row label="Down wick"><Swatches label="Down wick color" value={d.wickDown} onChange={c=>set({wickDown:c})}/></Row>
        </>}
        {tab==="Scales" && <>
          <Row label="Price scale"><Segmented label="Price scale" value={d.scale} options={[["auto","Auto"],["log","Log"],["percent","%"]]} onChange={v=>set({scale:v})}/></Row>
          <Row label="Last price line"><Switch label="Last price line" checked={d.lastLine} onChange={v=>set({lastLine:v})}/></Row>
          <Row label="Countdown to bar close"><Switch label="Countdown to bar close" checked={d.countdown} onChange={v=>set({countdown:v})}/></Row>
          <p className="cx-muted">The countdown shows on live intraday candles only.</p>
        </>}
        {tab==="Canvas" && <>
          <Row label="Grid lines"><Switch label="Grid lines" checked={d.grid} onChange={v=>set({grid:v})}/></Row>
          <Row label="Session breaks"><Switch label="Session breaks" checked={d.sessionBreaks} onChange={v=>set({sessionBreaks:v})}/></Row>
          <Row label="Sync crosshair across panes"><Switch label="Sync crosshair across panes" checked={d.syncCrosshair} onChange={v=>set({syncCrosshair:v})}/></Row>
          <Row label="Data window"><Segmented label="Data window" value={d.dataWindow} options={[["follow","Follow"],["pinned","Pinned"],["off","Off"]]} onChange={v=>set({dataWindow:v})}/></Row>
        </>}
        {tab==="Trading" && <>
          <Row label="Position line"><Switch label="Position line" checked={d.positionLine} onChange={v=>set({positionLine:v})}/></Row>
          <Row label="EMA crossover markers"><Switch label="EMA crossover markers" checked={d.crossMarkers} onChange={v=>set({crossMarkers:v})}/></Row>
          <p className="cx-muted">The position line comes from your Alpaca paper position and shows only over live candles.</p>
        </>}
      </div>
    </div>
  </Dialog>;
}

// New price alert. Checked against live quotes while this chart is open; notify only.
export function AlertDialog({open, onClose, symbol, price, onCreate}) {
  const [condition,setCondition]=useState("crossing_up");
  const [value,setValue]=useState("");
  const [trigger,setTrigger]=useState("once");
  const [message,setMessage]=useState("");
  const [inApp,setInApp]=useState(true);
  const [sound,setSound]=useState(true);
  const [expiry,setExpiry]=useState("session");
  useEffect(()=>{ if(open){ setValue(price!=null?price.toFixed(2):""); setMessage(""); } },[open, price]);
  const v=+value, valid=value!=="" && Number.isFinite(v) && v>0 && (inApp||sound);
  const label=(CONDITIONS.find(c=>c[0]===condition)||[])[1];
  const auto=`${symbol} ${label?.toLowerCase()} ${valid?v.toFixed(2):"…"}`;
  return <Dialog open={open} onClose={onClose} width={440} title={`New alert on ${symbol}`}
    footer={<><span className="cx-warn grow">Alerts notify only and never place orders</span>
      <button className="primary" disabled={!valid} onClick={()=>{
        if(sound) unlockSound();
        onCreate({id:globalThis.crypto?.randomUUID?.()||String(Date.now()), symbol, condition, value:v, trigger, message:message.trim()||auto,
          inApp, sound, expiry, expires: expiry==="session" ? sessionCloseAfter() : null, active:true, status:"Active", created:Date.now()});
        onClose();
      }}>Create</button></>}>
    <div className="cx-form">
      <div className="cx-alert-cond">
        <select aria-label="Condition" value={condition} onChange={e=>setCondition(e.target.value)}>{CONDITIONS.map(([c,t])=><option key={c} value={c}>{t}</option>)}</select>
        <input data-autofocus type="number" step="0.01" aria-label="Price" value={value} onChange={e=>setValue(e.target.value)}/>
      </div>
      <Row label="Trigger"><Segmented label="Trigger" value={trigger} options={[["once","Once"],["every","Every time"]]} onChange={setTrigger}/></Row>
      <Row label="Expires"><Segmented label="Expires" value={expiry} options={[["session","End of session"],["never","Never"]]} onChange={setExpiry}/></Row>
      <label className="cx-field">Message<input value={message} placeholder={auto} onChange={e=>setMessage(e.target.value)}/></label>
      <div className="cx-form-row"><span className="cx-inline"><Switch label="In-app notification" checked={inApp} onChange={setInApp}/>In-app</span><span className="cx-inline"><Switch label="Sound" checked={sound} onChange={setSound}/>Sound</span></div>
      <p className="cx-muted">Checked against live quotes about every 15 seconds while this chart is open in this browser. Sample or stale data never triggers alerts.</p>
    </div>
  </Dialog>;
}
