// --- the rest of the grid, as it is scrolled towards -------------------------
// The grid comes with its first page and the rest arrive as you near the
// bottom, so a view of the whole library answers in a quarter of a second and
// a file nobody scrolls to is never queried, drawn or sent.
//
// `served` is how many rows the server has handed over that the grid still
// holds. A write that takes files out of the view takes them out of the
// server's order too, so the next page starts that many rows earlier — the
// count follows `drop`, or the next page would skip exactly those.
const moreEl=document.getElementById('more');
let served=grid?+(grid.dataset.served||0):0;
let total=grid?+(grid.dataset.total||0):0;
let paging=null;
function moreToCome(){ return !!moreEl&&served<total; }
function keyOfSection(sec){ return sec&&sec.dataset?sec.dataset.key:undefined; }

async function loadMore(){
  if(paging) return paging;
  if(!moreToCome()) return;
  paging=(async()=>{
    const p=new URLSearchParams(location.search);
    p.set('offset',String(served));
    let out=null;
    try{ out=await (await fetch('/api/page?'+p)).json(); }
    catch(e){ say('could not load more: '+e.message,true); return; }
    if(!out) return;
    total=+out.total;
    served=+out.served;
    if(out.html) appendPage(out.html);
    if(moreEl) moreEl.hidden=!moreToCome();
  })();
  try{ await paging; }finally{ paging=null; }
  // Still near the bottom after it arrived — a tall screen, small
  // thumbnails, or a fast scroll — so another. The observer only says when
  // the marker *comes* into reach, and it never left.
  if(moreToCome()&&nearTheEnd()) setTimeout(loadMore,0);
}
function nearTheEnd(){
  if(!moreEl||!moreEl.getBoundingClientRect) return false;
  const r=moreEl.getBoundingClientRect();
  return r.top<(window.innerHeight||0)*2.5;
}

// Everything still to come. For the gestures that mean *all of them* —
// select all, a group's own circle, the end of the viewer — which cannot be
// answered from a part.
async function loadAll(){
  while(moreToCome()){
    const was=served;
    await loadMore();
    if(served<=was) break;
  }
}
// Enough to hold the whole of one section: until the section after it has
// started, or there is nothing more.
async function loadSection(h){
  const sec=h&&h.parentNode;
  while(moreToCome()&&sec&&!sec.nextElementSibling){
    const was=served;
    await loadMore();
    if(served<=was) break;
  }
}

function appendPage(html){
  const holder=document.createElement('div');
  holder.innerHTML=html;
  const incoming=[...holder.children];
  const sections=[...grid.querySelectorAll('.sect')];
  const last=sections[sections.length-1];
  const fresh=[];
  incoming.forEach((sec,i)=>{
    // The page may carry on the section the grid ends with: the same day
    // keeps the one heading, and its cells join the ones already under it.
    if(i===0&&last&&keyOfSection(sec)===keyOfSection(last)){
      const into=last.querySelector('.cells');
      const kids=[...(sec.querySelector('.cells')||sec).children];
      const n=last.querySelector('.group .dim');
      kids.forEach(c=>{ into.appendChild(c); fresh.push(c); });
      // Arrived, and already counted by the heading.
      if(n&&n.dataset.seen!==undefined)
        n.dataset.seen=String(+n.dataset.seen+kids.length);
      return;
    }
    grid.appendChild(sec);
    const h=sec.querySelector('.group');
    if(h) wireHeading(h);
    sec.querySelectorAll('.cell').forEach(c=>fresh.push(c));
  });
  // Never the same file twice: a page asked for while a write was moving
  // the order can overlap the one before.
  const have=new Set(cells.map(c=>c.dataset.folder+'/'+c.dataset.name));
  const added=fresh.filter(c=>{
    const k=c.dataset.folder+'/'+c.dataset.name;
    if(have.has(k)){ c.remove(); return false; }
    have.add(k); return true;
  });
  cells.push(...added);
  const px=cellPixels();
  added.forEach(c=>{ wire(c); useSource(c,px); placeInfo(c); });
  clampInfo(added);
  resection(); drawSel();
}

