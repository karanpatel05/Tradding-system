import { useEffect, useMemo, useRef, useState } from "react";
import Drawings from "./Drawings.jsx";
import { crossovers, rsi } from "./chartMath.js";
import { studyValues } from "./chartData.js";

// Dark chart palette (matches the .cx tokens in charts.css).
const C = {
  up: "#2dd4a7", down: "#f0657a", grid: "#151b23", axis: "#7d8a9a", cross: "#5f6b7a",
  tag: "#2a3340", entry: "#7aa2ff", rsi: "#b58cff", ink: "#06231b", inkDown: "#2a0a10",
};
const clock = new Intl.DateTimeFormat("en-US", {timeZone:"America/New_York",hour:"2-digit",minute:"2-digit",hour12:false});
const money = n => (n >= 0 ? "+$" : "-$") + Math.abs(n).toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});

export default function PriceChart({pane,bars,tool,drawingProps,onRemove,position}) {
  const {interval, studies, symbol} = pane;
  const limit = (n, min, max) => Math.max(min, Math.min(max, n));
  const total = bars.length;
  const [view, setView] = useState({length: Math.min(80,total), offset:0});
  // Live polling can change the bar count; keep the window inside the data.
  const length = limit(view.length, Math.min(10,total), total);
  const offset = limit(view.offset, 0, total-length);
  const end = total - offset;
  const start = end - length;
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
  function zoom(factor, anchor) {
    setView(old => {
      const next = limit(Math.round(old.length * factor), Math.min(10,total), Math.min(400,total));
      const offset = limit(Math.round(old.offset + (old.length-next)*(1-anchor)),0,total-next);
      return {length:next,offset};
    });
    setHover(null);
  }
  useEffect(() => {
    const svg = plot.current;
    function wheel(event) {
      event.preventDefault();
      const rect = svg.getBoundingClientRect();
      const anchor = limit((event.clientX-rect.left)/(Math.max(30,rect.width-axisW)),0,1);
      if (Math.abs(event.deltaX)>Math.abs(event.deltaY)) {
        setView(old=>({...old,offset:limit(old.offset-Math.sign(event.deltaX)*Math.max(1,Math.round(old.length/12)),0,total-old.length)}));
      } else if (event.deltaY) {
        zoom(event.deltaY>0?1.15:1/1.15,anchor);
      }
    }
    svg.addEventListener("wheel",wheel,{passive:false});
    return ()=>svg.removeEventListener("wheel",wheel);
  },[total]);

  const series = useMemo(() => studies.filter(s => s.visible && (s.type === "EMA" || s.type === "VWAP")).map(s => ({
    ...s, values:studyValues(bars,s)
  })), [bars, studies]);
  const rsiValues = useMemo(() => rsiStudy ? rsi(bars.map(b=>b.close), rsiStudy.period || 14) : null, [bars, rsiStudy]);
  // Crossover markers between the two shortest visible EMAs.
  const signals = useMemo(() => {
    const emas = series.filter(s => s.type === "EMA").sort((a,b) => a.period-b.period);
    if (emas.length < 2 || emas[0].period === emas[1].period) return [];
    return crossovers(emas[0].values, emas[1].values);
  }, [series]);

  const visible = bars.slice(start,end);
  const lows = visible.map(b => b.low), highs = visible.map(b => b.high);
  if (position?.avg) { lows.push(position.avg); highs.push(position.avg); }
  const studyRange = series.flatMap(s => s.values.slice(start,end)).filter(Number.isFinite);
  const min = priceShift + Math.min(...lows, ...studyRange);
  const max = priceShift + Math.max(...highs, ...studyRange);
  const center=(max+min)/2;
  const pad=(max-min)*0.04;
  const low=center-((max-min)/2+pad)*scale, high=center+((max-min)/2+pad)*scale;
  const y = p => priceTop + priceHeight * (high-p)/(high-low || 1);
  const step = axisX/visible.length;
  const x = i => (i+0.5)*step;
  const volume = studies.find(s => s.visible && s.type === "Volume");
  const maxVolume = Math.max(1,...visible.map(b=>b.volume));
  const ry = v => rsiTop + 8 + (rsiH-14) * (100-v)/100;

  const selected = visible[hover] || visible.at(-1);
  const prevClose = bars[start + (hover ?? visible.length-1) - 1]?.close;
  const change = prevClose ? selected.close - prevClose : null;
  const label = b => interval === "1D" ? b.session.slice(5) : clock.format(b.time);
  const tickStride = Math.max(1, Math.ceil(64 / step));
  const ticks = visible.flatMap((bar,index) => {
    const px = x(index);
    return (start + index) % tickStride === 0 && px >= 20 && px <= axisX - 20 ? [index] : [];
  });
  const hoverLabel = interval === "1D" ? selected.session : selected.session + "  " + clock.format(selected.time) + " ET";
  const badgeWidth = interval === "1D" ? 80 : 142;
  const badgeLeft = limit(x(hover ?? 0) - badgeWidth / 2, 0, Math.max(0,axisX-badgeWidth));
  const lastBar = visible.at(-1);
  const hoverPrice = high-(cursorY-priceTop)*(high-low)/priceHeight;
  const rsiNow = rsiValues?.[start+(hover ?? visible.length-1)];
  const tag = (py, fill, text, ink) => py >= 6 && py <= volBase && <g>
    <rect x={axisX} y={py-9} width={axisW} height="18" fill={fill}/>
    <text x={axisX+6} y={py+4} style={{fill:ink,fontWeight:600}}>{text}</text></g>;

  return <><div className="cx-readout">
      <b>{symbol} · {interval}</b>
      {["O","H","L","C"].map((k,i)=><span key={k}>{k} <em className={selected.close>=selected.open?"up":"down"}>{[selected.open,selected.high,selected.low,selected.close][i].toFixed(2)}</em></span>)}
      {change!==null && <em className={change>=0?"up":"down"}>{change>=0?"+":""}{change.toFixed(2)} ({(change/prevClose*100).toFixed(2)}%)</em>}
      <span className="cx-readout-time">{label(selected)} ET</span>
    </div>
    <div className="cx-study-values">{series.map(study=><div key={study.id}><i style={{background:study.color}}/>{study.type} {study.type==="EMA" && study.period} <b style={{color:study.color}}>{study.values[start+(hover ?? visible.length-1)]?.toFixed(2)}</b><button aria-label={"Remove "+study.type+" "+(study.period||"")} onClick={()=>onRemove(study.id)}>×</button></div>)}
      {volume && <div><i style={{background:C.axis}}/>Vol <b>{Math.round(selected.volume).toLocaleString()}</b><button aria-label="Remove volume" onClick={()=>onRemove(volume.id)}>×</button></div>}
    </div>
    <svg ref={plot} data-tool={tool} className="cx-plot" tabIndex={0}
      onKeyDown={e=>{
        const direction = {ArrowLeft:1, ArrowRight:-1}[e.key];
        if (direction) {
          e.preventDefault();
          setView(old=>({...old,offset:limit(old.offset+direction*Math.max(1,Math.round(old.length/5)),0,total-old.length)}));
        }
      }}
      onPointerDown={e=>{
        if(e.button!==0 || !e.isPrimary) return;
        const axis=e.target.dataset.axis;
        e.currentTarget.setPointerCapture(e.pointerId);
        if(axis){
          drag.current={axis,x:e.clientX,y:e.clientY,length,offset,scale};
          setHover(null);
          return;
        }
        drag.current={x:e.clientX,y:e.clientY,offset,priceShift,range:high-low};
      }}
      onLostPointerCapture={()=>{drag.current=null;}}
       viewBox={`0 0 ${size.width} ${size.height}`} preserveAspectRatio="none" role="img" aria-label={symbol+" "+interval+" candlestick chart"} onPointerLeave={()=>setHover(null)} onPointerMove={e=>{
      const rect=e.currentTarget.getBoundingClientRect();
      if(drag.current && e.isPrimary) {
        const d=drag.current;
        if(d.axis==="price"){
          setScale(limit(d.scale*Math.exp((e.clientY-d.y)/140),0.08,16));
          return;
        }
        if(d.axis==="time"){
          const next=limit(Math.round(d.length*Math.exp((d.x-e.clientX)/180)),Math.min(total,10),Math.min(total,400));
          setView({length:next,offset:Math.min(d.offset,total-next)});
          return;
        }
        const shift=Math.round((e.clientX-d.x)*length/(Math.max(30,rect.width-axisW)));
        setView({length,offset:limit(d.offset+shift,0,total-length)});
        setPriceShift(d.priceShift+(e.clientY-d.y)*d.range/(priceHeight));
        setHover(null);
        return;
      }
      if(e.clientX-rect.left>=axisX || e.clientY-rect.top>=plotBottom){setHover(null);return;}
      setCursorY(limit(e.clientY-rect.top,0,plotBottom));
      setHover(Math.max(0,Math.min(visible.length-1,Math.floor((e.clientX-rect.left)/step))));
    }}>
      {ticks.map(i=><path key={i} d={`M${x(i)} 0V${plotBottom}`} stroke={C.grid}/>)}
      {[0,1,2,3,4,5].map(i=>{const price=high-(high-low)*i/5, py=y(price);return py<volBase && <g key={i}><path d={`M0 ${py}H${axisX}`} stroke={C.grid}/><text x={axisX+6} y={py+4}>{price.toFixed(2)}</text></g>})}
      <path d={`M${axisX} 0V${plotBottom}M0 ${plotBottom}H${size.width}`} stroke="#1c232d"/>
      {volume && visible.map((b,i)=><rect key={"v"+b.time} x={x(i)-step*.32} y={volBase-volMax*b.volume/maxVolume} width={step*.64} height={volMax*b.volume/maxVolume} fill={b.close>=b.open?C.up:C.down} opacity=".22"/>)}
      {visible.map((b,i)=>{const color=b.close>=b.open?C.up:C.down;return <g key={b.time} fill={color}><path d={`M${x(i)} ${y(b.high)}V${y(b.low)}`} stroke={color}/><rect x={x(i)-step*.32} y={y(Math.max(b.open,b.close))} width={step*.64} height={Math.max(1,Math.abs(y(b.close)-y(b.open)))} rx={step>9?1:0}/></g>})}
      {series.map(s=><polyline key={s.id} points={s.values.slice(start,end).map((v,i)=>x(i)+","+y(v)).join(" ")} fill="none" stroke={s.color} strokeWidth="1.6"/>)}
      {signals.filter(s=>s.index>=start && s.index<end).map(s=>{
        const i=s.index-start, b=visible[i], up=s.direction==="up";
        const py=up?y(b.low)+6:y(b.high)-20;
        return <g key={"sig"+s.index} pointerEvents="none"><title>{up?"EMA cross up":"EMA cross down"}</title>
          <rect x={x(i)-8} y={py} width="16" height="14" rx="2" fill={up?C.up:C.down}/>
          <text x={x(i)} y={py+11} textAnchor="middle" style={{fill:up?C.ink:C.inkDown,fontWeight:700}}>{up?"B":"S"}</text></g>;
      })}
      {position?.avg && (()=> {
        const py=y(position.avg), pnl=(lastBar.close-position.avg)*position.qty;
        if(py<6 || py>volBase) return null;
        return <g pointerEvents="none"><path d={`M0 ${py}H${axisX}`} stroke={C.entry} strokeDasharray="5 4"/>
          <rect x="10" y={py-20} width="150" height="17" rx="2" fill={C.entry}/>
          <text x="16" y={py-8} style={{fill:"#0b0f14",fontWeight:600}}>{position.qty>0?"Long":"Short"} {Math.abs(position.qty)} · {money(pnl)}</text>
          {tag(py, C.entry, position.avg.toFixed(2), "#0b0f14")}</g>;
      })()}
      {(()=> {
        const pos=y(lastBar.close);
        const up=lastBar.close>=lastBar.open, color=up?C.up:C.down;
        return pos>=6 && pos<=volBase && <g><path d={`M0 ${pos}H${axisX}`} stroke={color} strokeDasharray="2 3" opacity=".6"/>
          {tag(pos, color, lastBar.close.toFixed(2), up?C.ink:C.inkDown)}</g>;
      })()}
      {rsiValues && <g>
        <path d={`M0 ${rsiTop}H${size.width}`} stroke="#1c232d"/>
        <rect x="0" y={ry(70)} width={axisX} height={ry(30)-ry(70)} fill={C.rsi} opacity=".06"/>
        {[70,30].map(v=><g key={v}><path d={`M0 ${ry(v)}H${axisX}`} stroke="#2a3340" strokeDasharray="4 4"/><text x={axisX+6} y={ry(v)+4}>{v}</text></g>)}
        <polyline points={rsiValues.slice(start,end).flatMap((v,i)=>v===null?[]:[x(i)+","+ry(v)]).join(" ")} fill="none" stroke={C.rsi} strokeWidth="1.4"/>
        <text x="10" y={rsiTop+16} style={{fill:"#9aa7b6"}}>RSI {rsiStudy.period||14}</text>
        {rsiNow!=null && <><text x={58} y={rsiTop+16} style={{fill:C.rsi,fontWeight:600}}>{rsiNow.toFixed(1)}</text>
          {rsiNow>=70 && <text x={92} y={rsiTop+16} style={{fill:"#f5b84a"}}>Overbought</text>}
          {rsiNow<=30 && <text x={92} y={rsiTop+16} style={{fill:"#f5b84a"}}>Oversold</text>}
          <rect x={axisX} y={ry(rsiNow)-9} width={axisW} height="18" fill={C.rsi}/><text x={axisX+6} y={ry(rsiNow)+4} style={{fill:"#140a24",fontWeight:600}}>{rsiNow.toFixed(1)}</text></>}
      </g>}
      {ticks.map(i=><text key={i} x={x(i)} textAnchor="middle" y={size.height-8}>{label(visible[i])}</text>)}

      {hover!==null && tool==="Crosshair" && cursorY<volBase && <><path d={`M0 ${cursorY}H${axisX}`} stroke={C.cross} strokeDasharray="3 3"/>{tag(cursorY, C.tag, hoverPrice.toFixed(2), "#e6edf3")}</>}
      {hover!==null && tool==="Crosshair" && <path d={`M${x(hover)} 0V${plotBottom}`} stroke={C.cross} strokeDasharray="3 3"/>}
      {visible[hover] && <g pointerEvents="none">
        <rect x={badgeLeft} y={plotBottom+2} width={badgeWidth} height="21" rx="3" fill={C.tag}/>
        <text x={badgeLeft+badgeWidth/2} y={plotBottom+16} textAnchor="middle" style={{fill:"#e6edf3"}}>{hoverLabel}</text>
      </g>}
      <Drawings {...drawingProps} {...{pane,tool,bars,start,length,x,y,axisX}} height={volBase} invert={dy=>high-(dy-priceTop)*(high-low)/priceHeight}/>
      <rect data-axis="price" x={axisX} y="0" width={axisW} height={plotBottom} fill="transparent" style={{cursor:"ns-resize"}}><title>Drag up or down to scale prices</title></rect>
      <rect data-axis="time" x="0" y={plotBottom} width={axisX} height="25" fill="transparent" style={{cursor:"ew-resize"}}><title>Drag left or right to change candle spacing</title></rect>
    </svg><div className="cx-foot"><span>{selected.session} · ET</span><button title="Restore latest candles and fit price scale" onClick={()=>{setView({length:Math.min(80,total),offset:0});setPriceShift(0);setScale(1);setHover(null);}}>Reset view</button><button aria-label="Zoom out" disabled={length>=Math.min(400,total)} onClick={()=>zoom(1.25,.5)}>−</button><button aria-label="Zoom in" disabled={length<=Math.min(10,total)} onClick={()=>zoom(.8,.5)}>+</button></div></>;
}
