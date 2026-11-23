import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {drawingTools,symbolDrawings} from "./Drawings.jsx";
import PaneResize from "./PaneResize.jsx";
import PriceChart from "./PriceChart.jsx";
import { applyStudy, intervals, symbols } from "./chartData.js";
import { sessionStats } from "./chartMath.js";
import { HISTORY_SYMBOLS } from "./historyPaging.js";
import { foldForInterval, rebaseLive } from "./liveCandle.js";
import { usePaneBars, usePositions, useQuotes } from "./marketData.js";
import { conditionLabel, rearm, useAlertEngine, useAlerts } from "./alerts.js";
import { SYMBOL_PATTERN, describe } from "./controls/catalog.js";
import SymbolSearch from "./controls/SymbolSearch.jsx";
import { ChartTypeMenu, ContextMenu, INTERVAL_LABELS, LayoutMenu, TimeframeMenu } from "./controls/Menus.jsx";
import { IndicatorsDialog, StudySettingsDialog } from "./controls/IndicatorDialogs.jsx";
import { DrawingFlyout, DrawingToolbar, DRAWING_GROUPS, TOOL_SHORTCUTS } from "./controls/DrawingControls.jsx";
import { AlertDialog, ChartSettingsDialog, DEFAULT_SETTINGS } from "./controls/SettingsDialogs.jsx";
import Icon, { TYPE_ICON } from "./controls/icons.jsx";
import "./charts.css";

const key = "trading.chart.workspace.v1";
const counts = [1, 2, 4, 8];
const RSI_DEFAULT = {id:"rsi", type:"RSI", period:14, color:"#b58cff", width:1.5, visible:true};
const initialStudies = [
  {id:"fast", type:"EMA", period:9, color:"#f5b84a", source:"close", width:1.5, markers:true},
  {id:"slow", type:"EMA", period:15, color:"#7aa2ff", source:"close", width:1.5, markers:true},
  {id:"vwap", type:"VWAP", color:"#9aa7b6", width:1.5},
  {id:"volume", type:"Volume"},
].map(s=>({...s,visible:true})).concat(RSI_DEFAULT);
const DRAWING_NAMES = DRAWING_GROUPS.flatMap(([,n])=>n);
const toolIcon = Object.fromEntries(drawingTools);

// Saved layouts keep the v1 key; every newer field gets a default here.
function load() {
  const defaults = {count:1, active:0, side:true, bottom:true, rsiAdded:true, settings:DEFAULT_SETTINGS,
    favIntervals:["1m","5m","15m"], watchlist:symbols, recent:[], favoriteTools:[], lastTool:"Trend line"};
  try {
    const s = JSON.parse(localStorage.getItem(key));
    if (counts.includes(s?.count) && s.panes?.length===8 && s.panes[s.active] &&
      s.panes.every(p=>SYMBOL_PATTERN.test(p.symbol) && Object.hasOwn(intervals,p.interval) && Array.isArray(p.studies))) {
      if (!s.rsiAdded) s.panes = s.panes.map(p=>p.studies.some(x=>x.type==="RSI")?p:{...p,studies:[...p.studies,RSI_DEFAULT]});
      const watchlist = Array.isArray(s.watchlist) ? s.watchlist.filter(x=>SYMBOL_PATTERN.test(x)) : symbols;
      return {...defaults, side:s.watch ?? true, ...s, rsiAdded:true, watchlist, settings:{...DEFAULT_SETTINGS, ...(s.settings||{})}};
    }
  } catch { /* Storage may be unavailable. */ }
  return {...defaults, panes:["QQQ","QQQ","SPY","IWM","AAPL","NVDA","TSLA","MSFT"].map((symbol,i)=>({
    symbol, interval:["5m","15m","5m","15m"][i] || "5m", studies:initialStudies, group:""
  }))};
}

const fmt = (n, d=2) => n==null || !Number.isFinite(n) ? "—" : Number(n).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d});
const usd = n => n==null || !Number.isFinite(Number(n)) ? "—" : Number(n).toLocaleString("en-US",{style:"currency",currency:"USD"});
const signed = n => n==null || !Number.isFinite(n) ? "—" : (n>=0?"+":"")+fmt(n);
const tone = n => n==null ? "" : n>=0 ? "up" : "down";
const inField = el => el && (el.closest?.("input,select,textarea,[contenteditable=true]") || el.isContentEditable);

function positionFor(positions, symbol) {
  const p = positions?.find(p=>p.symbol===symbol && Number(p.quantity) && Number(p.average_open_price));
  return p ? {qty:Number(p.quantity), avg:Number(p.average_open_price)} : null;
}

