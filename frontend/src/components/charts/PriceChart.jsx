import { useEffect, useMemo, useRef, useState } from "react";
import Drawings from "./Drawings.jsx";
import { barIndexAt, chartWindow, crossovers, heikinAshi, makeScale, rsi, viewWindow, wheelBars } from "./chartMath.js";
import { PRICE_STUDIES, studyLabel, studyLines } from "./chartData.js";
import { clearHover, publishHover, useHoverTime } from "./syncStore.js";
import { sessionCloseAfter } from "./alerts.js";

const C = {
  grid: "#151b23", axis: "#7d8a9a", cross: "#5f6b7a", tag: "#2a3340", entry: "#7aa2ff",
  rsi: "#b58cff", alert: "#f5b84a", bg: "#0b0f14", ink: "#06231b", inkDown: "#2a0a10", line: "#5ec8f5",
};
const INTERVAL_MS = { "1m": 60e3, "5m": 300e3, "15m": 900e3, "30m": 1800e3, "1h": 3600e3 };
const clock = new Intl.DateTimeFormat("en-US", {timeZone:"America/New_York",hour:"2-digit",minute:"2-digit",hour12:false});
const dayFmt = new Intl.DateTimeFormat("en-US", {timeZone:"America/New_York",weekday:"short",day:"numeric",month:"short"});
const money = n => (n >= 0 ? "+$" : "-$") + Math.abs(n).toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
const ink = hex => { const n = parseInt(hex.slice(1,7),16); return ((n>>16)*299+((n>>8)&255)*587+(n&255)*114)/1000 > 140 ? "#0b0f14" : "#ffffff"; };

// Polyline point strings for a series, split wherever it has gaps (nulls).
function segments(values, start, end, x, y) {
  const out = []; let cur = [];
  for (let i = start; i < end; i++) {
    const v = values[i];
    if (v == null || !Number.isFinite(v)) { if (cur.length) { out.push(cur.join(" ")); cur = []; } continue; }
    cur.push(x(i-start).toFixed(1)+","+y(v).toFixed(1));
  }
  if (cur.length) out.push(cur.join(" "));
  return out;
}

// Ticks once a second on its own so the rest of the chart does not re-render.
function Countdown({endsAt, intervalMs, x, y, color}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);
  const left = endsAt - now;
  if (left <= 0 || left > intervalMs) return null;
  const s = Math.floor(left/1000), text = (s >= 3600 ? Math.floor(s/3600)+":" : "") + String(Math.floor(s/60)%60).padStart(2,"0")+":"+String(s%60).padStart(2,"0");
  return <text x={x} y={y} style={{fill:color, fontSize:10}}>{text}</text>;
}

