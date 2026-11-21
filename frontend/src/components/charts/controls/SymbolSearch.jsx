import { useEffect, useMemo, useState } from "react";
import { Dialog } from "./Overlay.jsx";
import { SYMBOL_PATTERN, describe, searchCatalog } from "./catalog.js";

// Search opened from the symbol button or by typing a letter on the chart.
export default function SymbolSearch({open, onClose, initialQuery="", recent=[], onPick, onAddWatch, target}) {
  const [query,setQuery]=useState(initialQuery);
  const [kind,setKind]=useState("All");
  const [active,setActive]=useState(0);
  useEffect(()=>{ if(open){ setQuery(initialQuery); setActive(0); } },[open, initialQuery]);
  const results = useMemo(()=>{
    if (kind==="Recent") return recent.map(s=>describe(s) || {symbol:s, name:"Recently viewed", type:"", exchange:""});
    const list = searchCatalog(query, kind);
    const typed = query.trim().toUpperCase();
    if (SYMBOL_PATTERN.test(typed) && !list.some(r=>r.symbol===typed)) list.push({symbol:typed, name:"Open "+typed+" (not in the list)", type:"", exchange:"", custom:true});
    return list;
  },[query, kind, recent]);
  const pick = (r, watch) => { if(!r) return; watch ? onAddWatch(r.symbol) : onPick(r.symbol); onClose(); };
  return <Dialog open={open} onClose={onClose} width={480} label="Symbol search">
    <div className="cx-search">
      <div className="cx-search-input"><span aria-hidden="true">⌕</span>
        <input data-autofocus value={query} placeholder="Search symbol or company" aria-label="Search symbol"
          onChange={e=>{setQuery(e.target.value);setActive(0);if(kind==="Recent")setKind("All");}}
          onKeyDown={e=>{
            if(e.key==="ArrowDown"){e.preventDefault();setActive(a=>Math.min(results.length-1,a+1));}
            if(e.key==="ArrowUp"){e.preventDefault();setActive(a=>Math.max(0,a-1));}
            if(e.key==="Enter"){e.preventDefault();pick(results[active], e.shiftKey);}
          }}/>
        <kbd>Esc</kbd></div>
      <div className="cx-chips" role="tablist" aria-label="Filter">{["All","Stocks","ETFs","Recent"].map(k=><button key={k} role="tab" aria-selected={kind===k} onClick={()=>{setKind(k);setActive(0);}}>{k}</button>)}</div>
      <div className="cx-results" role="listbox" aria-label="Results">
        {results.length ? results.map((r,i)=><button key={r.symbol} role="option" aria-selected={i===active} className={i===active?"active":""}
          onMouseEnter={()=>setActive(i)} onClick={e=>pick(r, e.shiftKey)}>
          <b>{r.symbol}</b><span>{r.name}</span><small>{[r.type,r.exchange].filter(Boolean).join(" · ")}</small></button>)
          : <p className="cx-muted cx-pad">{kind==="Recent" ? "No recent symbols yet." : "No match. Type a full ticker to open it."}</p>}
      </div>
      <div className="cx-search-foot"><span>Enter opens in {target}</span><span>Shift + Enter adds to watchlist</span></div>
    </div>
  </Dialog>;
}
