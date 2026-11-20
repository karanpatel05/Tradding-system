import { useEffect, useState } from "react";
import {drawingTools,symbolDrawings} from "./Drawings.jsx";
import PaneResize from "./PaneResize.jsx";
import PriceChart from "./PriceChart.jsx";
import { intervals, symbols } from "./chartData.js";
import { sessionStats } from "./chartMath.js";
import { useBars, usePositions, useQuotes } from "./marketData.js";
import "./charts.css";

const key = "trading.chart.workspace.v1";
const counts = [1, 2, 4, 8];
const RSI_DEFAULT = {id:"rsi", type:"RSI", period:14, color:"#b58cff", visible:true};
const initialStudies = [
  {id:"fast", type:"EMA", period:9, color:"#f5b84a"},
  {id:"slow", type:"EMA", period:15, color:"#7aa2ff"},
  {id:"vwap", type:"VWAP", color:"#9aa7b6"},
  {id:"volume", type:"Volume"},
].map(s=>({...s,visible:true})).concat(RSI_DEFAULT);

function load() {
  try {
    const s = JSON.parse(localStorage.getItem(key));
    if (counts.includes(s?.count) && s.panes?.length===8 && s.panes[s.active] &&
      s.panes.every(p=>symbols.includes(p.symbol) && Object.hasOwn(intervals,p.interval) && Array.isArray(p.studies))) {
      // One-time upgrade of saved layouts: add the RSI pane and the side/bottom panels.
      if (!s.rsiAdded) s.panes = s.panes.map(p=>p.studies.some(x=>x.type==="RSI")?p:{...p,studies:[...p.studies,RSI_DEFAULT]});
      return {side:s.watch ?? true, bottom:true, ...s, rsiAdded:true};
    }
  } catch { /* Storage may be unavailable. */ }
  return {count:1, active:0, side:true, bottom:true, rsiAdded:true, panes:["QQQ","QQQ","SPY","IWM","AAPL","NVDA","TSLA","MSFT"].map((symbol,i)=>({
    symbol, interval:["5m","15m","5m","15m"][i] || "5m", studies:initialStudies, group:""
  }))};
}

const fmt = (n, d=2) => n==null || !Number.isFinite(n) ? "—" : Number(n).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d});
const usd = n => n==null || !Number.isFinite(Number(n)) ? "—" : Number(n).toLocaleString("en-US",{style:"currency",currency:"USD"});
const signed = n => n==null || !Number.isFinite(n) ? "—" : (n>=0?"+":"")+fmt(n);
const tone = n => n==null ? "" : n>=0 ? "up" : "down";

function Indicators({pane, update, apply, close, preset, savePreset}) {
  const [type,setType]=useState("EMA");
  const [period,setPeriod]=useState(9);
  const change=(id,patch)=>update(pane.studies.map(s=>s.id===id?{...s,...patch}:s));
  const periodic = t => t==="EMA" || t==="RSI";
  return <section className="cx-studies" aria-label="Indicator settings">
    <div className="cx-row"><strong>Indicators · {pane.symbol} {pane.interval}</strong><button onClick={close}>Done</button></div>
    {pane.studies.map(s=><div className="cx-row" key={s.id}>
      <label><input type="checkbox" checked={s.visible} onChange={e=>change(s.id,{visible:e.target.checked})}/> {s.type}</label>
      {periodic(s.type) && <input aria-label={s.type+" period"} type="number" min="1" max="500" value={s.period} onChange={e=>change(s.id,{period:Math.max(1,Math.min(500,Math.floor(+e.target.value)))})}/>}
      {s.type!=="Volume" && <input aria-label="Line color" type="color" value={s.color} onChange={e=>change(s.id,{color:e.target.value})}/>}
      <button onClick={()=>update(pane.studies.filter(item=>item.id!==s.id))}>Remove</button>
    </div>)}
    <div className="cx-row"><select aria-label="New indicator" value={type} onChange={e=>{setType(e.target.value);setPeriod(e.target.value==="RSI"?14:9);}}>{["EMA","VWAP","Volume","RSI"].map(t=><option key={t}>{t}</option>)}</select>
    {periodic(type) && <input type="number" aria-label={"New "+type+" period"} min="1" max="500" value={period} onChange={e=>setPeriod(e.target.value)}/>}
    <button disabled={type==="EMA"? !(Number.isInteger(+period)&&+period>=1&&+period<=500):pane.studies.some(s=>s.type===type)} onClick={()=>update([...pane.studies,{id:globalThis.crypto?.randomUUID?.() || String(Date.now()),type,period:+period,color:type==="RSI"?"#b58cff":"#7aa2ff",visible:true}])}>Add</button></div>
    <div className="cx-row"><button onClick={savePreset}>Save as preset</button><button disabled={!preset} onClick={()=>update(preset)}>Load preset</button><button onClick={apply}>Apply to all visible charts</button></div>
  </section>;
}

