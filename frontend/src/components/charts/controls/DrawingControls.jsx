import { useRef, useState } from "react";
import { Popover, Swatches } from "./Overlay.jsx";
import { drawingTools } from "../Drawings.jsx";

const icon = Object.fromEntries(drawingTools);
export const DRAWING_GROUPS = [
  ["Lines", ["Trend line","Horizontal line","Brush"]],
  ["Shapes and levels", ["Rectangle","Fib retracement","Long position","Short position"]],
  ["Annotate and measure", ["Text","Measure"]],
];
// Alt + key (matched by e.code so macOS Option characters do not matter).
export const TOOL_SHORTCUTS = {KeyT:"Trend line", KeyH:"Horizontal line", KeyF:"Fib retracement"};
const shortcutFor = name => { const k = Object.keys(TOOL_SHORTCUTS).find(c=>TOOL_SHORTCUTS[c]===name); return k ? "Alt "+k.slice(3) : null; };

export function DrawingFlyout({open, onClose, anchorRef, tool, onPick, favorites, onToggleFavorite}) {
  return <Popover open={open} onClose={onClose} anchorRef={anchorRef} label="Drawing tools" className="cx-menu w-260 cx-flyout">
    {DRAWING_GROUPS.map(([group, names], gi)=><div key={group}>
      <div className={"cx-menu-label"+(gi?" sep":"")}>{group}</div>
      {names.map(n=><div key={n} className={"cx-menu-row"+(tool===n?" current":"")}>
        <button role="menuitem" className="cx-menu-main" onClick={()=>{onPick(n);onClose();}}><span className="cx-menu-icon" aria-hidden="true">{icon[n]}</span>{n}{shortcutFor(n) && <kbd>{shortcutFor(n)}</kbd>}</button>
        <button className={"cx-star"+(favorites.includes(n)?" on":"")} aria-pressed={favorites.includes(n)} aria-label={(favorites.includes(n)?"Remove ":"Add ")+n+" to favorites"} onClick={()=>onToggleFavorite(n)}>★</button>
      </div>)}
    </div>)}
  </Popover>;
}

// Floats over the pane while a drawing is selected. The grip drags it.
export function DrawingToolbar({drawing, onChange, onDelete, onCopy, onAlert}) {
  const [offset,setOffset]=useState({x:0,y:0});
  const drag=useRef(null);
  const [menu,setMenu]=useState(null);
  const refs={color:useRef(null), width:useRef(null), dash:useRef(null)};
  const color=drawing.color||"#7aa2ff", width=drawing.width||1.7, dash=drawing.dash||"solid";
  const close=()=>setMenu(null);
  return <div className="cx-draw-bar" role="toolbar" aria-label={drawing.type+" options"} style={{transform:`translate(calc(-50% + ${offset.x}px), ${offset.y}px)`}}>
    <span className="cx-grip" aria-label="Drag toolbar" title="Drag to move"
      onPointerDown={e=>{drag.current={x:e.clientX-offset.x,y:e.clientY-offset.y};e.currentTarget.setPointerCapture(e.pointerId);}}
      onPointerMove={e=>{if(drag.current) setOffset({x:e.clientX-drag.current.x,y:e.clientY-drag.current.y});}}
      onLostPointerCapture={()=>{drag.current=null;}}>⋮⋮</span>
    <button ref={refs.color} aria-label="Line color" aria-expanded={menu==="color"} onClick={()=>setMenu(menu==="color"?null:"color")}><i className="cx-dot" style={{background:color}}/>▾</button>
    <button ref={refs.width} aria-label="Line width" aria-expanded={menu==="width"} onClick={()=>setMenu(menu==="width"?null:"width")}>{Math.round(width)}px ▾</button>
    <button ref={refs.dash} aria-label="Line style" aria-expanded={menu==="dash"} onClick={()=>setMenu(menu==="dash"?null:"dash")}>{dash[0].toUpperCase()+dash.slice(1)} ▾</button>
    <span className="cx-nav-sep"/>
    <button aria-label="Add text label" title={drawing.label?"Edit label":"Add label"} onClick={()=>{const t=window.prompt("Label for this drawing", drawing.label||""); if(t!==null) onChange({label:t.trim()||undefined});}}>T</button>
    <button aria-label="Alert at this level" title={onAlert?"Create alert at this level":"Alerts work from horizontal lines"} disabled={!onAlert} onClick={onAlert}>⏰</button>
    <button aria-label="Duplicate drawing" title="Duplicate" onClick={onCopy}>⧉</button>
    <button aria-label={drawing.locked?"Unlock drawing":"Lock drawing"} aria-pressed={!!drawing.locked} title={drawing.locked?"Unlock":"Lock"} onClick={()=>onChange({locked:!drawing.locked})}>{drawing.locked?"🔒":"🔓"}</button>
    <button className="danger" aria-label="Delete drawing" title="Delete" onClick={onDelete}>🗑</button>
    <Popover open={menu==="color"} onClose={close} anchorRef={refs.color} label="Line color" className="cx-menu cx-pad-s"><Swatches label="Line color" value={color} onChange={c=>{onChange({color:c});close();}}/></Popover>
    <Popover open={menu==="width"} onClose={close} anchorRef={refs.width} label="Line width" className="cx-menu w-120">{[1,2,3,4].map(w=><button key={w} role="menuitemcheckbox" aria-checked={Math.round(width)===w} className="cx-menu-item" onClick={()=>{onChange({width:w});close();}}><span className="cx-width-sample" style={{height:w}}/>{w}px</button>)}</Popover>
    <Popover open={menu==="dash"} onClose={close} anchorRef={refs.dash} label="Line style" className="cx-menu w-120">{["solid","dashed","dotted"].map(d=><button key={d} role="menuitemcheckbox" aria-checked={dash===d} className="cx-menu-item" onClick={()=>{onChange({dash:d});close();}}>{d[0].toUpperCase()+d.slice(1)}</button>)}</Popover>
  </div>;
}
