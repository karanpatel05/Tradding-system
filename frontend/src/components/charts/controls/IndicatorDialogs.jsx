import { useState } from "react";
import { Dialog, Segmented, Switch, Swatches } from "./Overlay.jsx";
import { MULTI, STUDY_CATALOG, STUDY_DEFAULTS, newStudy, studyLabel } from "../chartData.js";

const CATEGORIES = ["Trend","Momentum","Volume","Volatility","My presets"];

// Library: add studies to the active pane, manage what is on it, save/load a preset.
export function IndicatorsDialog({open, onClose, pane, onChange, preset, onSavePreset, onLoadPreset, onEdit, onApplyAll}) {
  const [query,setQuery]=useState("");
  const [cat,setCat]=useState("Trend");
  const studies = pane.studies;
  const q = query.trim().toLowerCase();
  const list = q ? STUDY_CATALOG.filter(s=>(s.type+" "+s.desc).toLowerCase().includes(q)) : STUDY_CATALOG.filter(s=>s.category===cat);
  const onChart = type => studies.filter(s=>s.type===type).length;
  return <Dialog open={open} onClose={onClose} width={560} title={`Indicators · ${pane.symbol} ${pane.interval}`}
    footer={<><span className="cx-muted grow">On chart: {studies.map(studyLabel).join(", ") || "none"}</span>
      <button onClick={onApplyAll}>Apply to all panes</button><button onClick={onSavePreset}>Save preset</button></>}>
    <div className="cx-search-input"><span aria-hidden="true">⌕</span><input data-autofocus value={query} onChange={e=>setQuery(e.target.value)} placeholder="Search indicators" aria-label="Search indicators"/></div>
    <div className="cx-split">
      <nav className="cx-split-nav" aria-label="Categories">{CATEGORIES.map(c=><button key={c} aria-pressed={!q && cat===c} onClick={()=>{setCat(c);setQuery("");}}>{c}</button>)}</nav>
      <div className="cx-split-body">
        {!q && cat==="My presets" ? (preset
          ? <div className="cx-lib-row"><div><b>Saved preset</b><small>{preset.map(studyLabel).join(", ")}</small></div><button onClick={onLoadPreset}>Load</button></div>
          : <p className="cx-muted cx-pad">No preset yet. Set up a pane and choose Save preset.</p>)
        : list.map(s=>{
          const n=onChart(s.type), canAdd=MULTI.has(s.type) || n===0;
          return <div key={s.type} className="cx-lib-row"><div><b>{s.type}</b><small>{s.desc}</small></div>
            <div className="cx-lib-actions">{n>0 && <span className="cx-pill">{n>1?n+" on chart":"On chart"}</span>}
              <button disabled={!canAdd} onClick={()=>{const st=newStudy(s.type); onChange([...studies, st]);}}>Add</button></div></div>;
        })}
      </div>
    </div>
    {studies.length>0 && <div className="cx-active-list" aria-label="Studies on this pane">{studies.map(s=><div key={s.id} className="cx-active-row">
      <Switch checked={s.visible} onChange={v=>onChange(studies.map(x=>x.id===s.id?{...x,visible:v}:x))} label={"Show "+studyLabel(s)}/>
      <i style={{background:s.color||"#9aa7b6"}}/><span className="grow">{studyLabel(s)}</span>
      {s.type!=="Volume" && <button onClick={()=>onEdit(s.id)}>Settings</button>}
      <button className="danger" onClick={()=>onChange(studies.filter(x=>x.id!==s.id))}>Remove</button></div>)}</div>}
  </Dialog>;
}

function NumberField({value, onChange, min=1, max=500, stepBy=1, label}) {
  const set = v => onChange(Math.max(min, Math.min(max, Math.round(v/stepBy)*stepBy)));
  return <div className="cx-number"><input type="number" aria-label={label} min={min} max={max} step={stepBy} value={value} onChange={e=>e.target.value!=="" && set(+e.target.value)}/>
    <button aria-label={"Decrease "+label} onClick={()=>set(value-stepBy)}>−</button><button aria-label={"Increase "+label} onClick={()=>set(value+stepBy)}>+</button></div>;
}

// Settings for one study: inputs, style and visibility. Changes apply live.
export function StudySettingsDialog({open, onClose, study, onChange, onApplyAll}) {
  const [tab,setTab]=useState("Inputs");
  if (!study) return null;
  const set = patch => onChange({...study, ...patch});
  const hasLength = ["EMA","SMA","RSI","Bollinger Bands","Donchian Channel"].includes(study.type);
  const hasSource = ["EMA","SMA","Bollinger Bands"].includes(study.type);
  return <Dialog open={open} onClose={onClose} width={420} title={<span className="cx-title-line"><i style={{background:study.color}}/>{studyLabel(study)}</span>} label={"Settings for "+studyLabel(study)}
    footer={<><button className="cx-link-btn grow" onClick={()=>onChange({...study, ...STUDY_DEFAULTS[study.type]})}>Reset to defaults</button>
      <button onClick={()=>onApplyAll(study)}>Apply to all panes</button><button className="primary" onClick={onClose}>Done</button></>}>
    <div className="cx-tabs-inline" role="tablist">{["Inputs","Style","Visibility"].map(t=><button key={t} role="tab" aria-selected={tab===t} onClick={()=>setTab(t)}>{t}</button>)}</div>
    {tab==="Inputs" && <div className="cx-form">
      {hasLength && <div className="cx-form-row"><span>Length</span><NumberField label="Length" value={study.period} onChange={v=>set({period:v})}/></div>}
      {study.type==="Bollinger Bands" && <div className="cx-form-row"><span>Std dev multiplier</span><NumberField label="Multiplier" min={0.5} max={5} stepBy={0.5} value={study.mult ?? 2} onChange={v=>set({mult:v})}/></div>}
      {hasSource && <div className="cx-form-row"><span>Source</span><select aria-label="Source" value={study.source||"close"} onChange={e=>set({source:e.target.value})}>
        {[["close","Close"],["open","Open"],["high","High"],["low","Low"],["hl2","(H+L)/2"],["hlc3","(H+L+C)/3"]].map(([v,t])=><option key={v} value={v}>{t}</option>)}</select></div>}
      {!hasLength && !hasSource && <p className="cx-muted">{study.type} has no inputs. VWAP resets at each session open.</p>}
    </div>}
    {tab==="Style" && <div className="cx-form">
      {study.type!=="Volume" && <div className="cx-form-row"><span>Color</span><Swatches label="Line color" value={study.color} onChange={c=>set({color:c})}/></div>}
      {study.type!=="Volume" && <div className="cx-form-row"><span>Line width</span><Segmented label="Line width" value={study.width ?? 1.5} options={[[1,"1"],[1.5,"1.5"],[2,"2"],[3,"3"]]} onChange={w=>set({width:w})}/></div>}
    </div>}
    {tab==="Visibility" && <div className="cx-form">
      <div className="cx-form-row"><span>Show on chart</span><Switch label="Show on chart" checked={study.visible} onChange={v=>set({visible:v})}/></div>
      {study.type==="EMA" && <div className="cx-form-row"><span>Show crossover markers</span><Switch label="Show crossover markers" checked={study.markers!==false} onChange={v=>set({markers:v})}/></div>}
      {study.type==="EMA" && <p className="cx-muted">Markers appear where the two shortest EMAs cross, when both have markers on.</p>}
    </div>}
  </Dialog>;
}