// The broker position (if any) in this pane's symbol, for the chart's entry line.
function positionFor(positions, symbol) {
  const p = positions?.find(p=>p.symbol===symbol && Number(p.quantity) && Number(p.average_open_price));
  return p ? {qty:Number(p.quantity), avg:Number(p.average_open_price)} : null;
}

function ChartPane({index, pane, selected, tool, positions, onActivate, patch, drawingProps}) {
  const {bars, live, stale, note} = useBars(pane.symbol, pane.interval, !selected);
  return <article className={"cx-pane "+(selected?"selected":"")} tabIndex="0" aria-label={"Chart "+(index+1)+" "+pane.symbol} onFocus={onActivate} onPointerDown={onActivate}>
    <div className="cx-controls"><select aria-label={"Symbol for chart "+(index+1)} value={pane.symbol} onChange={e=>patch({symbol:e.target.value})}>{symbols.map(t=><option key={t}>{t}</option>)}</select>
    <select aria-label={"Timeframe for chart "+(index+1)} value={pane.interval} onChange={e=>patch({interval:e.target.value})}>{Object.keys(intervals).map(t=><option key={t}>{t}</option>)}</select>
    {bars && <span className={"cx-source "+(live&&!stale?"live":"sample")} title={note || undefined}>{!live?"Sample data":stale?"Tastytrade · stale":"Tastytrade"}</span>}
    <select aria-label={"Symbol link group for chart "+(index+1)} value={pane.group} onChange={e=>patch({group:e.target.value})}><option value="">Unlinked</option><option value="A">Link A</option><option value="B">Link B</option></select></div>
    {bars ? <PriceChart key={pane.symbol+"-"+pane.interval} pane={pane} bars={bars} tool={tool} position={live ? positionFor(positions, pane.symbol) : null}
      onRemove={id=>patch({studies:pane.studies.filter(study=>study.id!==id)})} drawingProps={drawingProps}/>
      : <div className="cx-loading" role="status">Loading {pane.symbol} {pane.interval} candles…</div>}
    <PaneResize direction="width"/><PaneResize direction="height"/></article>;
}