// Serialize a pane's SVG to PNG; copy it where allowed, otherwise download it.
async function copyChartImage(svg, name) {
  const rect = svg.getBoundingClientRect();
  const clone = svg.cloneNode(true);
  const src = svg.querySelectorAll("text"), dst = clone.querySelectorAll("text");
  src.forEach((t,i)=>{ const cs=getComputedStyle(t); dst[i].setAttribute("fill", cs.fill); dst[i].setAttribute("font-size", cs.fontSize); dst[i].setAttribute("font-family", cs.fontFamily); });
  clone.setAttribute("width", rect.width); clone.setAttribute("height", rect.height);
  const bg = document.createElementNS("http://www.w3.org/2000/svg","rect");
  bg.setAttribute("width","100%"); bg.setAttribute("height","100%"); bg.setAttribute("fill","#0b0f14");
  clone.insertBefore(bg, clone.firstChild);
  const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(clone)], {type:"image/svg+xml"}));
  const img = new Image();
  await new Promise((ok, fail)=>{ img.onload=ok; img.onerror=fail; img.src=url; });
  const scale = window.devicePixelRatio || 1, canvas = document.createElement("canvas");
  canvas.width = rect.width*scale; canvas.height = rect.height*scale;
  const ctx = canvas.getContext("2d"); ctx.scale(scale, scale); ctx.drawImage(img, 0, 0, rect.width, rect.height);
  URL.revokeObjectURL(url);
  const blob = await new Promise(ok=>canvas.toBlob(ok, "image/png"));
  if (window.isSecureContext && navigator.clipboard?.write && window.ClipboardItem) {
    try { await navigator.clipboard.write([new window.ClipboardItem({"image/png":blob})]); return "Chart image copied"; } catch { /* fall back to download */ }
  }
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name+".png"; a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href), 2000);
  return "Clipboard unavailable here, so the chart image was downloaded";
}

function ChartPane({index, pane, selected, tool, positions, settings, onSettings, alerts, resetToken, onActivate, patch, chartPrices, onActiveData,
  drawingProps, selectedDrawing, onDrawingChange, onDrawingDelete, onDrawingCopy, onDrawingAlert, onEditStudy, onSearch, onContextMenu}) {
  const data = usePaneBars(pane.symbol, pane.interval, !selected);
  const {live, stale, note} = data;
  const history = HISTORY_SYMBOLS.includes(pane.symbol);
  const behind = !!data.hasMoreAfter; // history symbols only: newer bars are stored but not loaded, so the pane is not at the newest bar
  // Live trades accumulate into the newest candle (open stays the first live price, high/low widen) while the
  // pane shows the newest bars. New stored bars rebase the live state; each tick is folded once (deduped by ts).
  const tick = chartPrices?.[pane.symbol];
  const liveRef = useRef({key:"", base:null, bars:null, ts:0});
  const bars = useMemo(() => {
    const L = liveRef.current, key = pane.symbol+"|"+pane.interval;
    if (L.key !== key) Object.assign(L, {key, base:null, bars:null, ts:0});
    if (!data.bars || !live || behind) { L.bars = null; L.base = null; return data.bars; }
    let cur = L.bars && L.base === data.bars ? L.bars : rebaseLive(data.bars, L.bars);
    L.base = data.bars;
    if (tick && tick.ts !== L.ts) { L.ts = tick.ts; cur = foldForInterval(cur, tick.price, tick.ts, pane.interval); }
    L.bars = cur;
    return cur;
  }, [data.bars, live, behind, tick?.ts, tick?.price, pane.symbol, pane.interval]); // eslint-disable-line react-hooks/exhaustive-deps
  const latest = useRef(data);
  latest.current = data;
  useEffect(() => { // the toolbar drives the selected pane's own paging state
    if (selected) onActiveData({history, behind, goToDate:d=>latest.current.goToDate?.(d), backToLive:()=>latest.current.backToLive?.()});
  }, [selected, history, behind, pane.symbol, pane.interval]); // eslint-disable-line react-hooks/exhaustive-deps
  // Near-edge check keyed on view/bars changes in PriceChart; loadMore guards in-flight requests itself.
  const onNearEdge = side => data.loadMore?.(side);
  return <article className={"cx-pane "+(selected?"selected":"")} data-pane={index} tabIndex="0" aria-label={"Chart "+(index+1)+" "+pane.symbol}
    onFocus={onActivate} onPointerDown={e=>{onActivate(); if(e.target.closest("svg.cx-plot") && !e.target.closest("[data-drawing]")) drawingProps.onSelect(null);}}>
    <div className="cx-controls"><button className="cx-sym-btn" aria-label={"Change symbol for chart "+(index+1)} onClick={onSearch}>{pane.symbol} <span>▾</span></button>
    <select aria-label={"Timeframe for chart "+(index+1)} value={pane.interval} onChange={e=>patch({interval:e.target.value})}>{Object.keys(intervals).map(t=><option key={t}>{t}</option>)}</select>
    {bars && <span className={"cx-source "+(live&&!stale?"live":"sample")} title={note || undefined}>{!live?"Sample data":stale?"Tastytrade · stale":"Tastytrade"}</span>}
    <select aria-label={"Symbol link group for chart "+(index+1)} value={pane.group} onChange={e=>patch({group:e.target.value})}><option value="">Unlinked</option><option value="A">Link A</option><option value="B">Link B</option></select></div>
    {selectedDrawing && <DrawingToolbar key={selectedDrawing.id} drawing={selectedDrawing} onChange={onDrawingChange} onDelete={onDrawingDelete} onCopy={onDrawingCopy}
      onAlert={selectedDrawing.type==="Horizontal line" ? onDrawingAlert : null}/>}
    {bars ? <PriceChart key={pane.symbol+"-"+pane.interval} paneId={"p"+index} pane={pane} bars={bars} tool={tool} live={live && !stale}
      position={live ? positionFor(positions, pane.symbol) : null} settings={settings} onSettings={onSettings} alerts={alerts} resetToken={resetToken}
      onNearEdge={history ? onNearEdge : undefined} hasMoreBefore={!!data.hasMoreBefore} hasMoreAfter={behind} earliest={data.earliest}
      focusTime={data.focusTime} appended={data.appended || 0} appendedFor={data.bars}
      onRemove={id=>patch({studies:pane.studies.filter(study=>study.id!==id)})} onEditStudy={onEditStudy} onContextMenu={onContextMenu} drawingProps={drawingProps}/>
      : <div className="cx-loading" role="status">Loading {pane.symbol} {pane.interval} candles…</div>}
    <PaneResize direction="width"/><PaneResize direction="height"/></article>;
}

