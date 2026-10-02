// --- swiping between photographs ---------------------------------------------
// Left and right are the arrow keys' job, and a phone has no arrow keys — so
// without this the only way from one photograph to the next is to close the
// viewer, find the thumbnail after it, and open that.
if(stage&&typeof stage.addEventListener==='function'){
  let sx=0,sy=0,swiping=false;
  stage.addEventListener('touchstart',e=>{
    const t=e.touches&&e.touches.length===1&&e.touches[0];
    // A drag beginning at the very edge of the screen belongs to the system —
    // that is how you go back — and an app that takes it over is one you
    // cannot get out of.
    swiping=!!t&&t.clientX>28&&t.clientX<(window.innerWidth||0)-28;
    if(t){sx=t.clientX;sy=t.clientY;}
  },{passive:true});
  stage.addEventListener('touchend',e=>{
    if(!swiping) return;
    swiping=false;
    const t=e.changedTouches&&e.changedTouches[0];
    if(!t) return;
    const dx=t.clientX-sx,dy=t.clientY-sy;
    // Far enough across to have been meant, and more across than down: without
    // the second test every slightly crooked scroll of the details turns a
    // page. Down is deliberately not a gesture — it is the system's in an
    // installed app, and there is a close button.
    if(Math.abs(dx)<48||Math.abs(dx)<Math.abs(dy)*1.6) return;
    const to=nextShown(cur,dx<0?1:-1);
    if(to<0) return;
    // Paging is looking, not choosing — the same rule the arrow keys follow.
    setCur(to,true);
    drawSel();
  },{passive:true});
}

