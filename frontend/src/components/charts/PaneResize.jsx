import { useRef } from "react";

// Read the resolved tracks so the same handles work after responsive reflow.
export default function PaneResize({direction}) {
  const drag=useRef(null);
  const horizontal=direction==="width";
  const property=horizontal?"gridTemplateColumns":"gridTemplateRows";
  return <div className={"cx-resize cx-resize-"+direction} role="separator"
    aria-label={"Resize pane "+direction} aria-orientation={horizontal?"vertical":"horizontal"}
    title={"Drag to resize pane "+direction}
    onPointerDown={event=>{
      const pane=event.currentTarget.parentElement, grid=pane.parentElement;
      const style=getComputedStyle(grid);
      const tracks=style[property].split(" ").map(parseFloat);
      const columns=style.gridTemplateColumns.split(" ").length;
      const index=Array.from(grid.children).indexOf(pane);
      const track=horizontal?index%columns:Math.floor(index/columns);
      if(horizontal && track===tracks.length-1) return;
      drag.current={grid,tracks,track,start:horizontal?event.clientX:event.clientY};
      event.currentTarget.setPointerCapture(event.pointerId);
    }}
    onPointerMove={event=>{
      if(!drag.current) return;
      const {grid,tracks,track,start}=drag.current;
      const next=[...tracks];
      const min=horizontal?120:200;
      const delta=(horizontal?event.clientX:event.clientY)-start;
      const available=tracks[track+1]===undefined?Infinity:tracks[track+1]-min;
      const move=Math.max(min-tracks[track],Math.min(available,delta));
      next[track]+=move;
      if(track+1<next.length) next[track+1]-=move;
      grid.style[property]=next.map(n=>horizontal?n+"fr":n+"px").join(" ");
    }}
    onLostPointerCapture={()=>{drag.current=null;}}/>;
}