function SidePanel({active, onPick, quotes, bars, live}) {
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
      <div className="cx-side-head"><h2>Watchlist</h2>{(!quotes.live || quotes.stale) && <span className="cx-source sample" title={quotes.note || undefined}>{quotes.stale?"Stale":"Sample data"}</span>}</div>
      <table><thead><tr><th>Symbol</th><th>Last</th><th>Chg%</th></tr></thead><tbody>{symbols.map(symbol=>{
        const q=quotes.quotes[symbol]||{};
        return <tr key={symbol} className={symbol===active.symbol?"current":""} onClick={()=>onPick(symbol)}>
          <td><button onClick={e=>{e.stopPropagation();onPick(symbol);}}>{symbol}</button></td>
          <td>{fmt(q.price)}</td><td className={tone(q.change_pct)}>{q.change_pct==null?"—":signed(q.change_pct)+"%"}</td></tr>;
      })}</tbody></table>
    </section>
    <section className="cx-details" aria-label={active.symbol+" details"}>
      <div className="cx-side-head"><h2>{active.symbol}</h2>{bars && <span className={"cx-source "+(live?"live":"sample")}>{live?"Tastytrade":"Sample data"}</span>}</div>
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

function BottomPanel({positions, positionsNote, quotes, account, accountError}) {
  const [tab,setTab]=useState("Positions");
  return <section className="cx-bottom" aria-label="Positions and account">
    <div className="cx-tabs" role="tablist">{["Positions","Account"].map(t=><button key={t} role="tab" aria-selected={tab===t} onClick={()=>setTab(t)}>{t}{t==="Positions" && positions?.length ? <span>{positions.length}</span> : null}</button>)}
      <span className="cx-tabs-note">Alpaca paper</span></div>
    {tab==="Positions" ? (positions==null
      ? <p className="cx-muted cx-pad">Positions unavailable{positionsNote?": "+positionsNote:""}.</p>
      : !positions.length ? <p className="cx-muted cx-pad">{positionsNote || "No open positions."}</p>
      : <table className="cx-table"><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Avg price</th><th>Last</th><th>P&amp;L</th></tr></thead>
        <tbody>{positions.map(p=>{
          const qty=Number(p.quantity), avg=Number(p.average_open_price);
          const stock=!/option/i.test(p.instrument_type||"");
          const last=stock ? quotes.live ? quotes.quotes[p.symbol]?.price : null : null;
          const pnl=last!=null && avg ? (last-avg)*qty : null;
          return <tr key={p.symbol+p.account}><td><b>{p.symbol}</b></td><td className={qty>=0?"up":"down"}>{qty>=0?"Long":"Short"}</td><td>{fmt(Math.abs(qty),0)}</td><td>{fmt(avg)}</td><td>{fmt(last)}</td><td className={tone(pnl)}>{pnl==null?"—":(pnl>=0?"+":"")+usd(pnl)}</td></tr>;
        })}</tbody></table>)
    : <div className="cx-account">{accountError && !account ? <p className="cx-muted">Account unavailable: {accountError}</p> : <>
        <div><span>Net liquidation</span><b>{usd(account?.net_liquidation)}</b></div>
        <div><span>Buying power</span><b>{usd(account?.buying_power)}</b></div>
        <div><span>Unrealized P&amp;L</span><b className={tone(account?.unrealized_pnl)}>{usd(account?.unrealized_pnl)}</b></div>
        <div><span>Realized P&amp;L</span><b className={tone(account?.realized_pnl)}>{usd(account?.realized_pnl)}</b></div>
      </>}</div>}
  </section>;
}

export default function ChartWorkspace({account, accountError}) {
  const [s,set]=useState(load);
  const [tool,setTool]=useState("Crosshair");
  const [selected,setSelected]=useState(null);
  const [magnet,setMagnet]=useState(false);
  const [locked,setLocked]=useState(false);
  const [hidden,setHidden]=useState(false);
  const [repeat,setRepeat]=useState(false);
  const [favoriteMenu,setFavoriteMenu]=useState(false);
  const favorites=s.favoriteTools ?? [];
  const [editor,setEditor]=useState(false);
  useEffect(()=>{try{localStorage.setItem(key,JSON.stringify(s));}catch{/* Private browsing. */}},[s]);
  const active=s.panes[s.active];
  const start=Math.floor(s.active/s.count)*s.count;
  const ids=Array.from({length:s.count},(_,i)=>start+i);
  const activeData=useBars(active.symbol, active.interval);
  const quotes=useQuotes(symbols);
  const {positions, note:positionsNote}=usePositions();
  function patch(i,changes) {
    set(old=>({...old,active:i,panes:old.panes.map((p,j)=>{
      if(i===j) return {...p,...changes};
      if(changes.symbol && p.group && p.group===old.panes[i].group) return {...p,symbol:changes.symbol};
      return p;
    })}));
  }
  const live=activeData.bars && activeData.live;
  return <section className="cx"><div className="cx-head"><div className="cx-instrument"><select aria-label="Active chart symbol" value={active.symbol} onChange={e=>patch(s.active,{symbol:e.target.value})}>{symbols.map(t=><option key={t}>{t}</option>)}</select>
    <div className="cx-intervals">{Object.keys(intervals).map(t=><button key={t} aria-pressed={active.interval===t} onClick={()=>patch(s.active,{interval:t})}>{t}</button>)}</div></div>
    <div className="cx-tools"><div className="cx-layout" role="group" aria-label="Chart layout">{counts.map(n=><button key={n} aria-label={n+" chart layout"} aria-pressed={s.count===n} onClick={()=>set({...s,count:n})}>{n}</button>)}</div>
    <button aria-expanded={editor} onClick={()=>setEditor(!editor)}>Indicators</button>
    <button aria-pressed={s.side} onClick={()=>set({...s,side:!s.side})}>Watchlist</button>
    <button aria-pressed={s.bottom} onClick={()=>set({...s,bottom:!s.bottom})}>Positions</button>
    {activeData.bars && <span className={"cx-source "+(live&&!activeData.stale?"live":"sample")} title={activeData.note || undefined}>{!live?"Sample market data":activeData.stale?"Tastytrade · stale, refresh failed":"Live · Tastytrade"}</span>}</div></div>
    {activeData.bars && !live && <div className="cx-banner" role="status">Showing fixed sample candles, not market data{activeData.note?" · "+activeData.note:""}</div>}
    {favoriteMenu && <section className="cx-favorite-menu" aria-label="Choose favorite drawing tools"><strong>Favorite tools</strong>{drawingTools.map(([name])=><label key={name}><input type="checkbox" checked={favorites.includes(name)} onChange={()=>set({...s,favoriteTools:favorites.includes(name)?favorites.filter(t=>t!==name):[...favorites,name]})}/>{name}</label>)}<button onClick={()=>setFavoriteMenu(false)}>Done</button></section>}
    {favorites.length>0 && <div className="cx-favorites" role="toolbar" aria-label="Favorite drawing tools">{drawingTools.filter(([name])=>favorites.includes(name)).map(([name,icon])=><button key={name} title={name} aria-label={name} aria-pressed={tool===name} onClick={()=>{setTool(name);setLocked(false);setHidden(false);}}>{icon}</button>)}</div>}
    {editor && <Indicators preset={s.preset} savePreset={()=>set({...s,preset:active.studies})} pane={active} close={()=>setEditor(false)} update={studies=>patch(s.active,{studies})} apply={()=>set({...s,panes:s.panes.map((p,i)=>ids.includes(i)?{...p,studies:active.studies}:p)})}/>}
    <div className="cx-body"><div className="cx-rail" role="group" aria-label="Chart tools">{drawingTools.map(([name,icon])=><button key={name} title={name} aria-label={name} aria-pressed={tool===name} onClick={()=>{setTool(name);if(name!=="Pan" && name!=="Crosshair"){setLocked(false);setHidden(false);}}}>{icon}</button>)}
      <button aria-label="Favorite drawing tools" title="Favorite drawing tools" aria-expanded={favoriteMenu} onClick={()=>setFavoriteMenu(!favoriteMenu)}>☆</button>
      <hr/>{[["Snap to OHLC","⌁",magnet,setMagnet],["Keep drawing","⟳",repeat,setRepeat],["Lock drawings","⚿",locked,setLocked],["Hide drawings","◉",hidden,setHidden]].map(([name,icon,on,toggle])=><button key={name} aria-label={name} title={name} aria-pressed={on} onClick={()=>toggle(!on)}>{icon}</button>)}
      <button title="Delete selected drawing" aria-label="Delete selected drawing" disabled={locked||hidden||!symbolDrawings(active).some(d=>d.id===selected)} onClick={()=>{patch(s.active,{drawings:{...active.drawings,[active.symbol]:symbolDrawings(active).filter(d=>d.id!==selected)}});setSelected(null);}}>⌫</button></div>
      <div className="cx-center"><div key={s.count} className={"cx-grid cx-count-"+s.count}>{ids.map(i=>{
        const p=s.panes[i];
        return <ChartPane key={i} index={i} pane={p} selected={s.active===i} tool={tool} positions={positions}
          onActivate={()=>set(old=>old.active===i?old:{...old,active:i})} patch={changes=>patch(i,changes)} drawingProps={{
          magnet,locked,hidden,selected:s.active===i?selected:null,
          onSelect:id=>{set(old=>({...old,active:i}));setSelected(id);},
          save:items=>patch(i,{drawings:{...p.drawings,[p.symbol]:items}}),
          finish:()=>{if(!repeat)setTool("Crosshair");}
        }}/>;
      })}</div>
      {s.bottom && <BottomPanel positions={positions} positionsNote={positionsNote} quotes={quotes} account={account} accountError={accountError}/>}</div>
    {s.side && <SidePanel active={active} onPick={symbol=>patch(s.active,{symbol})} quotes={quotes} bars={activeData.bars} live={live}/>}</div>
    <footer className="cx-status">Eastern time · {live?"Candles from Tastytrade, refreshed automatically":"Sample market data"}</footer></section>;
}