function SidePanel({active, watchlist, onPick, onRemove, onAdd, quotes, bars, live}) {
  const stats = bars ? sessionStats(bars) : null;
  const [side,setSide]=useState("Buy");
  const [type,setType]=useState("Limit");
  const [qty,setQty]=useState(10);
  const [price,setPrice]=useState("");
  const [tp,setTp]=useState("");
  const [sl,setSl]=useState("");
  const entry = Number(price) || stats?.last || 0;
  const dir = side==="Buy" ? 1 : -1;
  const risk = sl ? Math.abs(entry-Number(sl))*qty : null;
  const reward = tp ? Math.abs(Number(tp)-entry)*qty : null;
  const rangePct = stats && stats.high>stats.low ? (stats.last-stats.low)/(stats.high-stats.low)*100 : null;
  return <aside className="cx-side" aria-label="Watchlist and symbol details">
    <section className="cx-watch">
      <div className="cx-side-head"><h2>Watchlist</h2><span className="cx-side-actions">{(!quotes.live || quotes.stale) && <span className="cx-source sample" title={quotes.note || undefined}>{quotes.stale?"Stale":"Sample data"}</span>}
        <button className="cx-icon-btn" aria-label="Add symbol to watchlist" onClick={onAdd}>+</button></span></div>
      <table><thead><tr><th>Symbol</th><th>Last</th><th>Chg%</th><th aria-label="Remove"/></tr></thead><tbody>{watchlist.map(symbol=>{
        const q=quotes.quotes[symbol]||{};
        return <tr key={symbol} className={symbol===active.symbol?"current":""} onClick={()=>onPick(symbol)}>
          <td><button onClick={e=>{e.stopPropagation();onPick(symbol);}}>{symbol}</button></td>
          <td>{fmt(q.price)}</td><td className={tone(q.change_pct)}>{q.change_pct==null?"—":signed(q.change_pct)+"%"}</td>
          <td><button className="cx-row-x" aria-label={"Remove "+symbol+" from watchlist"} onClick={e=>{e.stopPropagation();onRemove(symbol);}}>×</button></td></tr>;
      })}</tbody></table>
    </section>
    <section className="cx-details" aria-label={active.symbol+" details"}>
      <div className="cx-side-head"><h2>{active.symbol}</h2>{bars && <span className={"cx-source "+(live?"live":"sample")}>{live?"Tastytrade":"Sample data"}</span>}</div>
      {describe(active.symbol) && <p className="cx-muted cx-sub">{describe(active.symbol).name} · {describe(active.symbol).exchange}</p>}
      {stats ? <>
        <div className="cx-price"><strong>{fmt(stats.last)}</strong><span className={tone(stats.change)}>{signed(stats.change)} ({stats.changePct==null?"—":signed(stats.changePct)+"%"})</span></div>
        <div className="cx-range"><div><span>{fmt(stats.low)}</span><span>Day range</span><span>{fmt(stats.high)}</span></div>
          <div className="cx-range-bar"><i style={{width:(rangePct??0)+"%"}}/><b style={{left:(rangePct??0)+"%"}}/></div></div>
        <dl className="cx-stats">
          <div><dt>Open</dt><dd>{fmt(stats.open)}</dd></div><div><dt>Prev close</dt><dd>{fmt(stats.prevClose)}</dd></div>
          <div><dt>VWAP</dt><dd>{fmt(stats.vwap)}</dd></div><div><dt>Volume</dt><dd>{Math.round(stats.volume).toLocaleString()}</dd></div>
        </dl>
      </> : <p className="cx-muted">Loading…</p>}
    </section>
    <section className="cx-ticket" aria-label="Order ticket preview">
      <div className="cx-side-head"><h2>Order ticket</h2><span className="cx-source sample">Preview</span></div>
      <div className="cx-seg" role="group" aria-label="Side">{["Buy","Sell"].map(s=><button key={s} aria-pressed={side===s} className={s.toLowerCase()} onClick={()=>setSide(s)}>{s}</button>)}</div>
      <div className="cx-seg" role="group" aria-label="Order type">{["Limit","Market","Stop"].map(t=><button key={t} aria-pressed={type===t} onClick={()=>setType(t)}>{t}</button>)}</div>
      <div className="cx-fields">
        <label>Price<input type="number" step="0.01" disabled={type==="Market"} placeholder={fmt(stats?.last)} value={price} onChange={e=>setPrice(e.target.value)}/></label>
        <label>Quantity<input type="number" min="1" value={qty} onChange={e=>setQty(Math.max(1,Math.floor(+e.target.value)||1))}/></label>
        <label>Take profit<input type="number" step="0.01" value={tp} placeholder={stats?fmt(stats.last+dir*stats.last*0.004):""} onChange={e=>setTp(e.target.value)}/></label>
        <label>Stop loss<input type="number" step="0.01" value={sl} placeholder={stats?fmt(stats.last-dir*stats.last*0.002):""} onChange={e=>setSl(e.target.value)}/></label>
      </div>
      <div className="cx-rr"><span>Risk {risk==null?"—":usd(risk)} · Reward {reward==null?"—":usd(reward)}</span><b>R:R {risk && reward ? (reward/risk).toFixed(2) : "—"}</b></div>
      <button className="cx-submit" disabled title="Manual order routing is not connected to this ticket">{side} {qty} {active.symbol} · not connected</button>
      <p className="cx-muted">Preview only. Orders are placed by strategies through the existing confirmation and live-order checks.</p>
    </section>
  </aside>;
}

