import {useRef,useState} from "react";

export const drawingTools = [
  ["Crosshair","⌖"],["Pan","✥"],["Trend line","╱"],["Horizontal line","━"],
  ["Rectangle","▭"],["Fib retracement","≋"],["Brush","∿"],["Text","T"],
  ["Measure","↔"],["Long position","↗"],["Short position","↘"]
];

export function symbolDrawings(pane) {
  const saved=pane.drawings?.[pane.symbol];
  if(saved) return saved;
  const price=pane.levels?.[pane.symbol];
  return Number.isFinite(price)?[{id:"legacy-line",type:"Horizontal line",points:[{time:0,price}]}]:[];
}

function barIndex(bars,time) {
  let lo=0,hi=bars.length;
  while(lo<hi){
    const mid=Math.floor((lo+hi)/2);
    if(bars[mid].time<=time) lo=mid+1;
    else hi=mid;
  }
  return Math.max(0,lo-1);
}

export default function Drawings({pane,tool,bars,start,length,x,y,axisX,height,invert,magnet,locked,hidden,selected,onSelect,save,finish}) {
  const items=symbolDrawings(pane);
  const gesture=useRef(null);
  const [draft,setDraft]=useState(null);
  const drawing=tool!=="Pan" && tool!=="Crosshair";
  function point(e) {
    const box=e.currentTarget.ownerSVGElement.getBoundingClientRect();
    const i=Math.min(length-1,Math.max(0,Math.floor((e.clientX-box.left)*length/axisX)));
    const bar=bars[start+i];
    let price=invert(Math.max(0,Math.min(height,e.clientY-box.top)));
    if(magnet) price=[bar.open,bar.high,bar.low,bar.close].reduce((a,b)=>Math.abs(price-a)<Math.abs(price-b)?a:b);
    return {time:bar.time,price};
  }
  function down(e) {
    if(locked || hidden || !e.isPrimary || e.button!==0) return;
    const item=items.find(d=>d.id===e.target.closest("[data-drawing]")?.dataset.drawing);
    if(!drawing && (!item || tool==="Pan")) return;
    e.stopPropagation();
    // A locked drawing can be selected (to unlock or restyle it) but not moved.
    if(!drawing && item.locked){ onSelect(item.id); return; }
    const anchor=point(e);
    const shape=drawing?{id:globalThis.crypto?.randomUUID?.()||String(Date.now()),type:tool,points:[anchor,anchor]}:item;
    if(drawing && tool==="Text"){
      const text=window.prompt("Chart annotation");
      if(!text?.trim()) return;
      shape.text=text.trim();
    }
    gesture.current={anchor,shape,moving:!drawing};
    onSelect(shape.id);
    setDraft(shape);
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function next(e) {
    const {anchor,shape,moving}=gesture.current;
    const end=point(e);
    if(moving){
      const shift=barIndex(bars,end.time)-barIndex(bars,anchor.time);
      return {...shape,points:shape.points.map(p=>({
        time:bars[Math.max(0,Math.min(bars.length-1,barIndex(bars,p.time)+shift))].time,
        price:p.price+end.price-anchor.price
      }))};
    }
    if(shape.type==="Brush") shape.points.push(end);
    else shape.points[1]=end;
    return {...shape,points:[...shape.points]};
  }
  function render(d){
    const [a,b=a]=d.points;
    const ax=x(barIndex(bars,a.time)-start),bx=x(barIndex(bars,b.time)-start);
    const ay=y(a.price),by=y(b.price),left=Math.min(ax,bx),width=Math.max(2,Math.abs(bx-ax));
    const line=(x1,y1,x2,y2)=><line {...{x1,y1,x2,y2}}/>;
    const label=(px,py,text)=><text x={px} y={py} stroke="none" style={{fill:"#e6edf3",paintOrder:"stroke",stroke:"#0b0f14",strokeWidth:3}}>{text}</text>;
    let body;
    switch(d.type){
      case "Horizontal line": body=line(0,ay,axisX,ay); break;
      case "Rectangle": body=<rect x={left} y={Math.min(ay,by)} width={width} height={Math.max(2,Math.abs(by-ay))} fill={(d.color||"#7aa2ff")+"22"}/>;break;
      case "Brush": body=<polyline points={d.points.map(p=>x(barIndex(bars,p.time)-start)+","+y(p.price)).join(" ")}/>;break;
      case "Text": body=label(ax,ay,d.text);break;
      case "Fib retracement": body=[0,.236,.382,.5,.618,.786,1].map(f=><g key={f}>{line(left,ay+(by-ay)*f,left+width,ay+(by-ay)*f)}{label(left+2,ay+(by-ay)*f-2,String(f))}</g>);break;
      case "Long position": case "Short position":{
        const risk=Math.abs(b.price-a.price),dir=d.type==="Long position"?1:-1;
        const stop=y(a.price-dir*risk),target=y(a.price+2*dir*risk);
        body=<>{[[stop,"#d04c59"],[target,"#158773"]].map(([p,c])=><rect key={c} x={left} y={Math.min(ay,p)} width={width} height={Math.abs(p-ay)} fill={c} fillOpacity=".17" stroke={c}/>)}{line(left,ay,left+width,ay)}{label(left+3,ay-3,"2R · risk "+risk.toFixed(2))}</>;break;
      }
      default: body=<>{line(ax,ay,bx,by)}{d.type==="Measure" && label(left+2,(ay+by)/2-5,(b.price-a.price).toFixed(2)+" · "+((b.price/a.price-1)*100).toFixed(2)+"% · "+Math.abs(barIndex(bars,a.time)-barIndex(bars,b.time))+" bars")}</>;
    }
    const dash={dashed:"7 5",dotted:"2 4"}[d.dash];
    const note=d.label && d.type!=="Text" && label(Math.max(ax,bx)+6,(d.type==="Horizontal line"?ay:by)-6,d.label);
    return <g key={d.id} data-drawing={d.id} stroke={d.color||"#7aa2ff"} strokeWidth={(d.width||1.7)+(selected===d.id?1:0)} strokeDasharray={dash} fill="none" style={{cursor:locked||d.locked?"inherit":"move"}}><title>{d.type}{d.locked?" (locked)":""}</title>{body}{note}</g>;
  }
  if(hidden) return null;
  return <g onPointerDown={down} onPointerMove={e=>{if(gesture.current){e.stopPropagation();setDraft(next(e));}}}
    onPointerUp={e=>{
      if(!gesture.current)return;
      const shape=next(e),moving=gesture.current.moving;
      gesture.current=null;setDraft(null);
      save(moving?items.map(d=>d.id===shape.id?shape:d):[...items,shape]);
      if(!moving)finish();
    }} onLostPointerCapture={()=>{gesture.current=null;setDraft(null);}}>
    <svg width={axisX} height={height} pointerEvents={locked?"none":"auto"}>
      {drawing && <rect width="100%" height="100%" fill="transparent" style={{cursor:"crosshair"}}/>}
      {items.filter(d=>d.id!==draft?.id).map(render)}{draft && render(draft)}
    </svg>
  </g>;
}
