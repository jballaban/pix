// --- selection ---------------------------------------------------------------
// Moving the cursor **selects** what it lands on, the way a file manager
// does. The alternative was a cell that looked half-chosen: not ticked, the
// count saying none, and the actions quietly applying to it anyway. Pass
// `keep` to move without disturbing a selection.
function setCur(n,keep){
  // An action asked for from the viewer was about the photograph that was
  // on show. Another one on show is not what its open menu was for.
  if(vActing){vActing=null; closeMenu();}
  if(!cells.length){cur=-1;return;}
  n=Math.max(0,Math.min(cells.length-1,n));
  cells.forEach(c=>c.classList.remove('cur'));
  cur=n; cells[cur].classList.add('cur');
  // Only while the viewer is open, where the cursor is what you are looking at
  // and the grid behind should end up where you left off. With it closed
  // nothing points at the cursor, so scrolling to it is the page moving for
  // reasons of its own — which is what a bulk delete did, landing the cursor
  // on a survivor hundreds of rows away.
  if(viewer.classList.contains('on')) cells[cur].scrollIntoView({block:'nearest'});
  if(!keep){
    picked.forEach(c=>c.classList.remove('picked'));
    picked.clear();
    togglePick(cur,true);
    drawSel();
  }
  if(viewer.classList.contains('on')) load(cells[cur]);
}
// The next photograph you can actually see, or -1 where there is none that
// way. While a stack is open the rest of the grid is still in `cells` — hidden
// rather than removed, because it comes back when you are done — and paging
// walked straight through it into files that were not on screen.
function nextShown(from,dir){
  const start=from<0?(dir>0?-1:cells.length):from;
  for(let i=start+dir;i>=0&&i<cells.length;i+=dir){
    if(!cells[i].hidden) return i;
  }
  return -1;
}

function togglePick(n,on){
  const c=cells[n]; if(!c) return;
  if(on===undefined) on=!picked.has(c);
  on?picked.add(c):picked.delete(c);
  c.classList.toggle('picked',on);
}
function range(a,b){
  const [lo,hi]=a<b?[a,b]:[b,a];
  for(let n=lo;n<=hi;n++) togglePick(n,true);
}
function clearPicks(){picked.forEach(c=>c.classList.remove('picked'));
                      picked.clear(); drawSel();}
function show(act,on){
  const b=actions&&actions.querySelector('[data-act="'+act+'"]');
  if(b) b.hidden=!on;
}

function drawSel(){
  drawGroupPicks();
  if(!actions) return;
  // The landing page has a selection too, and it is not made of these.
  if(FOLDERS){drawFolderSel();return;}
  // A menu that acts on the selection has nothing left to act on once the
  // selection is empty — which is exactly where a write that pushes every
  // file out of the view leaves it, and it sat there open over a grid it
  // could no longer touch. Filter and grouping menus are about the view
  // rather than the selection, so they are left alone.
  if(!targets().length&&menuCtx&&(menuCtx.mode==='set'||menuCtx.mode==='date'))
    closeMenu();
  // Each set is on screen exactly when the selection holds files it applies
  // to. Not greyed: an action that is absent says *not for these files*,
  // where a greyed one says *not yet* — and with a mixed selection both are
  // present and neither is waiting for anything.
  const live=targetsOn('live'), dead=targetsOn('gone').length;
  // The tick and the count belong to a selection, and while a top is being
  // chosen there is not one.
  const tickEl=document.getElementById('selall');
  if(tickEl) tickEl.hidden=!!choosing;
  if(selcount) selcount.hidden=!!choosing;
  for(const g of actions.querySelectorAll('.grp')){
    const side=g.dataset.side;
    // While a top is being chosen there is one question on screen, so there is
    // one set of controls: the others would be offering to do something else
    // to a selection that is halfway through becoming a stack.
    g.hidden = choosing ? side!=='choose'
             : side==='choose' ? true
             : !(side==='gone'?dead:live.length);
  }
  // The stack actions ask a narrower question than *is anything selected*, so
  // they answer it themselves: two or more to make a stack, one that is in one
  // to promote, anything already stacked to take out.
  //
  // Never on video (spec/clips.md §4) — absent rather than refused, the rule
  // above: *not for these files*.
  const stackable=!hasVideo(live);
  show('stack', stackable && (live.length > 1 || live.some(tops)));
  // Only where the rest of the stack is on the page, because that is what
  // this writes to. It used to ask whether the file was *behind* something,
  // which was two answers wrong at once: it was offered in the folded grid,
  // where there is nothing on screen to write to and pressing it said so; and
  // it was withheld from the photograph a guess is drawn on, which is the one
  // the question *shall this be the top* most needs asking of — saying yes to
  // it is how a suggestion is accepted.
  show('top', stackable && live.length === 1 && familyOn(live[0]));
  // Not for a guess: there is nothing to take apart yet, and undoing
  // something nobody did would be a button whose whole answer is that it
  // should not have been there. Refusing is what a guess answers to.
  show('unstack', live.some(c => stacked(c) || +(c.dataset.behind||0) > 0));
  // Only where there is a guess to refuse. On a stack somebody made it would
  // be offering to un-decide a decision, which is what Unstack is for.
  show('nostack', live.some(inGuess));
  // One video at a time: the page it opens is one video's timeline.
  show('splice', live.length === 1 && !!live[0].dataset.splice);
  // Anything selected can be downloaded, deleted or not: what it is on the
  // disk does not depend on what has been decided about it.
  show('download', live.length + dead > 0);
  // The tick wears the three states of what it would do: nothing selected and
  // it selects everything, anything selected and it clears.
  actions.dataset.state = !picked.size ? 'none'
    : picked.size===cells.length ? 'all' : 'some';
  if(selcount) selcount.textContent = `${picked.size} selected`;
  fitActions();
}
// The index is looked up **at click time**, never captured when the handler is
// bound. Cells leave the grid when an edit pushes them out of the filters, and
// `cells` is rebuilt around the gap — so a handler holding the position its
// cell had at load would open whatever has since slid into it. Delete two
// files near the top and every thumbnail below them opened the picture two
// along. `picked` is keyed by element for exactly this reason; the handlers
// were the half that still counted.
//: How long a press has to be to mean *and everything back to the last one*.
//: Long enough not to catch a slow tap, short enough that nobody lets go
//: first — which is the whole range either way.
const HOLD_MS=450;

