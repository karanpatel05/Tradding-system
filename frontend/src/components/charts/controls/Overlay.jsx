import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// Menus and dialogs render into document.body (inside .cx-layer, which carries the
// chart's colour tokens) so the chart's overflow and container never clip them.

function useDismiss(open, onClose, panel, anchor) {
  useEffect(() => {
    if (!open) return;
    const returnTo = anchor?.current || document.activeElement;
    const key = e => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    const down = e => {
      if (panel.current?.contains(e.target) || anchor?.current?.contains(e.target)) return;
      onClose();
    };
    document.addEventListener("keydown", key, true);
    document.addEventListener("pointerdown", down, true);
    return () => {
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("pointerdown", down, true);
      if (returnTo && typeof returnTo.focus === "function" && document.contains(returnTo)) returnTo.focus({preventScroll:true});
    };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
}

// Anchored menu: below `anchorRef` (or at a point `at`), kept inside the viewport.
export function Popover({open, onClose, anchorRef, at, align="start", className="", label, children}) {
  const panel = useRef(null);
  const [pos, setPos] = useState({left:-9999, top:-9999});
  useDismiss(open, onClose, panel, anchorRef);
  useLayoutEffect(() => {
    if (!open || !panel.current) return;
    const p = panel.current.getBoundingClientRect();
    let left, top;
    if (at) { left = at.x; top = at.y; }
    else {
      const a = anchorRef.current.getBoundingClientRect();
      left = align === "end" ? a.right - p.width : a.left;
      top = a.bottom + 4;
      if (top + p.height > innerHeight - 8 && a.top - p.height - 4 > 8) top = a.top - p.height - 4;
    }
    left = Math.max(8, Math.min(left, innerWidth - p.width - 8));
    top = Math.max(8, Math.min(top, innerHeight - p.height - 8));
    setPos({left, top});
  }, [open, at?.x, at?.y]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (open) panel.current?.querySelector("button:not(:disabled),input,select,[tabindex='0']")?.focus({preventScroll:true});
  }, [open]);
  if (!open) return null;
  return createPortal(<div className="cx-layer">
    <div ref={panel} role="menu" aria-label={label} className={"cx-pop "+className} style={pos}
      onKeyDown={e => {
        if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
        const items = [...panel.current.querySelectorAll("[role=menuitem]:not(:disabled),[role=menuitemcheckbox]:not(:disabled)")];
        const i = items.indexOf(document.activeElement);
        if (!items.length) return;
        e.preventDefault();
        items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
      }}>{children}</div>
  </div>, document.body);
}

// Modal dialog centred in the viewport.
export function Dialog({open, onClose, title, width=440, footer, children, label}) {
  const panel = useRef(null);
  useDismiss(open, onClose, panel);
  useEffect(() => {
    if (open) (panel.current?.querySelector("[data-autofocus]") || panel.current?.querySelector("input,select,button"))?.focus({preventScroll:true});
  }, [open]);
  if (!open) return null;
  return createPortal(<div className="cx-layer">
    <div className="cx-backdrop" />
    <div ref={panel} role="dialog" aria-modal="true" aria-label={label || title} className="cx-dialog" style={{width:`min(${width}px, calc(100vw - 24px))`}}>
      {title && <div className="cx-dialog-head"><strong>{title}</strong><button className="cx-icon-btn" aria-label="Close" onClick={onClose}>✕</button></div>}
      <div className="cx-dialog-body">{children}</div>
      {footer && <div className="cx-dialog-foot">{footer}</div>}
    </div>
  </div>, document.body);
}

export function Switch({checked, onChange, label}) {
  return <button type="button" role="switch" aria-checked={!!checked} aria-label={label} className="cx-switch" onClick={() => onChange(!checked)}><i/></button>;
}

export function Segmented({value, options, onChange, label}) {
  return <div className="cx-seg" role="group" aria-label={label}>{options.map(o => {
    const [v, text] = Array.isArray(o) ? o : [o, o];
    return <button key={v} type="button" aria-pressed={value===v} onClick={() => onChange(v)}>{text}</button>;
  })}</div>;
}

export const PALETTE = ["#f5b84a", "#7aa2ff", "#2dd4a7", "#b58cff", "#f0657a", "#9aa7b6", "#5ec8f5", "#e6edf3"];
export function Swatches({value, onChange, label}) {
  return <div className="cx-swatches" role="group" aria-label={label}>
    {PALETTE.map(c => <button key={c} type="button" aria-label={c} aria-pressed={value?.toLowerCase()===c} style={{background:c}} onClick={() => onChange(c)}/>)}
    <label className="cx-swatch-custom" title="Custom colour"><input type="color" aria-label={label+" custom"} value={value || "#ffffff"} onChange={e => onChange(e.target.value)}/></label>
  </div>;
}