if(moreEl&&typeof IntersectionObserver!=='undefined'){
  // Well before the bottom, so the next page is there by the time it is
  // looked at: a screen and a half of warning at any thumbnail size.
  new IntersectionObserver(es=>{
    if(es.some(e=>e.isIntersecting)) loadMore();
  },{rootMargin:'0px 0px 150% 0px'}).observe(moreEl);
}
// And on scroll, as well: an observer only speaks when the marker crosses
// into reach, and a browser that skips one — a tab restored from the cache,
// a jump with the End key — would leave the grid stopped at a page.
if(moreEl){
  let scrollDue=false;
  window.addEventListener('scroll',()=>{
    if(scrollDue||!moreToCome()) return;
    scrollDue=true;
    setTimeout(()=>{ scrollDue=false; if(nearTheEnd()) loadMore(); },120);
  },{passive:true});
}

drawChips(); drawSel();

// **An opened guess asks its question on the photographs.** Every file in
// here is a candidate for the only thing this page is for — *are these one
// photograph, and which of them shows* — so every one of them offers the
// answer, one press, where you are already looking.
//
// It was select-then-press-the-bar, which is two gestures for a question with
// a picture of its answer under the pointer, and the control to do it in one
// already existed: it was simply summoned by a mode rather than standing. The
// reason it is summoned elsewhere is good — a button on every thumbnail in a
// two-thousand-cell grid is a page about its own controls — and it does not
// hold here, where there are three or four photographs and one question.
//
// **A guess only.** An opened stack somebody already made is a place you go
// to look, or to tag, or to take one out; re-picking its top is one of the
// things you might do there rather than the reason you came, and a standing
// button on every take would be answering a question nobody asked. The bar
// still has *Make top* for that.
if(STACK){
  for(const c of cells){
    const here=stackKey(c)===keyOf(c);
    // The one that already shows has nothing to say on a stack somebody made
    // — it is the answer already given, and a button whose whole reply is
    // that it should not have been pressed is worse than none.
    //
    // On a guess it has everything to say, and says it in words that are not
    // *Show this one*: under the photograph that is already the one shown
    // that reads as a button that would do nothing, which is exactly the
    // press somebody needs to make and exactly the one they will not. Nor
    // *Stack these*, which was the first try and reads as making a second
    // stack inside the one you are standing in — the thing it does is agree,
    // and what it agrees to is the top.
    if(here&&!guessed(c)) continue;
    offerChoice(c, here?'Confirm top':'Show this one', true);
  }
}

// Landing on one photograph, because something sent you here standing on it.
// Agreeing with a suggestion is the one gesture that leaves the page it was
// made on, and it names the file it was made about so that coming back is
// coming back rather than starting again.
//
// Silent about anything it cannot find. A fragment outlives the view it was
// written for — change a filter, reload a bookmark, and the photograph it
// names is somewhere else or nowhere — and a page that complained about that
// would be complaining about an address that is merely old.
function land(){
  let want='';
  try{ want=decodeURIComponent(String(location.hash||'').slice(1)); }
  catch(e){ return; }
  if(!want) return;
  // `open:` asks for its preview as well — which is where a clip's *cut
  // from* leads, since the point of following it is to look at the source.
  const open=want.startsWith('open:');
  if(open) want=want.slice(5);
  const n=cells.findIndex(x=>keyOf(x)===want);
  const c=n<0?null:cells[n];
  // `center`, because `nearest` on a cell that is already technically in view
  // does nothing — and the browser's idea of in view includes the strip under
  // the bar, where the sticky heading is standing.
  if(c&&c.scrollIntoView) c.scrollIntoView({block:'center'});
  if(c&&open&&viewer) openViewer(n);
}
land();
// A link that changes nothing but the part after `#` does not load a page,
// so arriving that way has to be heard as well.
window.addEventListener('hashchange',land);