function wire(c){
  const pick=c.querySelector('.pick');
  // A press that has already done its work. The finger coming off it is still
  // a tap as far as the browser is concerned, and that tap would untick the
  // far end of the range the press just made.
  let held=null,done=false;
  pick.addEventListener('click',e=>{
    e.stopPropagation();
    if(done){done=false;return;}
    const n=cells.indexOf(c);
    if(n<0) return;
    // The circle is the deliberate gesture: it adds and removes without
    // throwing away what is already ticked.
    if(e.shiftKey&&anchor>=0) range(anchor,n); else {togglePick(n); anchor=n;}
    setCur(n,true); drawSel();
  });
  // Holding a circle is what shift-clicking one is on a keyboard: everything
  // from the last circle you touched to this one. A phone has no shift key,
  // and 61,846 files one circle at a time is not a job anybody finishes —
  // which made a range the difference between the library being cullable on a
  // phone and not.
  if(typeof pick.addEventListener==='function'){
    const stop=()=>{ if(held){clearTimeout(held);held=null;} };
    pick.addEventListener('touchstart',()=>{
      done=false;
      stop();
      held=setTimeout(()=>{
        held=null;
        const n=cells.indexOf(c);
        if(n<0) return;
        // With nothing touched yet there is no range to make, so it is an
        // ordinary tick — and it becomes the end to measure the next one from.
        if(anchor>=0&&anchor!==n) range(anchor,n);
        else {togglePick(n,true); anchor=n;}
        setCur(n,true); drawSel();
        done=true;
      },HOLD_MS);
    },{passive:true});
    // Moving is scrolling, and letting go early is an ordinary tap. Either
    // way this was not a hold.
    pick.addEventListener('touchmove',stop,{passive:true});
    pick.addEventListener('touchend',stop,{passive:true});
    pick.addEventListener('touchcancel',stop,{passive:true});
  }
  c.addEventListener('click',e=>{
    // A link inside the cell is somewhere to go, not a photograph to open.
    // The stack badge did both: the viewer opened over the grid and then the
    // page left for the stack underneath it, so coming back restored a page
    // with a photograph on it that nobody had asked to see.
    if(e.target.closest('a')) return;
    const n=cells.indexOf(c);
    if(n<0) return;
    if(e.shiftKey&&anchor>=0){range(anchor,n);setCur(n,true);drawSel();return;}
    if(e.ctrlKey||e.metaKey){togglePick(n);anchor=n;setCur(n,true);drawSel();
                             return;}
    // A plain click is *show me this one*, and it must not cost a selection.
    // The viewer moves the cursor itself, since whether that also selects
    // depends on what was selected before the click.
    anchor=n; openViewer(n);
  });
}
cells.forEach(wire);
// One control for one question. Empty, it selects everything; otherwise it
// clears — which is what both *Select all* and *Deselect* were for, and it
// sits beside the count it is about rather than up in the filter bar.
const selall=document.getElementById('selall');
if(selall) selall.onclick=e=>{
  e.stopPropagation();
  if(FOLDERS){
    if(pickedFolders.size) pickedFolders.clear();
    else tiles.forEach(t=>pickedFolders.add(t));
    expanded=null; expandedBy.clear();
    drawFolderSel();
    return;
  }
  if(picked.size){clearPicks();return;}
  // Every file the view holds, not the part of it scrolled into so far.
  const go=()=>{ cells.forEach((_,n)=>togglePick(n,true)); drawSel(); };
  if(moreToCome()){ say('loading every file…'); loadAll().then(()=>{say('');go();}); }
  else go();
};