function BottomPanel({positions, positionsNote, quotes, account, accountError, alerts, onRearm, onRemoveAlert}) {
  const [tab,setTab]=useState("Positions");
  const activeAlerts = alerts.filter(a=>a.active).length;
  return <section className="cx-bottom" aria-label="Positions, account and alerts">
    <div className="cx-tabs" role="tablist">{["Positions","Account","Alerts"].map(t=><button key={t} role="tab" aria-selected={tab===t} onClick={()=>setTab(t)}>{t}
      {t==="Positions" && positions?.length ? <span>{positions.length}</span> : null}{t==="Alerts" && activeAlerts ? <span>{activeAlerts}</span> : null}</button>)}
      <span className="cx-tabs-note">{tab==="Alerts" ? "Notify only · checked while this chart is open" : "Alpaca paper"}</span></div>
    {tab==="Positions" && (positions==null
      ? <p className="cx-muted cx-pad">Positions unavailable{positionsNote?": "+positionsNote:""}.</p>
      : !positions.length ? <p className="cx-muted cx-pad">{positionsNote || "No open positions."}</p>
      : <table className="cx-table"><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Avg price</th><th>Last</th><th>P&amp;L</th></tr></thead>
        <tbody>{positions.map(p=>{
          const qty=Number(p.quantity), avg=Number(p.average_open_price);
          const stock=!/option/i.test(p.instrument_type||"");
          const last=stock && quotes.live && !quotes.stale ? quotes.quotes[p.symbol]?.price : null;
          const pnl=last!=null && avg ? (last-avg)*qty : null;
          return <tr key={p.symbol+p.account}><td><b>{p.symbol}</b></td><td className={qty>=0?"up":"down"}>{qty>=0?"Long":"Short"}</td><td>{fmt(Math.abs(qty),0)}</td><td>{fmt(avg)}</td><td>{fmt(last)}</td><td className={tone(pnl)}>{pnl==null?"—":(pnl>=0?"+":"")+usd(pnl)}</td></tr>;
        })}</tbody></table>)}
    {tab==="Account" && <div className="cx-account">{accountError && !account ? <p className="cx-muted">Account unavailable: {accountError}</p> : <>
        <div><span>Net liquidation</span><b>{usd(account?.net_liquidation)}</b></div>
        <div><span>Buying power</span><b>{usd(account?.buying_power)}</b></div>
        <div><span>Unrealized P&amp;L</span><b className={tone(account?.unrealized_pnl)}>{usd(account?.unrealized_pnl)}</b></div>
        <div><span>Realized P&amp;L</span><b className={tone(account?.realized_pnl)}>{usd(account?.realized_pnl)}</b></div>
      </>}</div>}
    {tab==="Alerts" && (!alerts.length ? <p className="cx-muted cx-pad">No alerts. Right-click the chart or press Alt A to add one.</p>
      : <table className="cx-table"><thead><tr><th>Symbol</th><th>Condition</th><th>Trigger</th><th>Expires</th><th>Status</th><th aria-label="Actions"/></tr></thead>
        <tbody>{alerts.map(a=><tr key={a.id}><td><b>{a.symbol}</b></td><td>{conditionLabel(a.condition)} {fmt(a.value)}</td><td>{a.trigger==="every"?"Every time":"Once"}</td>
          <td>{a.expires ? new Date(a.expires).toLocaleString("en-US",{timeZone:"America/New_York",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})+" ET" : "Never"}</td>
          <td className={a.active?"up":""}>{a.active?"Active":(a.status||"Off")}{a.fires?` · fired ${a.fires}×`:""}</td>
          <td className="cx-row-actions">{!a.active && <button onClick={()=>onRearm(a.id)}>Re-arm</button>}<button className="danger" onClick={()=>onRemoveAlert(a.id)}>Remove</button></td></tr>)}</tbody></table>)}
  </section>;
}