export default function PriceChart({pane, paneId, bars, tool, drawingProps, onRemove, onEditStudy, position, live,
  settings, onSettings, alerts = [], resetToken, onContextMenu}) {
  const {interval, studies, symbol} = pane;
  const S = settings;
  const limit = (n, min, max) => Math.max(min, Math.min(max, n));
  const total = bars.length;
  const [view, saveView] = useState({ length: 80, endTime: null, futureSlots: 0 });
  const { length, offset, end, start, minOffset } = chartWindow(bars, view);
  const clampOffset = (o, len) => viewWindow(total, len, o).offset;
  function setView(update) {
    saveView(old => {
      const next = typeof update === "function" ? update(chartWindow(bars, old)) : update;
      const offset = clampOffset(next.offset, next.length);
      const endTime = offset > 0 ? bars[total - 1 - offset]?.time : null;
      return { length: next.length, endTime, futureSlots: Math.max(0, -offset) };
    });
  }
  const plot = useRef(null);
  const [size,setSize] = useState({width:580,height:240});
  useEffect(()=>{
    const observer = new ResizeObserver(([entry])=>{
      const {width,height} = entry.contentRect;
      if(width && height) setSize({width,height});
    });
    observer.observe(plot.current);
    return ()=>observer.disconnect();
  },[]);

  const rsiStudy = studies.find(s => s.visible && s.type === "RSI");
  const axisW = 62;
  const axisX = Math.max(30,size.width-axisW);
  const plotBottom = size.height-25;
  const rsiH = rsiStudy ? Math.min(90, Math.max(56, size.height*0.18)) : 0;
  const rsiTop = plotBottom - rsiH;
  const volBase = rsiTop - 4;
  const volMax = 34;
  const priceTop = 14;
  const priceHeight = Math.max(40, volBase - volMax - 8 - priceTop);

  const drag = useRef(null);
  const [hover, setHover] = useState(null);
  const [priceShift,setPriceShift] = useState(0);
  const [scale,setScale]=useState(1);
  const [cursorY,setCursorY] = useState(0);
  const [cursorX,setCursorX] = useState(0);
  const [nearBottom,setNearBottom] = useState(false);
  function resetView() { setView({length:Math.min(80,total),offset:0}); setPriceShift(0); setScale(1); setHover(null); }
  useEffect(() => { if (resetToken) resetView(); }, [resetToken]); // eslint-disable-line react-hooks/exhaustive-deps
  function zoom(factor, anchor) {
    setView(old => {
      const next = limit(Math.round(old.length * factor), Math.min(10,total), Math.min(400,total));
      const offset = clampOffset(old.offset + (old.length-next)*(1-anchor), next);
      return {length:next,offset};
    });
    setHover(null);
  }
  const wheelCarry = useRef(0);   // sub-candle trackpad distance carried between wheel events
  const viewLength = useRef(length); viewLength.current = length;
  const scroll = dir => setView(old=>({...old,offset:clampOffset(old.offset+dir*Math.max(1,Math.round(old.length/5)), old.length)}));
  useEffect(() => {
    const svg = plot.current;
    function wheel(event) {
      event.preventDefault();
      const rect = svg.getBoundingClientRect();
      const anchor = limit((event.clientX-rect.left)/(Math.max(30,rect.width-axisW)),0,1);
      if (Math.abs(event.deltaX)>Math.abs(event.deltaY)) {
        const {bars, carry} = wheelBars(wheelCarry.current, event.deltaX, event.deltaMode, Math.max(30,rect.width-axisW), viewLength.current);
        wheelCarry.current = carry;
        if (bars) setView(old=>({...old,offset:clampOffset(old.offset-bars, old.length)}));
      } else if (event.deltaY) {
        zoom(event.deltaY>0?1.15:1/1.15,anchor);
      }
    }
    svg.addEventListener("wheel",wheel,{passive:false});
    return ()=>svg.removeEventListener("wheel",wheel);
  },[total, bars[0].time, bars.at(-1).time]); // eslint-disable-line react-hooks/exhaustive-deps

  // Price studies -> drawn lines (nulls skipped). RSI in its own pane.
  const series = useMemo(() => studies.filter(s => s.visible && PRICE_STUDIES.has(s.type)).map(s => ({...s, lines:studyLines(bars,s)})), [bars, studies]);
  const rsiValues = useMemo(() => rsiStudy ? rsi(bars.map(b=>b.close), rsiStudy.period || 14) : null, [bars, rsiStudy]);
  const signals = useMemo(() => {
    if (!S.crossMarkers) return [];
    const emas = series.filter(s => s.type === "EMA" && s.markers !== false).sort((a,b) => a.period-b.period);
    if (emas.length < 2 || emas[0].period === emas[1].period) return [];
    return crossovers(emas[0].lines[0].values, emas[1].lines[0].values);
  }, [series, S.crossMarkers]);
  // Heikin Ashi changes only what is drawn; readout, tags and alerts use real prices.
  const drawBars = useMemo(() => S.type === "Heikin Ashi" ? heikinAshi(bars) : bars, [bars, S.type]);

  const visible = bars.slice(start,end);
  const drawn = drawBars.slice(start,end);
  const showPosition = S.positionLine && position?.avg;
  const lows = drawn.map(b => b.low), highs = drawn.map(b => b.high);
  if (showPosition) { lows.push(position.avg); highs.push(position.avg); }
  const studyRange = series.flatMap(s => s.lines.flatMap(l => l.values.slice(start,end))).filter(Number.isFinite);
  const min = priceShift + Math.min(...lows, ...studyRange);
  const max = priceShift + Math.max(...highs, ...studyRange);
  const center=(max+min)/2, pad=(max-min)*0.04;
  const low=Math.max(1e-6, center-((max-min)/2+pad)*scale), high=center+((max-min)/2+pad)*scale;
  const sc = makeScale({low, high, top:priceTop, height:priceHeight, mode:S.scale, base:visible[0]?.close});
  const y = sc.y;
  const step = axisX/length;   // one slot per candle at this zoom; slots past the newest stay blank
  const x = i => (i+0.5)*step;
  const volume = studies.find(s => s.visible && s.type === "Volume");
  const maxVolume = Math.max(1,...visible.map(b=>b.volume));
  const ry = v => rsiTop + 8 + (rsiH-14) * (100-v)/100;

  // Crosshair sync: publish the hovered bar; draw other panes' hovered time here.
  const [syncTime, syncSource] = useHoverTime();
  const hoverTime = hover !== null ? visible[hover]?.time : null;
  useEffect(() => { if (!S.syncCrosshair) return; if (hoverTime != null) publishHover(hoverTime, paneId); else clearHover(paneId); }, [hoverTime, S.syncCrosshair]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => clearHover(paneId), [paneId]); // remount/unmount: drop our stale sync time
  const syncIndex = S.syncCrosshair && syncTime != null && syncSource !== paneId && hover === null ? barIndexAt(bars, syncTime) - start : null;

  const shown = hover !== null ? Math.min(hover, visible.length-1)
    : (syncIndex !== null && syncIndex >= 0 && syncIndex < visible.length ? syncIndex : visible.length-1);
  const selected = visible[shown];
  const volumeNote = selected.hasTrades
    ? "Trade-built volume is provisional until candle history is refreshed."
    : undefined;
  const prevClose = bars[start + shown - 1]?.close;
  const change = prevClose ? selected.close - prevClose : null;
  const label = b => interval === "1D" ? b.session.slice(5) : clock.format(b.time);
  const tickStride = Math.max(1, Math.ceil(64 / step));
  const ticks = visible.flatMap((bar,index) => {
    const px = x(index);
    return (start + index) % tickStride === 0 && px >= 20 && px <= axisX - 20 ? [index] : [];
  });
  const hoverLabel = interval === "1D" ? selected.session : dayFmt.format(selected.time) + " · " + clock.format(selected.time) + " ET";
  const badgeWidth = interval === "1D" ? 80 : 150;
  const badgeLeft = limit(x(hover ?? 0) - badgeWidth / 2, 0, Math.max(0,axisX-badgeWidth));
  const lastBar = visible.at(-1);
  const hoverPrice = sc.invert(cursorY);
  const rsiNow = rsiValues?.[start+shown];
  const tag = (py, fill, text, textColor, extra) => py >= 6 && py <= volBase && <g>
    <rect x={axisX} y={py-9} width={axisW} height={extra ? 30 : 18} fill={fill}/>
    <text x={axisX+6} y={py+4} style={{fill:textColor,fontWeight:600}}>{text}</text>{extra}</g>;
  const up = S.up, down = S.down;
  const intervalMs = INTERVAL_MS[interval];

  function contextMenu(e) {
    e.preventDefault();
    const rect = e.currentTarget.getBoundingClientRect();
    const px = e.clientX-rect.left, py = e.clientY-rect.top;
    const i = limit(Math.floor(px/step), 0, visible.length-1);
    onContextMenu?.({x:e.clientX, y:e.clientY, price: py < volBase ? sc.invert(py) : lastBar.close, time: visible[i].time});
  }

  return <><div className="cx-readout">
      <b>{symbol} · {interval}</b>
      {["O","H","L","C"].map((k,i)=><span key={k}>{k} <em className={selected.close>=selected.open?"up":"down"}>{[selected.open,selected.high,selected.low,selected.close][i].toFixed(2)}</em></span>)}
      {change!==null && <em className={change>=0?"up":"down"}>{change>=0?"+":""}{change.toFixed(2)} ({(change/prevClose*100).toFixed(2)}%)</em>}
      <span className="cx-readout-time">{label(selected)} ET</span>
    </div>
    <div className="cx-plot-wrap" onPointerLeave={()=>setNearBottom(false)}>
    <div className="cx-study-values">{series.map(study=><div key={study.id}><i style={{background:study.color}}/>{studyLabel(study)} <b style={{color:study.color}}>{study.lines.map(l=>l.values[start+shown]).filter(Number.isFinite).map(v=>v.toFixed(2)).join(" · ")||"—"}</b>
        <button aria-label={"Settings for "+studyLabel(study)} onClick={()=>onEditStudy(study.id)}>⚙</button><button aria-label={"Remove "+studyLabel(study)} onClick={()=>onRemove(study.id)}>×</button></div>)}
      {volume && <div><i style={{background:C.axis}}/>Vol <b title={volumeNote}>{Math.round(selected.volume).toLocaleString()}</b><button aria-label="Remove volume" onClick={()=>onRemove(volume.id)}>×</button></div>}
    </div>
    <svg ref={plot} data-tool={tool} className="cx-plot" tabIndex={0} onContextMenu={contextMenu}
      onKeyDown={e=>{
        const direction = {ArrowLeft:1, ArrowRight:-1}[e.key];
        if (direction) { e.preventDefault(); scroll(direction); }
      }}
      onPointerDown={e=>{
        if(e.button!==0 || !e.isPrimary) return;
        const axis=e.target.dataset.axis;
        e.currentTarget.setPointerCapture(e.pointerId);
        if(axis){ drag.current={axis,x:e.clientX,y:e.clientY,length,offset,scale}; setHover(null); return; }
        drag.current={x:e.clientX,y:e.clientY,offset,priceShift,range:high-low};
      }}
      onLostPointerCapture={()=>{drag.current=null;}}
      viewBox={`0 0 ${size.width} ${size.height}`} preserveAspectRatio="none" role="img" aria-label={symbol+" "+interval+" "+S.type.toLowerCase()+" chart"}
      onPointerLeave={()=>setHover(null)} onPointerMove={e=>{
      const rect=e.currentTarget.getBoundingClientRect();
      setNearBottom(e.clientY-rect.top > plotBottom-90 && e.clientX-rect.left < axisX);
      if(drag.current && e.isPrimary) {
        const d=drag.current;
        if(d.axis==="price"){ setScale(limit(d.scale*Math.exp((e.clientY-d.y)/140),0.08,16)); return; }
        if(d.axis==="time"){
          const next=limit(Math.round(d.length*Math.exp((d.x-e.clientX)/180)),Math.min(total,10),Math.min(total,400));
          setView({length:next,offset:clampOffset(d.offset,next)});
          return;
        }
        const shift=Math.round((e.clientX-d.x)*length/(Math.max(30,rect.width-axisW)));
        setView({length,offset:clampOffset(d.offset+shift,length)});
        setPriceShift(d.priceShift+(e.clientY-d.y)*d.range/(priceHeight));
        setHover(null);
        return;
      }
      if(e.clientX-rect.left>=axisX || e.clientY-rect.top>=plotBottom){setHover(null);return;}
      setCursorY(limit(e.clientY-rect.top,0,plotBottom));
      setCursorX(e.clientX-rect.left);
      setHover(Math.max(0,Math.min(visible.length-1,Math.floor((e.clientX-rect.left)/step))));
    }}>
      <defs><linearGradient id={"cx-area-"+paneId} x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor={C.line} stopOpacity=".28"/><stop offset="100%" stopColor={C.line} stopOpacity="0"/></linearGradient></defs>
      {S.grid && ticks.map(i=><path key={i} d={`M${x(i)} 0V${plotBottom}`} stroke={C.grid}/>)}
      {[0,1,2,3,4,5].map(i=>{const py=priceTop+priceHeight*i/5, price=sc.invert(py);return <g key={i}>{S.grid && <path d={`M0 ${py}H${axisX}`} stroke={C.grid}/>}<text x={axisX+6} y={py+4}>{sc.label(price)}</text></g>})}
      {S.sessionBreaks && interval!=="1D" && visible.map((b,i)=>i>0 && b.session!==visible[i-1].session && <path key={"sb"+b.time} d={`M${x(i)-step/2} 0V${plotBottom}`} stroke="#2a3340" strokeDasharray="2 4"/>)}
      <path d={`M${axisX} 0V${plotBottom}M0 ${plotBottom}H${size.width}`} stroke="#1c232d"/>
      {volume && visible.map((b,i)=><rect key={"v"+b.time} x={x(i)-step*.32} y={volBase-volMax*b.volume/maxVolume} width={step*.64} height={volMax*b.volume/maxVolume} fill={b.close>=b.open?up:down} opacity=".22"/>)}
      {(S.type==="Line" || S.type==="Area") ? (()=>{
        const pts = drawn.map((b,i)=>x(i).toFixed(1)+","+y(b.close).toFixed(1));
        return <g>{S.type==="Area" && <path d={`M${x(0)} ${volBase}L${pts.join("L")}L${x(drawn.length-1)} ${volBase}Z`} fill={`url(#cx-area-${paneId})`}/>}
          <polyline points={pts.join(" ")} fill="none" stroke={C.line} strokeWidth="1.8"/></g>;
      })() : drawn.map((b,i)=>{
        const isUp=b.close>=b.open, body=isUp?up:down, wick=isUp?S.wickUp:S.wickDown, w=step*.32;
        if (S.type==="OHLC bars") return <path key={b.time} d={`M${x(i)} ${y(b.high)}V${y(b.low)}M${x(i)-w} ${y(b.open)}H${x(i)}M${x(i)} ${y(b.close)}H${x(i)+w}`} stroke={body} strokeWidth={step>8?1.5:1}/>;
        const hollow = S.type==="Hollow candles" && isUp;
        return <g key={b.time}><path d={`M${x(i)} ${y(b.high)}V${y(b.low)}`} stroke={wick}/>
          <rect x={x(i)-w} y={y(Math.max(b.open,b.close))} width={w*2} height={Math.max(1,Math.abs(y(b.close)-y(b.open)))} rx={step>9?1:0} fill={hollow?C.bg:body} stroke={hollow?body:"none"}/></g>;
      })}
      {series.flatMap(s=>s.lines.flatMap(l=>segments(l.values,start,end,x,y).map((pts,k)=><polyline key={s.id+l.key+k} points={pts} fill="none" stroke={l.color} strokeWidth={l.width} strokeDasharray={l.dash}/>)))}
      {signals.filter(s=>s.index>=start && s.index<end).map(s=>{
        const i=s.index-start, b=visible[i], isUp=s.direction==="up";
        const py=isUp?y(b.low)+6:y(b.high)-20;
        return <g key={"sig"+s.index} pointerEvents="none"><title>{isUp?"EMA cross up":"EMA cross down"}</title>
          <rect x={x(i)-8} y={py} width="16" height="14" rx="2" fill={isUp?up:down}/>
          <text x={x(i)} y={py+11} textAnchor="middle" style={{fill:isUp?C.ink:C.inkDown,fontWeight:700}}>{isUp?"B":"S"}</text></g>;
      })}
      {alerts.map(a=>{
        const py=y(a.value);
        if(py<6 || py>volBase) return null;
        return <g key={"al"+a.id} pointerEvents="none" opacity={a.active?1:.45}><path d={`M0 ${py}H${axisX}`} stroke={C.alert} strokeDasharray="6 4"/>
          <text x={axisX-8} y={py-5} textAnchor="end" style={{fill:C.alert}}>Alert {a.condition.replace("_"," ")} {a.value.toFixed(2)}</text>
          {tag(py, C.alert, a.value.toFixed(2), "#2a1a00")}</g>;
      })}
      {showPosition && (()=> {
        const py=y(position.avg), pnl=(lastBar.close-position.avg)*position.qty;
        if(py<6 || py>volBase) return null;
        return <g pointerEvents="none"><path d={`M0 ${py}H${axisX}`} stroke={C.entry} strokeDasharray="5 4"/>
          <rect x="10" y={py-20} width="150" height="17" rx="2" fill={C.entry}/>
          <text x="16" y={py-8} style={{fill:"#0b0f14",fontWeight:600}}>{position.qty>0?"Long":"Short"} {Math.abs(position.qty)} · {money(pnl)}</text>
          {tag(py, C.entry, position.avg.toFixed(2), "#0b0f14")}</g>;
      })()}
      {(()=> {
        const pos=y(lastBar.close), isUp=lastBar.close>=lastBar.open, color=isUp?up:down;
        const countdown = S.countdown && live && intervalMs ? <Countdown endsAt={Math.min(lastBar.time+intervalMs, sessionCloseAfter(lastBar.time))} intervalMs={intervalMs} x={axisX+6} y={pos+17} color={ink(color)}/> : null;
        return pos>=6 && pos<=volBase && <g>{S.lastLine && <path d={`M0 ${pos}H${axisX}`} stroke={color} strokeDasharray="2 3" opacity=".6"/>}
          {tag(pos, color, sc.label(lastBar.close), ink(color), countdown)}</g>;
      })()}
      {rsiValues && <g>
        <path d={`M0 ${rsiTop}H${size.width}`} stroke="#1c232d"/>
        <rect x="0" y={ry(70)} width={axisX} height={ry(30)-ry(70)} fill={rsiStudy.color||C.rsi} opacity=".06"/>
        {[70,30].map(v=><g key={v}><path d={`M0 ${ry(v)}H${axisX}`} stroke="#2a3340" strokeDasharray="4 4"/><text x={axisX+6} y={ry(v)+4}>{v}</text></g>)}
        {segments(rsiValues,start,end,x,ry).map((pts,k)=><polyline key={"rsi"+k} points={pts} fill="none" stroke={rsiStudy.color||C.rsi} strokeWidth={rsiStudy.width||1.4}/>)}
        <text x="10" y={rsiTop+16} style={{fill:"#9aa7b6"}}>RSI {rsiStudy.period||14}</text>
        {rsiNow!=null && <><text x={58} y={rsiTop+16} style={{fill:rsiStudy.color||C.rsi,fontWeight:600}}>{rsiNow.toFixed(1)}</text>
          {(rsiNow>=70 || rsiNow<=30) && <text x={92} y={rsiTop+16} style={{fill:"#f5b84a"}}>{rsiNow>=70?"Overbought":"Oversold"}</text>}
          <rect x={axisX} y={ry(rsiNow)-9} width={axisW} height="18" fill={rsiStudy.color||C.rsi}/><text x={axisX+6} y={ry(rsiNow)+4} style={{fill:"#140a24",fontWeight:600}}>{rsiNow.toFixed(1)}</text></>}
      </g>}
      {ticks.map(i=><text key={i} x={x(i)} textAnchor="middle" y={size.height-8}>{label(visible[i])}</text>)}

      {hover!==null && tool==="Crosshair" && cursorY<volBase && <><path d={`M0 ${cursorY}H${axisX}`} stroke={C.cross} strokeDasharray="3 3"/>{tag(cursorY, C.tag, sc.label(hoverPrice), "#e6edf3")}</>}
      {hover!==null && tool==="Crosshair" && <path d={`M${x(hover)} 0V${plotBottom}`} stroke={C.cross} strokeDasharray="3 3"/>}
      {hover===null && syncIndex!==null && syncIndex>=0 && syncIndex<visible.length && <path d={`M${x(syncIndex)} 0V${plotBottom}`} stroke={C.cross} strokeDasharray="3 3" opacity=".8"/>}
      {visible[hover] && <g pointerEvents="none">
        <rect x={badgeLeft} y={plotBottom+2} width={badgeWidth} height="21" rx="3" fill={C.tag}/>
        <text x={badgeLeft+badgeWidth/2} y={plotBottom+16} textAnchor="middle" style={{fill:"#e6edf3"}}>{hoverLabel}</text>
      </g>}
      <Drawings {...drawingProps} {...{pane,tool,bars,start,length,x,y,axisX}} height={volBase} invert={sc.invert}/>
      <rect data-axis="price" x={axisX} y="0" width={axisW} height={plotBottom} fill="transparent" style={{cursor:"ns-resize"}}><title>Drag up or down to scale prices</title></rect>
      <rect data-axis="time" x="0" y={plotBottom} width={axisX} height="25" fill="transparent" style={{cursor:"ew-resize"}}><title>Drag left or right to change candle spacing</title></rect>
    </svg>
    <div className="cx-scale-modes" role="group" aria-label="Price scale">
      {[["auto","A","Auto scale"],["log","L","Logarithmic scale"],["percent","%","Percent scale"]].map(([m,t,title])=><button key={m} title={title} aria-label={title} aria-pressed={S.scale===m} onClick={()=>onSettings({scale:m})}>{t}</button>)}
    </div>
    <div className={"cx-nav"+(nearBottom?" show":"")} role="toolbar" aria-label="Chart navigation" style={{bottom:size.height-plotBottom+10}} onPointerEnter={()=>setNearBottom(true)}>
      <button aria-label="Zoom out (overlay)" title="Zoom out" disabled={length>=Math.min(400,total)} onClick={()=>zoom(1.25,.5)}>−</button>
      <button aria-label="Zoom in (overlay)" title="Zoom in" disabled={length<=Math.min(10,total)} onClick={()=>zoom(.8,.5)}>+</button>
      <span className="cx-nav-sep"/>
      <button aria-label="Scroll back" title="Scroll back" disabled={offset>=total-length} onClick={()=>scroll(1)}>‹</button>
      <button aria-label="Scroll forward" title="Scroll forward" disabled={offset<=minOffset} onClick={()=>scroll(-1)}>›</button>
      <button aria-label="Reset view" title="Reset view (Alt R)" onClick={resetView}>↺</button>
    </div>
    {offset>0 && <button className="cx-latest" style={{bottom:size.height-plotBottom+10}} onClick={()=>setView(v=>({...v,offset:0}))}>Go to latest »</button>}
    {S.dataWindow!=="off" && (S.dataWindow==="pinned" || hover!==null) && (()=>{
      const pinned=S.dataWindow==="pinned", w=214;
      const left=pinned ? axisX-w-8 : (cursorX+18+w > axisX ? cursorX-18-w : cursorX+18);
      const top=pinned ? 8 : limit(cursorY+16, 8, Math.max(8, plotBottom-230));
      const v=s=>s.lines.map(l=>l.values[start+shown]).filter(Number.isFinite).map(n=>n.toFixed(2)).join(" · ")||"—";
      return <div className={"cx-datawin"+(pinned?" pinned":"")} style={{left:Math.max(4,left),top,width:w}}>
        <div className="cx-dw-head"><b>{hoverLabel}</b>{pinned && <button aria-label="Unpin data window" title="Unpin (follow the crosshair)" onClick={()=>onSettings({dataWindow:"follow"})}>📌</button>}</div>
        <dl>{[["Open",selected.open],["High",selected.high],["Low",selected.low],["Close",selected.close]].map(([k,n])=><div key={k}><dt>{k}</dt><dd className={selected.close>=selected.open?"up":"down"}>{n.toFixed(2)}</dd></div>)}
          <div><dt>Change</dt><dd className={change>=0?"up":"down"}>{change==null?"—":(change>=0?"+":"")+(change/prevClose*100).toFixed(2)+"%"}</dd></div>
          <div><dt>Volume</dt><dd title={volumeNote}>{Math.round(selected.volume).toLocaleString()}</dd></div></dl>
        {(series.length>0 || rsiNow!=null) && <ul>{series.map(s=><li key={s.id}><span><i style={{background:s.color}}/>{studyLabel(s)}</span><b style={{color:s.color}}>{v(s)}</b></li>)}
          {rsiNow!=null && <li><span><i style={{background:rsiStudy.color||C.rsi}}/>RSI {rsiStudy.period||14}</span><b style={{color:rsiStudy.color||C.rsi}}>{rsiNow.toFixed(1)}</b></li>}</ul>}
      </div>;
    })()}
    </div>
    <div className="cx-foot"><span>{selected.session} · ET</span><button title="Restore latest candles and fit price scale" onClick={resetView}>Reset view</button>
      <button aria-label="Zoom out" title="Zoom out" disabled={length>=Math.min(400,total)} onClick={()=>zoom(1.25,.5)}>−</button>
      <button aria-label="Zoom in" title="Zoom in" disabled={length<=Math.min(10,total)} onClick={()=>zoom(.8,.5)}>+</button></div></>;
}