// **One line of actions, and a `+` for what does not fit.** Which actions are
// on the bar is the selection's business (`drawSel`): only the ones that
// apply to these files. This is only about room — once that is decided, the
// ones that do not fit on the line fold away from the end, behind a `+`, the
// same gesture as the filters'. The bar sits over the grid, so a second line
// would push every thumbnail down the moment you tick one.
//
// No chevron, unlike the filters: an action has no value to read, so the
// list behind the `+` is everything there is to see.
const spillBtn=actions&&(()=>{
  const b=document.createElement('button');
  b.className='addchip spillbtn';
  b.textContent='+';
  b.title='More actions';
  b.setAttribute('aria-label',b.title);
  b.hidden=true;
  b.onclick=e=>{e.stopPropagation(); spillMenu();};
  actions.appendChild(b);
  return b;
})();
function onBar(b){ return !b.hidden&&!b.classList.contains('spill')
                          &&!(b.closest('.grp')||{}).hidden; }
// Whether a row is one line and runs no further than its own width — the
// action groups do not wrap inside themselves, so too many of them overflow
// the row rather than starting a line of their own.
function rowFits(el){
  return oneLine(el)&&(el.scrollWidth||0)<=(el.clientWidth||0)+1;
}
// A rule between two clusters, with nothing on the bar after it, is a rule
// beside nothing.
function tidySeps(){
  actions.querySelectorAll('.sep').forEach(sep=>{
    let n=sep.nextElementSibling, after=false;
    for(;n;n=n.nextElementSibling) if(n.dataset&&n.dataset.act&&onBar(n)) after=true;
    sep.classList.toggle('spill',!after);
  });
}
function fitActions(){
  if(!actions||!spillBtn) return;
  actions.querySelectorAll('.spill').forEach(b=>b.classList.remove('spill'));
  spillBtn.hidden=true;
  tidySeps();
  if(rowFits(actions)) return;
  const shown=[...actions.querySelectorAll('[data-act]')].filter(onBar);
  spillBtn.hidden=!shown.length;
  for(let i=shown.length-1;i>=0;i--){
    shown[i].classList.add('spill');
    tidySeps();
    if(rowFits(actions)) return;
  }
}
// The folded-away actions, drawing beside name — the same list the bar would
// have shown, in the same order.
function spillMenu(){
  const key='spill';
  if(menuCtx&&menuCtx.key===key&&!menu.hidden){closeMenu();return;}
  menu.classList.remove('over');
  menu.innerHTML='<div id="menulist"></div>';
  const list=menu.querySelector('#menulist');
  for(const b of actions.querySelectorAll('[data-act].spill')){
    if(b.hidden||(b.closest('.grp')||{}).hidden) continue;
    const svg=b.querySelector('svg');
    const word=b.querySelector('.word');
    const d=document.createElement('div');
    d.className='opt'+(b.classList.contains('danger')?' danger':'');
    d.innerHTML=`<i class="mark">${svg?svg.outerHTML:''}</i>`
               +`<span>${esc(word?word.textContent.replace('…','').trim():b.title)}</span>`;
    d.onclick=e=>{e.stopPropagation(); closeMenu(); barAct(b.dataset.act,spillBtn);};
    list.appendChild(d);
  }
  placeMenu(spillBtn);
  menuCtx={key};
}
if(actions&&typeof ResizeObserver!=='undefined'){
  let wide=-1;
  new ResizeObserver(es=>{
    const w=Math.round(es[0].contentRect.width);
    if(w!==wide){ wide=w; requestAnimationFrame(fitActions); }
  }).observe(actions);
}
// A measurement taken before the typeface arrived is a measurement of a
// different bar: the words come in wider or narrower than the fallback's.
if(document.fonts&&document.fonts.ready)
  document.fonts.ready.then(()=>{ fitChips(); fitActions(); });