export default function ChartWorkspace({account, accountError, onOpenBuilder, chartPrices, onChartSymbols}) {
  const [s,set]=useState(load);
  const [tool,setTool]=useState("Crosshair");
  const [selected,setSelected]=useState(null);
  const [magnet,setMagnet]=useState(false);
  const [locked,setLocked]=useState(false);
  const [hidden,setHidden]=useState(false);
  const [repeat,setRepeat]=useState(false);
  const [open,setOpen]=useState(null); // which menu/dialog is open
  const [search,setSearch]=useState(null); // {target: pane index | "watch", query}
  const [editStudy,setEditStudy]=useState(null); // {pane, id}
  const [alertDraft,setAlertDraft]=useState(null); // {symbol, price}
  const [ctx,setCtx]=useState(null); // context menu
  const [resets,setResets]=useState({});
  const [activeCtl,setActiveCtl]=useState(null); // selected pane: {history, behind, goToDate, backToLive}
  const [toasts,setToasts]=useState([]);
  const refs={symbol:useRef(null), timeframe:useRef(null), type:useRef(null), layout:useRef(null), draw:useRef(null)};
  useEffect(()=>{try{localStorage.setItem(key,JSON.stringify(s));}catch{/* Private browsing. */}},[s]);
  const active=s.panes[s.active];
  const start=Math.floor(s.active/s.count)*s.count;
  const ids=Array.from({length:s.count},(_,i)=>start+i);
  const activeData=usePaneBars(active.symbol, active.interval);
  // Tell the server which symbols the visible panes show so it can push their live prices.
  const symbolsKey=[...new Set(ids.map(i=>s.panes[i].symbol))].sort().join(",");
  useEffect(()=>{ onChartSymbols?.(symbolsKey.split(",")); return ()=>onChartSymbols?.([]); },[symbolsKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const today=(d=>d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0"))(new Date());
  const [alerts,setAlerts]=useAlerts();
  const quoteSymbols=[...new Set([...s.watchlist, ...alerts.filter(a=>a.active).map(a=>a.symbol)])];
  const quotes=useQuotes(quoteSymbols.length?quoteSymbols:["QQQ"]);
  const {positions, note:positionsNote}=usePositions();
  const notify=text=>{const id=Date.now()+Math.random(); setToasts(t=>[...t,{id,text}]); setTimeout(()=>setToasts(t=>t.filter(x=>x.id!==id)),8000);};
  useAlertEngine(alerts, setAlerts, quotes, (a, muted)=>notify(`${a.symbol} alert · ${a.message} · last ${fmt(a.price)}${muted?" (sound blocked by the browser until you click the page)":""}`));

  function patch(i,changes) {
    set(old=>({...old,active:i,panes:old.panes.map((p,j)=>{
      if(i===j) return {...p,...changes};
      if(changes.symbol && p.group && p.group===old.panes[i].group) return {...p,symbol:changes.symbol};
      return p;
    })}));
  }
  const setSettings = patch2 => set(old=>({...old, settings:{...old.settings, ...patch2}}));
  const pickSymbol = (i, symbol) => { patch(i,{symbol}); set(old=>({...old, recent:[symbol, ...old.recent.filter(x=>x!==symbol)].slice(0,8)})); setSelected(null); };
  const addWatch = symbol => set(old=>old.watchlist.includes(symbol)?old:({...old, watchlist:[...old.watchlist, symbol]}));
  const chooseTool = name => { setTool(name); if(DRAWING_NAMES.includes(name)) set(old=>({...old,lastTool:name})); if(name!=="Pan" && name!=="Crosshair"){setLocked(false);setHidden(false);} };
  const lastPrice = () => activeData.bars?.at(-1)?.close ?? null;
  const resetPane = i => setResets(r=>({...r,[i]:(r[i]||0)+1}));
  const drawingsOf = p => symbolDrawings(p);
  const saveDrawings = (i, items) => { const p=s.panes[i]; patch(i,{drawings:{...p.drawings,[p.symbol]:items}}); };

  // Keyboard: Alt T/H/F tools, Alt A alert, Alt R reset, Esc clears, letters open search.
  useEffect(()=>{
    function onKey(e) {
      if (inField(e.target) || document.querySelector(".cx-layer:not(.cx-toasts)")) return;
      // Only when focus is on the page itself or inside the chart, never the sidebar or Emergency stop.
      if (e.target !== document.body && !e.target.closest?.(".cx")) return;
      if (e.altKey && !e.metaKey && !e.ctrlKey) {
        if (TOOL_SHORTCUTS[e.code]) { e.preventDefault(); chooseTool(TOOL_SHORTCUTS[e.code]); return; }
        if (e.code==="KeyA") { e.preventDefault(); setAlertDraft({symbol:active.symbol, price:lastPrice()}); return; }
        if (e.code==="KeyR") { e.preventDefault(); resetPane(s.active); return; }
      }
      if (e.key==="Escape") { setSelected(null); setTool("Crosshair"); return; }
      if (!e.altKey && !e.metaKey && !e.ctrlKey && /^[a-zA-Z]$/.test(e.key)) { e.preventDefault(); setSearch({target:s.active, query:e.key.toUpperCase()}); }
    }
    window.addEventListener("keydown", onKey);
    return ()=>window.removeEventListener("keydown", onKey);
  });

  const live=activeData.bars && activeData.live;
  const editPane = editStudy ? s.panes[editStudy.pane] : null;
  const studyBeingEdited = editPane?.studies.find(x=>x.id===editStudy.id);
  const ctxPane = ctx ? s.panes[ctx.pane] : null;

  return <section className="cx">
    <div className="cx-head"><div className="cx-instrument">
      <button ref={refs.symbol} className="cx-sym-main" aria-label="Search symbol" onClick={()=>setSearch({target:s.active, query:""})}><b>{active.symbol}</b><span>{describe(active.symbol)?.name || "Search symbol"}</span></button>
      <div className="cx-intervals">{s.favIntervals.map(t=><button key={t} aria-pressed={active.interval===t} onClick={()=>patch(s.active,{interval:t})}>{t}</button>)}
        {!s.favIntervals.includes(active.interval) && <button aria-pressed="true">{active.interval}</button>}
        <button ref={refs.timeframe} className="cx-chev" aria-label="All timeframes" aria-expanded={open==="timeframe"} onClick={()=>setOpen(open==="timeframe"?null:"timeframe")}><Icon name="chevron" size={14}/></button></div>
      <button ref={refs.type} className="cx-tool-btn" aria-label={"Chart type: "+s.settings.type} title={s.settings.type} aria-expanded={open==="type"} onClick={()=>setOpen(open==="type"?null:"type")}><Icon name={TYPE_ICON[s.settings.type]||"candles"} size={18}/></button>
      <button className="cx-tool-btn" aria-expanded={open==="indicators"} onClick={()=>setOpen("indicators")}>ƒx Indicators</button>
      <button className="cx-tool-btn" onClick={()=>setAlertDraft({symbol:active.symbol, price:lastPrice()})}><Icon name="bell"/> Alert</button>
    </div>
    <div className="cx-tools">
      {s.favoriteTools.length>0 && <div className="cx-favorites" role="toolbar" aria-label="Favorite drawing tools">{s.favoriteTools.map(name=><button key={name} title={name} aria-label={"Favorite: "+name} aria-pressed={tool===name} onClick={()=>chooseTool(name)}>{toolIcon[name]}</button>)}</div>}
      <button ref={refs.layout} className="cx-tool-btn" aria-label={"Layout: "+s.count+" charts"} aria-expanded={open==="layout"} onClick={()=>setOpen(open==="layout"?null:"layout")}><Icon name="layout"/> {s.count}</button>
      {activeCtl?.history && <label className="cx-goto">Go to date<input type="date" min="2022-01-03" max={today} aria-label="Go to date"
        onChange={e=>{ if(e.target.value) activeCtl.goToDate(e.target.value); }}/></label>}
      {activeCtl?.history && activeCtl.behind && <button className="cx-tool-btn cx-live-btn" title="Jump back to the newest bars" onClick={()=>activeCtl.backToLive()}>Live</button>}
      <button aria-pressed={s.side} onClick={()=>set({...s,side:!s.side})}>Watchlist</button>
      <button aria-pressed={s.bottom} onClick={()=>set({...s,bottom:!s.bottom})}>Positions</button>
      <button className="cx-icon-btn" aria-label="Chart settings" title="Chart settings" onClick={()=>setOpen("settings")}><Icon name="gear"/></button>
      {activeData.bars && <span className={"cx-source "+(live&&!activeData.stale?"live":"sample")} title={activeData.note || undefined}>{!live?"Sample market data":activeData.stale?"Tastytrade · stale, refresh failed":"Live · Tastytrade"}</span>}</div></div>
    {activeData.bars && !live && <div className="cx-banner" role="status">Showing fixed sample candles, not market data{activeData.note?" · "+activeData.note:""}</div>}
    <div className="cx-body"><div className="cx-rail" role="group" aria-label="Chart tools">
      {[["Crosshair",toolIcon.Crosshair],["Pan",toolIcon.Pan]].map(([name,icon])=><button key={name} title={name} aria-label={name} aria-pressed={tool===name} onClick={()=>chooseTool(name)}>{icon}</button>)}
      <button ref={refs.draw} className="cx-rail-draw" title={"Drawing tools ("+s.lastTool+")"} aria-label="Drawing tools" aria-pressed={DRAWING_NAMES.includes(tool)} aria-expanded={open==="draw"}
        onClick={()=>setOpen(open==="draw"?null:"draw")}>{toolIcon[DRAWING_NAMES.includes(tool)?tool:s.lastTool]}<small>▸</small></button>
      <button title={s.lastTool} aria-label={"Use "+s.lastTool} aria-pressed={tool===s.lastTool} onClick={()=>chooseTool(s.lastTool)}>✎</button>
      <hr/>{[["Snap to OHLC","⌁",magnet,setMagnet],["Keep drawing","⟳",repeat,setRepeat],["Lock drawings","⚿",locked,setLocked],["Hide drawings","◉",hidden,setHidden]].map(([name,icon,on,toggle])=><button key={name} aria-label={name} title={name} aria-pressed={on} onClick={()=>toggle(!on)}>{icon}</button>)}
      <button title="Delete selected drawing" aria-label="Delete selected drawing" disabled={locked||hidden||!drawingsOf(active).some(d=>d.id===selected)} onClick={()=>{saveDrawings(s.active, drawingsOf(active).filter(d=>d.id!==selected));setSelected(null);}}>⌫</button></div>
      <div className="cx-center"><div key={s.count} className={"cx-grid cx-count-"+s.count}>{ids.map(i=>{
        const p=s.panes[i];
        const sel = s.active===i ? drawingsOf(p).find(d=>d.id===selected) : null;
        const updateSel = changes => saveDrawings(i, drawingsOf(p).map(d=>d.id===selected?{...d,...changes}:d));
        return <ChartPane key={i} index={i} pane={p} selected={s.active===i} tool={tool} positions={positions} settings={s.settings} onSettings={setSettings}
          alerts={alerts.filter(a=>a.symbol===p.symbol && (a.active || a.status==="Triggered"))} resetToken={resets[i]}
          chartPrices={chartPrices} onActiveData={setActiveCtl}
          onActivate={()=>set(old=>old.active===i?old:{...old,active:i})} patch={changes=>patch(i,changes)}
          onSearch={()=>setSearch({target:i, query:""})} onEditStudy={id=>setEditStudy({pane:i,id})}
          onContextMenu={info=>setCtx({...info, pane:i})}
          selectedDrawing={sel && !hidden && !locked ? sel : null} onDrawingChange={updateSel}
          onDrawingDelete={()=>{saveDrawings(i, drawingsOf(p).filter(d=>d.id!==selected)); setSelected(null);}}
          onDrawingCopy={()=>{const copy={...sel, id:globalThis.crypto?.randomUUID?.()||String(Date.now()), locked:false, points:sel.points.map(pt=>({...pt, price:pt.price*1.002}))}; saveDrawings(i,[...drawingsOf(p), copy]); setSelected(copy.id);}}
          onDrawingAlert={()=>setAlertDraft({symbol:p.symbol, price:sel.points[0].price})}
          drawingProps={{
            magnet,locked,hidden,selected:s.active===i?selected:null,
            onSelect:id=>{set(old=>old.active===i?old:{...old,active:i});setSelected(id);},
            save:items=>saveDrawings(i, items),
            finish:()=>{if(!repeat)setTool("Crosshair");}
          }}/>;
      })}</div>
      {s.bottom && <BottomPanel positions={positions} positionsNote={positionsNote} quotes={quotes} account={account} accountError={accountError}
        alerts={alerts} onRearm={id=>setAlerts(list=>list.map(a=>a.id===id?rearm(a):a))}
        onRemoveAlert={id=>setAlerts(list=>list.filter(a=>a.id!==id))}/>}</div>
    {s.side && <SidePanel active={active} watchlist={s.watchlist} onPick={symbol=>pickSymbol(s.active, symbol)} onAdd={()=>setSearch({target:"watch", query:""})}
      onRemove={symbol=>set(old=>({...old, watchlist:old.watchlist.filter(x=>x!==symbol)}))} quotes={quotes} bars={activeData.bars} live={live}/>}</div>
    <footer className="cx-status">Eastern time · {live?"Candles from Tastytrade, refreshed automatically":"Sample market data"} · Alt T/H/F draw · Alt A alert · Alt R reset · type to search</footer>

    <SymbolSearch open={!!search} onClose={()=>setSearch(null)} initialQuery={search?.query || ""} recent={s.recent}
      target={search?.target==="watch" ? "the watchlist" : "chart "+((search?.target ?? 0)+1)}
      onPick={symbol=>search?.target==="watch" ? addWatch(symbol) : pickSymbol(search.target, symbol)} onAddWatch={addWatch}/>
    <TimeframeMenu open={open==="timeframe"} onClose={()=>setOpen(null)} anchorRef={refs.timeframe} value={active.interval} favorites={s.favIntervals}
      onPick={t=>patch(s.active,{interval:t})} onToggleFavorite={t=>set(old=>({...old, favIntervals:old.favIntervals.includes(t)?old.favIntervals.filter(x=>x!==t):Object.keys(intervals).filter(x=>x===t||old.favIntervals.includes(x))}))}/>
    <ChartTypeMenu open={open==="type"} onClose={()=>setOpen(null)} anchorRef={refs.type} value={s.settings.type} onPick={t=>setSettings({type:t})}/>
    <LayoutMenu open={open==="layout"} onClose={()=>setOpen(null)} anchorRef={refs.layout} count={s.count} panes={s.panes} start={start}
      onCount={n=>set(old=>({...old,count:n}))} onLink={(i,g)=>patch(i,{group:g})} sync={s.settings.syncCrosshair} onSync={v=>setSettings({syncCrosshair:v})}/>
    <DrawingFlyout open={open==="draw"} onClose={()=>setOpen(null)} anchorRef={refs.draw} tool={tool} onPick={chooseTool} favorites={s.favoriteTools}
      onToggleFavorite={n=>set(old=>({...old, favoriteTools:old.favoriteTools.includes(n)?old.favoriteTools.filter(x=>x!==n):[...old.favoriteTools,n]}))}/>
    <IndicatorsDialog open={open==="indicators"} onClose={()=>setOpen(null)} pane={active} onChange={studies=>patch(s.active,{studies})}
      preset={s.preset} onSavePreset={()=>{set(old=>({...old,preset:active.studies}));notify("Indicator preset saved");}} onLoadPreset={()=>patch(s.active,{studies:s.preset})}
      onEdit={id=>setEditStudy({pane:s.active,id})} onApplyAll={()=>{set(old=>({...old,panes:old.panes.map((p,i)=>ids.includes(i)?{...p,studies:active.studies}:p)}));notify("Indicators applied to all visible panes");}}/>
    <StudySettingsDialog open={!!studyBeingEdited} onClose={()=>setEditStudy(null)} study={studyBeingEdited}
      onChange={st=>patch(editStudy.pane,{studies:editPane.studies.map(x=>x.id===st.id?st:x)})}
      onApplyAll={st=>{set(old=>({...old,panes:old.panes.map((p,i)=>ids.includes(i)?{...p, studies:applyStudy(p.studies, st)}:p)}));notify(st.type+" settings applied to all visible panes");}}/>
    <ChartSettingsDialog open={open==="settings"} onClose={()=>setOpen(null)} settings={s.settings} onSave={v=>set(old=>({...old,settings:v}))}/>
    <AlertDialog open={!!alertDraft} onClose={()=>setAlertDraft(null)} symbol={alertDraft?.symbol || active.symbol} price={alertDraft?.price}
      onCreate={a=>{setAlerts(list=>[a,...list]);notify(`Alert created: ${a.symbol} ${conditionLabel(a.condition).toLowerCase()} ${fmt(a.value)}`);}}/>
    {ctx && <ContextMenu at={{x:ctx.x,y:ctx.y}} onClose={()=>setCtx(null)} symbol={ctxPane.symbol} price={ctx.price}
      onAlert={()=>setAlertDraft({symbol:ctxPane.symbol, price:ctx.price})}
      onLine={()=>saveDrawings(ctx.pane,[...drawingsOf(ctxPane),{id:globalThis.crypto?.randomUUID?.()||String(Date.now()),type:"Horizontal line",points:[{time:ctx.time,price:ctx.price}]}])}
      onBuilder={onOpenBuilder ? ()=>onOpenBuilder(ctxPane.symbol) : null}
      onReset={()=>resetPane(ctx.pane)}
      onCopy={()=>{const svg=document.querySelector(`.cx-pane[data-pane="${ctx.pane}"] svg.cx-plot`); if(svg) copyChartImage(svg, ctxPane.symbol+"-"+ctxPane.interval).then(notify).catch(()=>notify("Could not create the chart image"));}}
      drawingsHidden={hidden} onToggleDrawings={()=>setHidden(h=>!h)} hasDrawings={drawingsOf(ctxPane).length>0}
      onRemoveDrawings={()=>{ if(window.confirm(`Remove all ${drawingsOf(ctxPane).length} drawings on ${ctxPane.symbol}? This cannot be undone.`)) { saveDrawings(ctx.pane,[]); setSelected(null);} }}
      onSettings={()=>setOpen("settings")}/>}
    {toasts.length>0 && createPortal(<div className="cx-layer cx-toasts" role="status" aria-live="polite">{toasts.map(t=><div key={t.id} className="cx-toast">{t.text}<button aria-label="Dismiss" onClick={()=>setToasts(x=>x.filter(y=>y.id!==t.id))}>✕</button></div>)}</div>, document.body)}
  </section>;
}
