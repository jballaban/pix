
const grid=document.getElementById('grid');
const menu=document.getElementById('menu');
const chips=document.getElementById('chips');
const actions=document.getElementById('actions');
// Absent entirely for a non-admin: the page must not reach for controls
// the server would refuse anyway.
const selcount=document.getElementById('selcount');
const countEl=document.getElementById('count');
const binEl=document.getElementById('bincount');
// Every copy of the size control, not the first: the bar carries one where
// there is room and the account menu carries one where there is not, both are
// always rendered, and turning a phone over changes which is on screen without
// reloading the page. A `getElementById` here wired one of the two and left
// the other inert — a control that silently does nothing at one width.
const sizeOpts=Array.prototype.slice.call(
  document.querySelectorAll('.sizeopt'));
const note=document.getElementById('note');
const viewer=document.getElementById('viewer');
const vimg=document.getElementById('vimg'), vvid=document.getElementById('vvid');
const vmeta=document.getElementById('vmeta');
const stage=document.querySelector('.stage');
// Bounded so each request stays short: the server accepts 500, but a chunk that
// takes ten seconds gives no progress reading and holds the single worker.
const CHUNK=100;

// --- what kind of thing is this being used on --------------------------------
// Asked once, of the browser rather than of its name. A user-agent string says
// what somebody wants you to believe; `pointer: coarse` says a finger is what
// will be aiming at these twenty pixels, which is the only thing any of this
// turns on.
//
// `typeof` because the page script is run under a DOM stub in the tests, where
// `matchMedia` does not exist — and a bare reference would throw on load and
// take every handler on the page with it, which is the exact failure the stub
// was written to catch.
function media(q){
  try{ return typeof matchMedia==='function'&&matchMedia(q).matches; }
  catch(e){ return false; }
}
const COARSE=media('(pointer: coarse)');
// Whether the file can be handed to the system rather than downloaded. On iOS
// this is the only route into Photos at all: a download — in Safari, and
// assuming it happens at all in an installed app — lands in Files and nowhere
// else. Feature-tested, so a desktop browser without it simply never takes
// this path.
const CAN_SHARE=(typeof navigator!=='undefined'
                 &&typeof navigator.canShare==='function'
                 &&typeof navigator.share==='function');

let cells=[...document.querySelectorAll('.cell')];
let cur=-1, anchor=-1, busy=false;
// Keyed by element rather than index: cells leave the grid when a change
// pushes them out of the filters, and indices would then quietly re-point
// a selection at whatever slid into the gap.
const picked=new Set();

// --- thumbnail size ----------------------------------------------------------
// A preference about looking, not about which photographs — so it lives in the
// browser rather than the URL, beside the details rail. A view is a link; how
// big you like the thumbnails is not part of where you are.
// Small, medium, large. The letters and the words for them are in the markup
// the server sends — the script only has to know which names are real, so a
// stale value in `localStorage` cannot put the grid into a size that has no
// rule behind it.
const SIZES=['small','medium','large'];
let thumbSize='small';
try{
  const saved=localStorage.getItem('pix2.thumb');
  // `big` and `huge` are what these were called for an afternoon.
  const known={big:'medium',huge:'large'}[saved]||saved;
  if(SIZES.includes(known)) thumbSize=known;
}catch(e){}

// The biggest size is bigger than the thumbnail tier has pixels for, so it
// reads from `large` — sized for exactly this and nothing else. The media
// routes take the same path after the tier name, so this swaps one segment
// rather than building an address a second time.
// How wide a cell is drawn, in the pixels the screen actually has. Measured
// once rather than per cell: every cell in the grid is the same width, and
// asking two thousand of them costs a layout each.
// Capped at two on a phone, and it is not a compromise about sharpness.
// A modern phone reports three, so a 174px cell asked for 520 real pixels,
// which no thumbnail has — and every cell in the grid came from `large` at a
// thousand pixels, or from `preview` at sixteen hundred at the other two
// sizes. The biggest derivative in the library, ten times the bytes it can
// show, over wifi or a VPN, on the most memory-constrained thing in the house.
// Two is past the point anyone can see on a cell this size and it is what the
// thumbnail tier was built to cover.
function cellPixels(){
  const c=cells.find(x=>!x.hidden)||cells[0];
  const w=c?c.getBoundingClientRect().width:0;
  const dpr=window.devicePixelRatio||1;
  return Math.round((w||150)*(media('(max-width: 720px)')?Math.min(dpr,2):dpr));
}

// The smallest tier that can fill it. Not a fixed tier per size: the same
// grid on a retina screen needs twice the pixels for the same inch of glass,
// and reading `large` there was asking a 563-pixel square to cover 830 — soft
// in exactly the way a photograph never is in the viewer.
// With a fifth to spare, not to the pixel. A source that only just covers the
// cell is being shown at very nearly 1:1, which on a video frame — already
// soft, already compressed once — looks nothing like the same cell filled
// from a photograph with half again as many pixels to give away. The margin
// is what makes the two look alike.
const SPARE=1.2;

function sourceFor(c,px){
  const ar=+(c.dataset.ar||1)||1;
  for(const [dir,cap] of TIERS) if(cap*ar>=px*SPARE) return dir;
  return TIERS[TIERS.length-1][0];
}

function useSource(c,px){
  const img=c.querySelector('img');
  if(!img) return;
  const have=img.getAttribute('src')||'';
  const at=have.indexOf('/',1);
  if(at<0) return;
  const want=sourceFor(c,px===undefined?cellPixels():px);
  if(!have.startsWith(want)) img.setAttribute('src',want+have.slice(at+1));
}

function drawSize(){
  if(grid) grid.dataset.size=thumbSize;
  // Which one is on, on every copy. `aria-pressed` rather than a class: these
  // are three buttons of which exactly one is the state, which is what that
  // attribute means — and it is what the styling reads, so there is one fact
  // here rather than two that can disagree.
  sizeOpts.forEach(b=>b.setAttribute(
    'aria-pressed',b.dataset.size===thumbSize?'true':'false'));
  // After the grid has been told its new size, or every cell is measured at
  // the width it is about to stop being.
  const px=cellPixels();
  cells.forEach(c=>useSource(c,px));
  if(typeof clampInfo==='function') clampInfo(cells);
}

function chooseSize(next){
  if(!SIZES.includes(next)||next===thumbSize) return;
  // Anchored the way a write is: the row heights are about to change under
  // whatever you were looking at, and the point of a bigger thumbnail is to
  // look harder at the one you had already found.
  const at=cells.find(c=>c.getBoundingClientRect().bottom>0);
  const was=at?at.getBoundingClientRect().top:null;
  thumbSize=next;
  try{localStorage.setItem('pix2.thumb',thumbSize);}catch(e){}
  drawSize();
  if(at&&was!==null){
    const now=at.getBoundingClientRect().top;
    if(now!==was) window.scrollBy(0,now-was);
  }
}

sizeOpts.forEach(b=>{
  // Not letting the click reach the document: the menu copy sits inside the
  // account menu, and the shell dismisses that on any click outside it. This
  // one is inside, but the grid's own handlers are on the way past.
  b.onclick=e=>{e.stopPropagation();chooseSize(b.dataset.size);};
});
drawSize();

// --- what a thumbnail says (Display) -----------------------------------------
// Which facts a thumbnail shows, and in which lane. A preference about
// looking, like the size, so it lives in this browser: a phone and a desk want
// different amounts of writing over a photograph. The server draws the default
// arrangement (`_INFO`) and this moves each fact to where it was asked to be.
//
// What is hidden is hidden by the stylesheet, from an attribute on <html> set
// before the page paints (see `_INFO_BOOT`), so a fact turned off never
// flashes on. Only *where* needs a script, because a lane is a box and a fact
// has to be inside one.
const INFO_DEFAULT={people:'bot',access:'bot',tags:'top',subevent:'bot',clip:'on'};
// Each fact and the elements it is drawn as, in the order a lane lists them.
const INFO_PARTS=[['people',['.folk']],['access',['.who','.unshared']],
                  ['tags',['.tags']],['subevent',['.part']]];
const INFO_ALLOWED={people:['off','top','bot'],access:['off','top','bot'],
                    tags:['off','top','bot'],subevent:['off','top','bot'],
                    clip:['off','on']};
const info={...INFO_DEFAULT};
try{
  const saved=JSON.parse(localStorage.getItem('pix2.info')||'{}');
  for(const k of Object.keys(INFO_DEFAULT))
    if(saved&&INFO_ALLOWED[k].includes(saved[k])) info[k]=saved[k];
}catch(e){}
// The facts' switches; the stacks row beside them has a wiring of its own.
const showOpts=Array.prototype.slice.call(
  document.querySelectorAll('.showopt')).filter(b=>b.dataset.info);

// One edge of a cell, made if the cell came without one — a cell built by
// hand, or by markup from before there were lanes.
function laneOf(c,where){
  let ov=null;
  for(const k of c.children) if(k.classList.contains('ov')&&
                                k.classList.contains(where)) ov=k;
  if(!ov){
    ov=document.createElement('div'); ov.className='ov '+where;
    const lane=document.createElement('span'); lane.className='lane';
    const fix=document.createElement('span'); fix.className='fix';
    ov.appendChild(lane); ov.appendChild(fix); c.appendChild(ov);
  }
  let lane=null, fix=null;
  for(const k of ov.children){
    if(k.classList.contains('lane')) lane=k;
    if(k.classList.contains('fix')) fix=k;
  }
  return {lane, fix};
}

// Everything in its place. Facts go to their lane in menu order; what is
// always there goes to the right-hand end it owns — the clip mark, then the
// stack badge, then the circle at the top; the length at the bottom.
function placeInfo(c){
  const top=laneOf(c,'top'), bot=laneOf(c,'bot');
  for(const [key,sels] of INFO_PARTS){
    const into=info[key]==='top'?top.lane:bot.lane;
    for(const sel of sels){
      // Appended even where it already is, which is what keeps a lane in
      // the menu's order when two facts are moved into it one at a time.
      const el=c.querySelector(sel);
      if(el) into.appendChild(el);
    }
  }
  for(const sel of ['.cuts','.clip-mark','.stack','.top-mark','.pick']){
    const el=c.querySelector(sel);
    if(el) top.fix.appendChild(el);
  }
  const len=c.querySelector('.badge');
  if(len) bot.fix.appendChild(len);
}

// Past a few lines a lane stops and says how many more. Measured, because
// how many chips make a line depends on how wide the thumbnail is and how
// long the words are — so this runs after anything that changes either.
// Every reset before any read, and every read before any write, so a grid of
// two thousand costs two layouts rather than two thousand.
function laneLines(){ return thumbSize==='small'?2:3; }
function clampInfo(list){
  const lanes=[];
  for(const c of list){
    if(c.hidden) continue;
    for(const k of c.children){
      if(!k.classList.contains('ov')) continue;
      for(const l of k.children) if(l.classList.contains('lane')) lanes.push(l);
    }
  }
  for(const l of lanes){
    for(const m of l.querySelectorAll('.more')) m.remove();
    for(const x of l.querySelectorAll('.cut')) x.classList.remove('cut');
  }
  const max=laneLines();
  const plan=[];
  for(const l of lanes){
    const bits=[];
    for(const x of l.querySelectorAll('i')) bits.push(x);
    for(const sel of ['.part','.unshared']){
      const x=l.querySelector(sel); if(x) bits.push(x);
    }
    if(bits.length<2) continue;
    const at=bits.map(x=>[x,x.getBoundingClientRect()]).filter(
      ([,r])=>r.width||r.height);
    const tops=[...new Set(at.map(([,r])=>Math.round(r.top)))];
    if(tops.length<=max) continue;
    // The lane's own edge first: the top lane reads downward, the bottom
    // one upward, so its first line is its lowest.
    const up=l.parentNode&&l.parentNode.classList.contains('bot');
    tops.sort((a,b)=>up?b-a:a-b);
    const keep=new Set(tops.slice(0,max));
    plan.push([l,at.filter(([,r])=>!keep.has(Math.round(r.top))).map(([x])=>x),
               at.filter(([,r])=>Math.round(r.top)===tops[max-1]).map(([x])=>x)]);
  }
  for(const [l,cut,last] of plan){
    // Room for the count on the last kept line: one more goes with the rest.
    const drop=last.length>1?[last[last.length-1]]:[];
    const gone=cut.concat(drop);
    gone.forEach(x=>x.classList.add('cut'));
    const more=document.createElement('i');
    more.className='more';
    more.textContent='+'+gone.length;
    more.setAttribute('title',gone.map(x=>x.textContent||x.getAttribute('title')
                                      ||'').filter(Boolean).join(', '));
    l.appendChild(more);
  }
}

function drawInfo(){
  const root=document.documentElement;
  for(const [k,v] of Object.entries(info)){
    if(root&&root.setAttribute) root.setAttribute('data-info-'+k,v);
  }
  showOpts.forEach(b=>b.setAttribute('aria-pressed',
    info[b.dataset.info]===b.dataset.at?'true':'false'));
  cells.forEach(placeInfo);
  clampInfo(cells);
}

function chooseInfo(key,at){
  if(!INFO_ALLOWED[key]||!INFO_ALLOWED[key].includes(at)||info[key]===at) return;
  info[key]=at;
  try{localStorage.setItem('pix2.info',JSON.stringify(info));}catch(e){}
  drawInfo();
}
showOpts.forEach(b=>{
  b.onclick=e=>{e.stopPropagation();chooseInfo(b.dataset.info,b.dataset.at);};
});

// A chip made later — a write repaints access, a stack gets its badge, a
// stack opened in place brings its files — is made straight into the cell, by
// code that knows nothing about lanes. Rather than teach every one of them,
// anything that arrives as a cell's own child is put where it belongs.
if(typeof MutationObserver!=='undefined'&&grid){
  let due=new Set(), queued=false;
  new MutationObserver(records=>{
    for(const r of records){
      const t=r.target;
      if(t&&t.classList&&t.classList.contains('cell')) due.add(t);
      for(const n of r.addedNodes||[])
        if(n.classList&&n.classList.contains('cell')) due.add(n);
    }
    if(queued||!due.size) return;
    queued=true;
    requestAnimationFrame(()=>{
      queued=false;
      const list=[...due]; due=new Set();
      list.forEach(placeInfo); clampInfo(list);
    });
  }).observe(grid,{childList:true,subtree:true});
}
// Suggested stacks folded behind the one each would show, or apart as the
// separate photographs they are. In the address, because it changes what
// the server sends; offered here, because it is how you look, not what at.
document.querySelectorAll('.apartopt').forEach(b=>{
  b.setAttribute('aria-pressed',
    (b.dataset.at==='apart')===(VIEW.apart==='1')?'true':'false');
  b.onclick=e=>{
    e.stopPropagation();
    const want=b.dataset.at==='apart'?'1':null;
    if((VIEW.apart||null)!==want) location.href=url({apart:want});
  };
});
let infoTimer=null;
window.addEventListener('resize',()=>{
  clearTimeout(infoTimer);
  infoTimer=setTimeout(()=>clampInfo(cells),150);
});
drawInfo();

// --- filter chips ------------------------------------------------------------
// A filter's values, however many: none, one, or a list of them.
function asList(v){
  return v===null||v===undefined||v===''?[]:Array.isArray(v)?v:[v];
}
// The view into a query string. Several values are the same name repeated,
// which is how a form would send them and how the server reads them.
function putView(q,view){
  for(const [k,v] of Object.entries(view)) for(const x of asList(v)) q.append(k,x);
}
function url(patch){
  const q=new URLSearchParams();
  putView(q,{...VIEW,...patch});
  // Keep the grouping across a filter change: it is how you are reading the
  // library, not what you are reading.
  q.set('group',GROUPING.join(',')||'none');
  // Back to the page you are standing on. Both of these said `/browse`, so
  // every filter and every regrouping worked perfectly and then left the
  // landing page — which looks exactly like a control that does nothing,
  // except that the library you were summarising turns into a wall of files.
  return PAGE+(q.toString()?'?'+q:'');
}
// Only the filters that are doing something, and a `+` for the rest.
//
// Every filter, always, was a row of eleven controls that grew every time the
// app learned to ask something new — ten of them saying nothing, in front of
// the one or two that are the address of what you are looking at. The unused
// ones are a list of questions, and a list of questions belongs in a menu.
// The glyph a filter is drawn with, and its name where it has no glyph. The
// fallback is not decoration: a filter added to `_CHIPS` without a drawing
// would otherwise be a button with nothing in it, which is invisible — so an
// undrawn filter falls back to being a word, the way all of them used to be.
const MARK=(typeof MARKS!=='undefined')?MARKS:{};
// A fixed filter's headings (`_FIXED_GROUPS`), where it has any.
const HEADS=(typeof FIXED_GROUPS!=='undefined')?FIXED_GROUPS:{};
function markOf(col,label){return MARK[col]||esc(label);}

function drawChips(){
  // A stack's page has no filter bar: eleven questions about the library, on
  // a page that is eight frames of one moment. The script is one script for
  // every page and each provides the elements it has — the landing page has
  // no viewer and none is wired — and this is the one place that reached for
  // an element instead of asking for it, which on a page without it took
  // every handler down with it on load.
  if(!chips) return;
  chips.innerHTML='';
  // **One pass, in one order, whatever is set.** Every filter keeps the same
  // place in the bar whether it is doing something or not.
  //
  // They used to be drawn in two passes — the ones in use, then the rest — so
  // setting a filter made its glyph jump from ninth place to first, and
  // clearing it threw the glyph back again. Nothing on the bar could be
  // reached from memory: the camera was wherever the camera happened to be
  // that second, and the position you reached for belonged to whatever was
  // last switched on. Grouping the active ones at the left reads well in a
  // screenshot and is unusable under a thumb.
  //
  // They are still told apart at a glance — one is lit and carries a value,
  // the other is a dim glyph — which is the job colour is for. Position is
  // for finding things.
  const spare=CHIPS.filter(([col])=>!VIEW[col]);
  for(const [col,label] of CHIPS){
    const vs=asList(VIEW[col]);
    const v=vs.length?vs[0]:null;
    const b=document.createElement('button');
    // The cross climbs a level only from one value; from several it clears.
    const up=vs.length===1?wider(col,v):null;
    // The name is the drawing now. It stays in `title` for a pointer and in
    // `aria-label` for everything else — a glyph with no name anywhere is a
    // control only the person who drew it can read.
    b.title=label;
    if(v){
      b.className='chip on';
      // The first, and how many more: the whole list is in the menu, and
      // four names in a chip would push the rest of the bar off a phone.
      const said=labelFor(col,v)+(vs.length>1?' +'+(vs.length-1):'');
      b.setAttribute('aria-label',
                     label+': '+vs.map(x=>labelFor(col,x)).join(', '));
      b.innerHTML=markOf(col,label)
                 +`<span class="val">${esc(said)}</span>`
                 +`<span class="x" title="${up?'Up to '+esc(up):'Clear'}">`
                 +'&times;</span>';
    }else{
      // `spare` as well as `off`, because a narrow screen shows only the
      // filters that are doing something and puts the rest behind the `+`:
      // ten glyphs fit across a desktop bar and do not fit across a phone,
      // and which it is can change while the page is open by turning the
      // phone over. Hiding them leaves the rest exactly where they were.
      b.className='chip off spare';
      b.setAttribute('aria-label',label);
      b.innerHTML=markOf(col,label);
    }
    b.onclick=e=>{
      e.stopPropagation();
      if(v&&e.target.closest('.x')){location.href=url({[col]:up});return;}
      openMenu(b,{column:col,mode:'filter'});
    };
    chips.appendChild(b);
  }
  if(spare.length){
    const add=document.createElement('button');
    add.className='chip addchip';
    add.textContent='+';
    add.title='Add a filter';
    add.onclick=e=>{e.stopPropagation();filterMenu(add,spare);};
    chips.appendChild(add);
  }
  // An opened stack, said the way an operation is: not a chip, because a chip
  // is a value picked from a list and there is no list of stacks to pick from
  // — you arrive inside one by opening it. But it has to say where you are
  // and be dismissable for the same reason the chips are, because a filter
  // you cannot see is a library that looks smaller than it is. Until this,
  // the only way out of a stack was the browser's own back button.
}

// Which question to ask, and then what to answer — two steps, because the
// value list is the same one the chip itself opens and building a second
// version of it here is how the two would come to disagree.
function filterMenu(anchorEl,spare){
  const key='addfilter';
  if(menuCtx&&menuCtx.key===key&&!menu.hidden){closeMenu();return;}
  menu.innerHTML='<div id="menulist"></div>';
  const list=menu.querySelector('#menulist');
  const head=document.createElement('div');
  head.className='band';
  head.textContent='Filter by';
  list.appendChild(head);
  for(const [col,label] of spare){
    const d=document.createElement('div');
    d.className='opt';
    // Glyph beside name, which is where the glyphs are learnt: this list is
    // the only place in the app that says both at once.
    d.innerHTML=`<i class="mark">${MARK[col]||''}</i><span>${esc(label)}</span>`;
    d.onclick=e=>{e.stopPropagation();closeMenu();
                  openMenu(anchorEl,{column:col,mode:'filter'});};
    list.appendChild(d);
  }
  placeMenu(anchorEl);
  menuCtx={key};
}

// Under the control that opened it, and never off the right-hand edge.
function placeMenu(anchorEl){
  const r=anchorEl.getBoundingClientRect();
  const room=window.innerWidth;
  // Below 316px the old arithmetic went negative and hung the menu off the
  // left edge of the screen to discover it. On anything this narrow a 300px
  // menu is not standing beside something anyway, so it spans instead.
  if(room<=720){
    menu.style.left='8px';
    menu.style.width=(room-16)+'px';
  }else{
    menu.style.left=Math.max(8,Math.min(r.left,room-316))+'px';
    menu.style.width='';
  }
  menu.style.top=(r.bottom+window.scrollY+4)+'px';
  menu.hidden=false;
}

// One step wider, or nothing where there is no such step.
//
// A date is a prefix — `2026`, `2026-09`, `2026-09-15` — so it is the one
// filter that is a hierarchy rather than a value, and closing it should mean
// *out of September*, not *out of dates altogether*. Going all the way is then
// two more clicks, where the old behaviour had no way back to the year but
// retyping it.
function wider(col,v){
  // An event narrows the same way a date does, so the cross climbs rather
  // than clears: a part of an event goes up to the whole event, and the
  // event to the whole library. Which is also the way out of *no sub-event*
  // — it widens to the trip it is a slice of.
  if(col==='event'){
    const [head,leaf]=splitEvent(v);
    return leaf?head:null;
  }
  if(col!=='date') return null;
  const at=String(v).lastIndexOf('-');
  return at<0?null:String(v).slice(0,at);
}

function labelFor(col,v){
  // Dated to that and no finer: the chip says what is missing, the way the
  // folder that set it did.
  if(col==='date'&&String(v).endsWith('-*')){
    const known=String(v).slice(0,-2);
    return known+(known.length===4?', no month':', no day');
  }
  // *Sicily* is the trip and *Sicily, no sub-event* is the part of it nobody
  // has divided up yet — two different sets of files, and a chip that showed
  // the same word for both would be the bar disagreeing with the folder that
  // set it.
  if(col==='event'){
    const [head,leaf]=splitEvent(v);
    if(leaf===NO_EVENT) return head+' (no sub-event)';
  }
  const fixed=FIXED[col];
  if(!fixed) return v;
  const hit=fixed.find(f=>f[0]===v);
  return hit?hit[1]:v;
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

// --- the shared menu ---------------------------------------------------------
let menuCtx=null;
function closeMenu(){
  const then=menuCtx&&menuCtx.onclose;
  menu.hidden=true; menuCtx=null;
  if(then) then();
}
// Anywhere outside dismisses. The opener stops propagation and toggles,
// so clicking the same label again closes rather than reopening — a menu
// you cannot dismiss with the control that opened it feels stuck.
document.addEventListener('click',e=>{
  if(!menu.hidden&&!menu.contains(e.target)) closeMenu();
});
// Not a filter's checklist: on a phone the keyboard coming up is a resize,
// and closing would show what was ticked while it was still being ticked.
window.addEventListener('resize',()=>{
  if(menuCtx&&menuCtx.onclose) return;
  closeMenu();
});
// Closing on a scroll means *you have moved on*. On a phone it meant the
// keyboard: focusing a field makes the browser scroll the document to bring
// it into view, so the menu shut the instant it became usable — every Event,
// Tags, Date and Access, on the device this library is mostly read on, opened
// and then disappeared. Nothing in the app was broken and none of it worked.
//
// So a scroll with the cursor still inside the menu is the browser moving the
// page, not the reader. Nothing else changes: the menu is positioned in the
// document and scrolls with it either way.
window.addEventListener('scroll',()=>{
  const at=(typeof document!=='undefined')&&document.activeElement;
  if(at&&!menu.hidden&&menu.contains(at)) return;
  closeMenu();
},{passive:true});

menu.addEventListener('click',e=>e.stopPropagation());

async function openMenu(anchorEl,ctx){
  const key=ctx.mode+':'+(ctx.column||'')+':'+(ctx.as||'');
  if(menuCtx&&menuCtx.key===key&&!menu.hidden){closeMenu();return;}
  ctx.key=key; menuCtx=ctx;
  placeMenu(anchorEl);

  if(ctx.mode==='date'){drawDate();return;}
  const fixed=FIXED[ctx.column];
  menu.innerHTML='<input id="menuq" autocomplete="off">'
                +'<div id="menulist" class="dim" style="padding:10px 11px">'
                +'loading…</div>';
  const q=document.getElementById('menuq');
  q.placeholder = ctx.mode!=='set' ? 'Filter…'
    : ctx.as==='access' ? 'Tick who can see these'
    : ctx.as==='tags' ? 'Tick a tag, or type a new one'
    : ctx.as==='event' ? 'Name an event, or pick one below'
    : 'Type a new name, or pick one below';
  q.oninput=()=>render(q.value);
  q.onkeydown=e=>{
    if(e.key==='Enter'&&ctx.mode==='set'&&ctx.column!=='audience'
       &&q.value.trim()){
      choose(q.value.trim()); e.preventDefault();
    }
    // Escape is *never mind*: what was ticked is not shown.
    if(e.key==='Escape'){ctx.onclose=null; closeMenu();}
    e.stopPropagation();
  };
  // Not on a phone. There the keyboard is half the screen, and it would come
  // up over the list before anyone has decided whether they want to type —
  // when what is usually wanted is the third name down, already on screen.
  // Tapping the field is how you ask for it.
  if(!COARSE) setTimeout(()=>q.focus(),0);

  let opts=[];
  if(fixed && ctx.mode!=='set'){
    // A fixed list, but only what is there: the server counts each value,
    // and the label is the one the bar uses for it.
    let counted=[];
    try{
      const p=new URLSearchParams();
      putView(p,VIEW);
      p.set('column',ctx.column);
      counted=await (await fetch('/api/suggest?'+p)).json();
    }catch(e){counted=[];}
    const n=new Map((Array.isArray(counted)?counted:[]).map(o=>[o.value,o]));
    opts=fixed.map(f=>({value:f[0],label:f[1],
                        n:n.has(f[0])?n.get(f[0]).n:null,
                        scope:n.has(f[0])?n.get(f[0]).scope:'all'}));
  }else{
    const p=new URLSearchParams();
    putView(p,VIEW);
    p.set('column',ctx.column);
    // What the selection spans, which only the page knows. Undated files are
    // left out rather than counted as some smallest date — one of those would
    // stretch the range across the whole library and propose everything.
    if(ctx.mode==='set'&&ctx.column==='event'){
      const days=targetsOn('live').map(c=>c.dataset.date)
                                  .filter(d=>d&&d!=='no date').sort();
      if(days.length){p.set('near_from',days[0]);
                      p.set('near_to',days[days.length-1]);}
    }
    try{
      const res=await fetch('/api/suggest?'+p);
      opts=(await res.json()).map(o=>({...o,label:o.value}));
    }catch(e){opts=[];}
    // Extras are prepended, not substituted: "shared with nobody" is a state
    // rather than a name, and the configured logins have to be offerable
    // before any file carries them.
    const extra=ctx.mode==='filter'?(EXTRA[ctx.column]||[]):[];
    // *Add* offers what is valid to grant; *remove* offers what is actually
    // there. They differ, and the difference matters: a grant left behind by
    // a renamed or deleted account names nobody, and seeding the remove list
    // from the account list would make it unremovable.
    //
    // Hiding is offered with the grants rather than as a button of its own:
    // it answers the same question — who may see this — with *nobody, me
    // included*. Only when acting, because the filter has it in `extra`.
    // Only when acting. A filter asks which files to look at, and a name
    // nothing is shared with answers *none* — so it is not offered there;
    // granting access is the one place a name nobody has yet is the point.
    const seed=ctx.column!=='audience'||ctx.mode==='filter' ? []
      : [[ARCHIVED,ARCHIVED_LABEL],...USERS.map(u=>[u,u])];
    const have=new Set(opts.map(o=>o.value));
    // A state the server counted keeps the words the bar has for it.
    const named=new Map(extra.map(e=>[e[0],e[1]]));
    opts.forEach(o=>{if(named.has(o.value)) o.label=named.get(o.value);});
    opts=[...extra,...seed].filter(e=>!have.has(e[0]))
      .map(e=>({value:e[0],label:e[1],n:null,scope:'all'}))
      .concat(opts);
  }
  // A filter offers only what some file in the library has — except what is
  // already ticked, which has to stay where it can be unticked.
  if(ctx.mode==='filter'){
    const ticked=new Set(asList(VIEW[ctx.column]));
    opts=opts.filter(o=>o.n===null||o.n===undefined
                        ? ticked.has(o.value)
                        : o.n>0||ticked.has(o.value));
  }
  if(menuCtx!==ctx) return;   // a later menu opened while this was loading

  // `act` names the field where it is not simply the column — an event has
  // two halves and each is written on its own, so that renaming an event
  // keeps each file's own sub-event on the end of the new name. `stay` keeps
  // the panel up and redraws it: naming an event is half a gesture, and the
  // parts of that event are in the list directly under it.
  async function choose(value,act,stay){
    if(ctx.mode==='filter'){flip(value);return;}
    if(!stay) closeMenu();
    const cs=await acting();
    if(!cs) return;
    if(!cs.length){workClose();say('nothing selected');return;}
    await applyToSelection(act||ctx.as||ctx.column,value,true,cs);
    if(FOLDERS) redrawFolders();
    if(stay&&menuCtx===ctx&&!menu.hidden){
      const q=document.getElementById('menuq');
      render(q?q.value:'');
    }
  }

  // A checklist, not a list of commands — and a half-ticked box completes,
  // it does not clear.
  //
  // It used to clear, on the argument that taking access away is the safer
  // direction and should be the one that costs a single click. Two things
  // were wrong with that. Ticking `family` on a folder that is ninety per
  // cent `family` is not an ambiguous gesture: it says *all of it*, and
  // answering with *none of it* is the opposite of what was asked, on the
  // ninety per cent that were already right. And `event` beside it always
  // completed — one field going one way and the others the other, in one
  // menu, under one kind of box.
  //
  // So: none and some both add, all removes. The destructive direction is the
  // one you reach by ticking a box that is already full, which is the only
  // state where it reads as *undo this*.
  // The field each action edits. `event` holds one value where tags and
  // access hold many, but the question the menu asks is the same one —
  // *do these files say this?* — so it is one control either way.
  const FIELD=MULTI[ctx.as]?MULTI[ctx.as][0]:(ctx.as==='event'?'event':null);

  // **A filter is any of several.** Ticking stages a value rather than
  // going there, so a second and a third can be ticked in the same visit;
  // what is ticked is shown when the menu closes — a click elsewhere, the
  // chip again, or Show — and Escape leaves the view as it was. Clear unticks
  // everything at once, which is the short way from several to one.
  const picks=ctx.mode==='filter'?new Set(asList(VIEW[ctx.column])):null;
  const was=picks?JSON.stringify([...picks].sort()):'';
  function flip(value){
    if(picks.has(value)) picks.delete(value); else picks.add(value);
    const q=document.getElementById('menuq');
    render(q?q.value:'');
  }
  if(picks) ctx.onclose=()=>{
    const now=[...picks];
    if(JSON.stringify(now.slice().sort())===was) return;
    location.href=url({[ctx.column]:now.length?now:null});
  };

  async function toggle(o,row){
    if(picks){flip(o.value);return;}
    // On the landing page this is every file the chosen folders hold, read
    // once and kept: the tri-state has to be able to say *some of them*, and
    // there is no way to know that about a folder without asking.
    const cs=await acting();
    if(!cs) return;
    if(!cs.length){workClose();say('nothing selected');return;}
    // A sub-event row writes the whole name, both halves at once.
    //
    // An event row depends on whether they are all in that event already,
    // and it is the tri-state rule the rest of this menu follows: none and
    // some add, all removes. Somewhere else, that event is where these files
    // are going, and each one's own sub-event goes with it — which is why it
    // is written as half a name. On the event they are all in, the same row
    // can only mean *this event and no part of it*, so it writes the whole
    // name, which is the name with no part on the end. There is no separate
    // *No sub-event* row: it would say the same thing twice, and the event's
    // own name is the plainer way to say it.
    if(nested){
      const flat=!o.sub&&cs.length&&cs.every(
        c=>splitEvent(c.dataset.event||'')[0]===o.value);
      await choose(o.value,(o.sub||flat)?'event':'event_head',!o.sub);
      return;
    }
    const state=shareState(cs,FIELD,o.value);
    if(FIELD==='event'){
      // Ticking the value they already have clears it; anything else sets
      // it. One value, so there is nothing to add to.
      await applyToSelection('event',state==='all'?null:o.value,true,cs);
    }else{
      await applyToSelection(ctx.as,o.value,state!=='all',cs);
    }
    if(FOLDERS) redrawFolders();
    mark(row,shareState(cs,FIELD,o.value));
  }
  function mark(row,state){
    row.dataset.state=state;
    const box=row.querySelector('.box');
    if(box) box.textContent=state==='all'?'✓':state==='some'?'●':'';
  }
  // **One list, with the sub-events under the events they are part of.** It
  // was a second panel, reached by opening an event — which hid the very
  // thing it was there to offer, redrew the whole menu to get to it, and
  // meant nobody could see what an event was divided into without first
  // choosing it. A sub-event is a part of an event and reads as one:
  // indented under it, in a list you can see all of at once.
  // The filter reads the same shape as the action, because it is a list of
  // the same names: *Sicily* with its parts under it, rather than *Sicily*,
  // *Sicily > Taormina*, *Sicily > Catania* — three rows repeating a word
  // and burying the one thing that differs at the end of each.
  const nested = ctx.as==='event'
              || (ctx.mode==='filter' && ctx.column==='event');
  // A file saying *Sicily > Taormina* does say *Sicily* when the question is
  // which event it is in, and says *Sicily > Taormina* when the question is
  // which part of it. So a row is ticked against its own width rather than
  // against the whole name either way, which left one or the other unticked
  // with the answer on the screen behind it.
  function stateOf(o){
    if(picks) return picks.has(o.value)?'all':'none';
    const cs=targets();
    if(!nested) return shareState(cs,FIELD,o.value);
    const n=cs.filter(c=>{
      const ev=c.dataset.event||'';
      return o.sub ? ev===o.value : splitEvent(ev)[0]===o.value;
    }).length;
    return n===0?'none':(n===cs.length?'all':'some');
  }
  // Every event once, carrying its own sub-events. The server sends whole
  // names — *Sicily*, *Sicily > Taormina* — because a whole name is what a
  // file holds; the shape this menu needs is the tree they describe.
  function tree(){
    const by=new Map();
    const at=head=>{
      let h=by.get(head);
      if(!h){h={value:head,label:head,n:0,scope:null,subs:[]}; by.set(head,h);}
      return h;
    };
    for(const o of opts){
      const [head,leaf]=splitEvent(o.value);
      const h=at(head);
      h.n+=(o.n||0);
      // A bare row is the event speaking for itself and its scope is the
      // event's; a sub-event's stands in only while nothing better has been
      // seen, or an event known solely by its parts falls to the bottom.
      if(!leaf||h.scope==null) h.scope=o.scope;
      // How many are in the event and in no part of it — which is a row of
      // its own once some of them *are* in a part.
      if(!leaf) h.own=o.n;
      if(leaf) h.subs.push({value:o.value,label:leaf,sub:true,n:o.n});
    }
    // The event the selection is already in belongs in the list whether or
    // not anything in the view carries it: it is the one whose parts are
    // being asked about.
    standing().forEach(v=>at(v));
    return [...by.values()];
  }
  function shown(){ return nested ? tree() : opts; }
  // What the selected files already say. Events, not whole names — a
  // sub-event hangs off its event and is ticked there.
  function standing(){
    if(!FIELD) return [];
    const have=currentValues(targets(),FIELD);
    if(!nested) return have;
    return [...new Set(have.map(v=>splitEvent(v)[0]).filter(Boolean))];
  }

  function render(text){
    const t=(text||'').toLowerCase();
    const checkable=(ctx.mode==='set'&&!!FIELD)||!!picks;
    const list=document.createElement('div');
    list.id='menulist';
    if(picks){
      const bar=document.createElement('div');
      bar.className='fbar';
      const n=picks.size;
      const said=document.createElement('span');
      said.className='dim';
      said.textContent=n?n+' ticked':'Any';
      const clear=document.createElement('button');
      clear.type='button'; clear.className='fclear'; clear.textContent='Clear';
      clear.disabled=!n;
      clear.onclick=e=>{
        e.stopPropagation(); picks.clear();
        const q=document.getElementById('menuq'); render(q?q.value:'');
      };
      const go=document.createElement('button');
      go.type='button'; go.className='fshow primary'; go.textContent='Show';
      go.onclick=e=>{e.stopPropagation(); closeMenu();};
      bar.appendChild(said); bar.appendChild(clear); bar.appendChild(go);
      list.appendChild(bar);
    }
    const typed=(text||'').trim();
    // An event matches on its own name or on any of its parts. A match on
    // the event keeps all of them: you are looking for the event, and its
    // parts are what it is made of.
    const hits=!nested ? opts.filter(o=>o.label.toLowerCase().includes(t))
      : tree().map(h=>{
          const me=h.label.toLowerCase().includes(t);
          const subs=me?h.subs:h.subs.filter(
            s=>s.label.toLowerCase().includes(t));
          return (me||subs.length)?{...h,subs}:null;
        }).filter(Boolean);
    // Tags are invented as you go; access is not. Somebody who can be given
    // access is an account or a role, made under Accounts — offering to
    // create one here would write a grant that reaches nobody. An event is
    // invented in the same way a tag is: naming one is the work, not picking
    // from a list of names somebody already made.
    const invent=ctx.mode==='set'&&(ctx.column==='tag'||nested);
    if(invent&&typed&&!hits.some(o=>o.label===typed)){
      // The box at the top names events. A part of one is named in the box
      // under the event it is part of, which is where there is an event for
      // it to be part of.
      const o=opt({label:'Add “'+typed+'”',n:null},
                  ()=>choose(typed,nested?'event_head':null,nested));
      o.classList.add('new'); list.appendChild(o);
    }
    if(ctx.mode==='set'&&!checkable){
      list.appendChild(opt({label:'Clear',n:null},()=>choose(null)));
    }
    // Ticking the event a file already has writes it again rather than
    // clearing it — there is a second half to name and the panel stays on
    // it — so taking an event off has to be a row of its own.
    // Only where a press writes something. In the filter this would mean
    // *clear*, and no menu here offers a row that means everything.
    if(nested&&ctx.mode==='set') list.appendChild(opt(
      {label:'No event',n:null},()=>choose(null,'event')));
    // What these files already say comes first, ticked, so the menu opens
    // showing the answer instead of asking a question whose answer is on
    // the screen behind it.
    const present=checkable?standing():[];
    // One event and everything that is part of it, in the order it is read:
    // the event, then a box for a part it has not got yet, then the parts it
    // has. The box only under an event these files are in — a sub-event of
    // an event they are not in is a name with nothing to attach it to.
    const place=(o,into)=>{
      const d=opt(o,()=>choose(o.value),checkable);
      // One row, two meanings, depending on where these files already are.
      if(nested&&!o.sub) d.title=stateOf(o)==='all'
        ? o.label+', and no part of it'
        : 'Put these in '+o.label+', each keeping its own sub-event';
      into.appendChild(d);
      if(!nested) return;
      (o.subs||[]).forEach(sub=>{
        const s=opt(sub,()=>choose(sub.value,'event'),checkable);
        s.classList.add('sub'); into.appendChild(s);
      });
      // An event that has been divided up, and still has files in none of
      // the parts: those files are a set you can ask for, and the folder the
      // grouping makes for them is reachable from the bar as well.
      //
      // Only in the filter. In the action menu the event's own name already
      // means *and no part of it*, so a row here would be the same answer
      // twice in less plain words. And only where there is something in it:
      // an event divided all the way up has no leftovers, and a row matching
      // nothing is a row that looks broken when pressed.
      if(ctx.mode==='filter'&&(o.subs||[]).length&&o.own){
        const rest=o.value+EVENT_SEP+NO_EVENT;
        const d=opt({value:rest,label:'No sub-event',n:o.own},
                    ()=>choose(rest));
        d.classList.add('sub'); into.appendChild(d);
      }
      // After the parts there are, not before them: naming a new one is rare
      // beside picking one that exists, and the list is what the panel is
      // for.
      if(present.includes(o.value)) into.appendChild(newSub(o));
    };
    if(present.length){
      const seen=present.filter(v=>v.toLowerCase().includes(t));
      if(seen.length){
        const h=document.createElement('div');
        h.className='band'; h.textContent='On these files';
        list.appendChild(h);
        seen.forEach(v=>place(
          hits.find(o=>o.value===v)||{value:v,label:v,n:null,subs:[]},list));
      }
    }
    const group=(title,band)=>{
      if(!band.length) return;
      const h=document.createElement('div');
      h.className='band'; h.textContent=title; list.appendChild(h);
      band.forEach(o=>place(o,list));
    };
    const left=hits.filter(o=>!present.includes(o.value));
    if(ctx.column==='audience'){
      // The sentinel first and on its own: *nobody has this yet* is the
      // pile of work, not a name, and grouping it with the names buried it
      // under a heading that read as though it were a deleted account.
      const special=new Set((EXTRA.audience||[]).map(e=>e[0]));
      left.filter(o=>special.has(o.value))
          .forEach(o=>list.appendChild(opt(o,()=>choose(o.value),false)));
      // Groups before individuals: a group keeps working as the household
      // changes, where naming four people does not — so it is almost always
      // the right answer and belongs where the eye lands first.
      const named=left.filter(o=>!special.has(o.value));
      group('Groups',named.filter(o=>GROUPS.includes(o.value)));
      group('People',named.filter(o=>!GROUPS.includes(o.value)
                                     &&USERS.includes(o.value)));
      group('No longer an account',
            named.filter(o=>!USERS.includes(o.value)));
    }else if(picks&&HEADS[ctx.column]){
      // A heading for each family, ticking everything under it — *all
      // videos* is Videos and Clips — then the family, then what belongs to
      // none. Tri-state, like every box here: some of them ticked is a dot.
      const inGroup=new Set();
      for(const [title,vals] of HEADS[ctx.column]){
        const kids=vals.map(v=>left.find(o=>o.value===v)).filter(Boolean);
        if(!kids.length) continue;
        kids.forEach(o=>inGroup.add(o.value));
        const head=opt({label:title,n:null},null,true);
        head.classList.add('grouphead');
        const on=kids.filter(o=>picks.has(o.value)).length;
        mark(head,on===0?'none':on===kids.length?'all':'some');
        head.onclick=e=>{
          e.stopPropagation();
          const all=kids.every(o=>picks.has(o.value));
          kids.forEach(o=>all?picks.delete(o.value):picks.add(o.value));
          const q=document.getElementById('menuq'); render(q?q.value:'');
        };
        list.appendChild(head);
        kids.forEach(o=>{
          const d=opt(o,()=>choose(o.value),true);
          d.classList.add('sub'); list.appendChild(d);
        });
      }
      left.filter(o=>!inGroup.has(o.value)).forEach(o=>place(o,list));
    }else{
      // Three bands, most relevant first: values already used by what you
      // are looking at, then by anything one filter away, then the rest.
      for(const [scope,title] of [['near','Around these dates'],
                                  ['all','In this view'],['any','Related'],
                                  ['other','Elsewhere']]){
        const band=left.filter(o=>o.scope===scope);
        if(hits.some(o=>o.scope!==scope)||present.length) group(title,band);
        else band.forEach(o=>place(o,list));
      }
    }
    if(!hits.length&&!typed&&!nested){
      list.innerHTML='<div class="band">nothing yet</div>';
    }
    menu.querySelector('#menulist').replaceWith(list);
  }
  // Naming a part of an event, in the list under the event it is part of —
  // its own box, because the one at the top of the panel names events and a
  // single box cannot be asked two questions at once.
  //
  // A word until it is wanted. Dividing an event up is done once and then
  // picked from ever after, so a box and a button standing open under every
  // event these files are in is a lot of furniture for the rare half of the
  // job. It does not filter as it is typed: rebuilding a list under a box
  // takes the focus out of it.
  function newSub(h){
    const row=document.createElement('div');
    row.className='subnew';
    const ask=document.createElement('button');
    ask.className='addsub';
    ask.textContent='Add a sub-event';
    const box=document.createElement('input');
    box.autocomplete='off';
    box.placeholder='Name a sub-event of '+h.label;
    box.hidden=true;
    const save=document.createElement('button');
    save.textContent='Save';
    save.hidden=true;
    const shut=()=>{box.hidden=true; save.hidden=true; ask.hidden=false;};
    const go=()=>{const v=box.value.trim(); if(v) choose(v,'event_leaf');
                  else shut();};
    ask.onclick=e=>{
      e.stopPropagation();
      ask.hidden=true; box.hidden=false; save.hidden=false;
      if(box.focus) box.focus();
    };
    save.onclick=e=>{e.stopPropagation();go();};
    box.onclick=e=>e.stopPropagation();
    box.onkeydown=e=>{
      if(e.key==='Enter'){go();e.preventDefault();}
      // Out of the box, not out of the menu: the list is still the thing
      // being read and folding the box away is what was asked for.
      if(e.key==='Escape') shut();
      e.stopPropagation();
    };
    row.appendChild(ask); row.appendChild(box); row.appendChild(save);
    return row;
  }
  function opt(o,fn,checkable){
    // Every row of a filter is a box: the states, the sentinels and the
    // leftovers of an event are values to tick like any other.
    if(picks) checkable=true;
    const d=document.createElement('div');
    d.className='opt';
    d.innerHTML=(checkable?'<span class="box"></span>':'')
               +`<span>${esc(o.label)}</span>`
               +(o.n!==null&&o.n!==undefined?`<span class="n">${o.n}</span>`:'');
    if(o.tip) d.title=o.tip;
    if(checkable){
      mark(d,stateOf(o));
      d.onclick=e=>{e.stopPropagation();toggle(o,d);};
    }else{
      d.onclick=fn;
    }
    return d;
  }
  render('');
}

function drawDate(){
  // The **effective** date: what the file actually has, after any override.
  // Opening on the current answer is the difference between editing a date
  // and guessing at one.
  const cs=targets();
  const parts=n=>[...new Set(cs.map(c=>{
    const d=c.dataset.date||'';
    return /^\d{4}-\d{2}-\d{2}/.test(d)?d.split('-')[n]:'';
  }))];
  const one=n=>{const v=parts(n); return v.length===1?v[0]:'';};
  const dates=[...new Set(cs.map(c=>(c.dataset.date||'').slice(0,10)))];
  const now=!cs.length ? 'nothing selected'
          : dates.length===1 ? dates[0]
          : `${dates.length} different dates`;

  // Year alone is a complete answer — that is the whole point of a partial
  // date, so month and day stay optional rather than being required to submit.
  menu.innerHTML=`<div class="form">
    <div class="hint" style="width:100%">Now: <b>${esc(now)}</b></div>
    <label>Year <input id="dy" maxlength="4" placeholder="${esc(one(0)||'*')}"
      value="${esc(one(0))}"></label>
    <label>Month <input id="dm" maxlength="2" placeholder="${esc(one(1)||'*')}"
      value="${esc(one(1))}"></label>
    <label>Day <input id="dd" maxlength="2" placeholder="${esc(one(2)||'*')}"
      value="${esc(one(2))}"></label>
    <button class="primary" id="dok">Apply</button>
    <button id="dclr">Clear</button>
    <div class="hint">Empty keeps what the file already says.</div>
  </div>`;
  const pad=(v,n)=>v.trim()?v.trim().padStart(n,'0'):'*';
  menu.querySelector('#dok').onclick=()=>{
    const dy=menu.querySelector('#dy'), dm=menu.querySelector('#dm'),
          dd=menu.querySelector('#dd');
    const y=pad(dy.value,4), m=pad(dm.value,2), d=pad(dd.value,2);
    if(y==='*'&&m==='*'&&d==='*'){closeMenu();return;}
    closeMenu();
    applyToSelection('date_override',`${y}-${m}-${d}-*:*:*`,true);
  };
  menu.querySelector('#dclr').onclick=()=>{
    closeMenu(); applyToSelection('date_override',null,true);
  };
  setTimeout(()=>menu.querySelector('#dy').focus(),0);
}

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

// --- viewer ------------------------------------------------------------------
function load(c){
  const f=encodeURIComponent(c.dataset.folder), n=encodeURIComponent(c.dataset.name);
  // Always stop the previous clip: moving on while audio keeps playing from the
  // one before is the kind of thing that makes a viewer feel broken.
  vvid.pause(); vvid.removeAttribute('src'); vvid.load();
  // A clip plays as its source between its two ends, which is what a media
  // fragment says to the browser; a still is its source stopped on the frame.
  const clip=c.dataset.clip?c.dataset.clip.split(','):null;
  if(clip&&c.dataset.kind!=='video'){
    vimg.classList.remove('on'); vvid.classList.add('on');
    vvid.src=`/media/${f}/${n}#t=${clip[0]}`;
  }else if(c.dataset.kind==='video'){
    vimg.classList.remove('on'); vvid.classList.add('on');
    vvid.src=`/media/${f}/${n}`+(clip?`#t=${clip[0]},${clip[1]}`:'');
    vvid.play().catch(()=>{});
  }else{
    vvid.classList.remove('on'); vimg.classList.add('on');
    vimg.src=`/preview/${f}/${n}`;
  }
  drawGet(c); drawTop(c); drawSplice(c); drawViewActs(c);
  vmeta.textContent=`${c.dataset.name} — ${c.dataset.date}`
                   +(c.dataset.tags?' — '+c.dataset.tags.split('\n').join(', '):'');
  fill(c);
}
// Saying *this is the one* about the photograph filling the screen. Only on a
// stack's page, where that is the question being asked; the grid's viewer is
// for looking, and a button deciding the shape of a stack has no business
// appearing over an ordinary photograph.
const viewTop=document.getElementById('viewtop');
function drawTop(c){
  if(!viewTop) return;
  const here=c&&stackKey(c)===keyOf(c);
  // Nothing to say on the one that already shows a stack somebody made — it
  // is the answer already given. Everything to say on a guess, which is what
  // the different word is for.
  viewTop.hidden=!c||!STACK||(here&&!guessed(c));
  viewTop.textContent=here?'Confirm top':'Show this one';
  viewTop.title=here
    ? 'Keep this one showing, and make them a stack'
    : 'Make this the one the stack shows';
}
if(viewTop) viewTop.onclick=e=>{
  e.stopPropagation();
  const c=cells[cur];
  if(!c) return;
  // The viewer closes because the page is about to: answering is the thing
  // this stack was opened to do, and `afterStacking` leaves for the grid.
  closeViewer();
  chooseTop(c);
};
const rail=document.getElementById('rail');
const railToggle=document.getElementById('railtoggle');
// Remembered per browser: whether you want the numbers alongside is a
// working style, not a per-photo choice.
let railOn=true;
try{railOn=localStorage.getItem('pix2.rail')!=='0';}catch(e){}
// Narrow enough and there is no room for a photograph and a column of numbers
// at once, so details is a tab rather than a rail — and a tap on a photograph
// opens the photograph. The stored preference is about the column; it says
// nothing about which tab you want to land on.
const tabbed=()=>media('(max-width: 720px)');
if(tabbed()) railOn=false;
function drawRail(){
  viewer.classList.toggle('norail',!railOn);
  // A tab is named for where it goes; a rail is named for what it does.
  railToggle.textContent=tabbed()?(railOn?'Photo':'Details')
                                 :(railOn?'Hide details':'Details');
  // Not remembered while it is a tab: the preference belongs to the column,
  // and writing it here would mean turning the phone sideways once decided
  // how every desktop viewer opened from then on.
  if(!tabbed()){
    try{localStorage.setItem('pix2.rail',railOn?'1':'0');}catch(e){}
  }
}
if(railToggle) railToggle.onclick=e=>{
  e.stopPropagation();railOn=!railOn;drawRail();
                       if(railOn&&cells[cur]) fill(cells[cur]);};
const viewClose=document.getElementById('viewclose');
if(viewClose) viewClose.onclick=e=>{e.stopPropagation(); closeViewer();};
// Where you have decided you want this one. A link rather than a button, so
// the browser does the transfer and a right-click still offers *save as*.
const viewGet=document.getElementById('viewget');
// Splice from the preview, without going back to the grid for it. A clip
// opens its source's timeline, standing on the clip.
const viewSplice=document.getElementById('viewsplice');
function drawSplice(c){
  if(!viewSplice) return;
  const to=c&&c.dataset.splice;
  viewSplice.hidden=!to;
  if(!to) return;
  viewSplice.href='/splice/'+encodeURIComponent(c.dataset.folder)+'/'
    +encodeURIComponent(to)
    +(to===c.dataset.name?'':'#'+encodeURIComponent(c.dataset.name));
}
function drawGet(c){
  if(!viewGet||!c) return;
  const at='/download/'+encodeURIComponent(c.dataset.folder)
          +'/'+encodeURIComponent(c.dataset.name);
  viewGet.setAttribute('href',at);
  // The original is a second thing to want only where it is a different file
  // — which is the clips a browser will not play as they are, and nothing
  // else in the library.
  const touch=COARSE&&CAN_SHARE;
  // The drawing says it; the name is for the pointer and the screen reader.
  viewGet.setAttribute('aria-label',
    touch?'Save':(c.dataset.copy?'Download copy':'Download'));
  viewGet.title=touch
    ? 'Save this to Photos, Files, or anywhere else.'
    : (c.dataset.copy
       ? 'The H.264 copy. Hold shift for the original off the camera.'
       : 'The file as it came off the camera.');
  viewGet.onclick=e=>{
    e.stopPropagation();
    if(touch){
      // **The playable copy, where there is one.** Everywhere else in the app
      // the original is the thing to want, and from the grid it still is — but
      // this button puts a file in Photos, and the whole reason a render
      // exists is that the original is something that will not play. Saving
      // the camera's HEVC here would put a clip in the photo library that the
      // photo library cannot show.
      e.preventDefault();
      saveFiles([c],false);
      return;
    }
    if(e.shiftKey&&c.dataset.copy) viewGet.setAttribute('href',at+'?original=1');
    else viewGet.setAttribute('href',at);
  };
}
// Everything above is the viewer, which only a page with photographs on it
// has. The landing page shows folders: it carries none of these elements, so
// the script wires none of them. Guarded one statement at a time rather than
// wrapped in a block, because the functions here are called from the grid and
// a block would put them out of its reach.
if(viewer) drawRail();

const details=new Map();
async function fill(c){
  if(!railOn) return;
  const key=c.dataset.folder+'\n'+c.dataset.name;
  rail.innerHTML='<p class="dim">loading…</p>';
  let d=details.get(key);
  if(!d){
    try{
      // With the view, so where a clip came from can be answered inside it.
      const p=new URLSearchParams();
      putView(p,VIEW);
      const r=await fetch(`/api/file/${encodeURIComponent(c.dataset.folder)}`
                         +`/${encodeURIComponent(c.dataset.name)}?`+p);
      if(!r.ok) throw new Error(await r.text());
      d=await r.json(); details.set(key,d);
    }catch(e){rail.innerHTML='<p class="dim">no details</p>';return;}
  }
  // The cursor may have moved on while this was in flight.
  if(cells[cur]!==c) return;
  rail.innerHTML=railHtml(d);
}

function kv(rows){
  const body=rows.filter(r=>r[1]!==null&&r[1]!==undefined&&r[1]!=='')
    .map(r=>`<dt>${esc(r[0])}</dt><dd${r[2]?' class="'+r[2]+'"':''}>`
            +`${r[3]?r[1]:esc(r[1])}</dd>`).join('');
  return body?`<dl class="kv">${body}</dl>`:'';
}
function bytes(n){
  if(!n&&n!==0) return null;
  const u=['B','KB','MB','GB']; let i=0, v=n;
  while(v>=1024&&i<u.length-1){v/=1024;i++;}
  return (i?v.toFixed(1):v)+' '+u[i];
}
function secs(n){
  if(n===null||n===undefined) return null;
  const t=Math.round(n); return `${Math.floor(t/60)}:${String(t%60).padStart(2,'0')}`;
}

// Fact and judgement are shown apart, always. Collapsing them into one
// "date" would hide the only interesting question: is this what the file
// says, or what somebody chose?
function railHtml(d){
  const dec=d.decided||{};
  const inh=d.inherited||{};
  const overridden=v=>`<span class="set">${esc(v)}</span>`;

  const dateRows=[['Effective',d.effective_date||'—']];
  if(d.date_override){
    dateRows.push(['Camera said',d.capture_date||'nothing','was']);
    dateRows.push(['Override',overridden(d.date_override),null,true]);
  }else{
    dateRows.push(['Camera said',d.capture_date||'nothing']);
  }

  const eventRow=dec.event
    ? [['Event',overridden(dec.event),null,true],
       ...(inh.event_auto&&inh.event_auto!==dec.event
           ? [['Inherited',inh.event_auto,'was']] : [])]
    : [['Event',d.event||'—']];

  const tags=(d.tags||[]).map(t=>`<span class="pill">${esc(t)}</span>`).join('');
  // Under a heading of their own rather than mixed in with the tags. They are
  // pills either way, and *Mum* sitting in a row with *beach* and *sunset*
  // reads as a keyword — which is the one thing a person is not.
  const folk=(d.people||[]).map(p=>`<span class="pill">${esc(p)}</span>`).join('');
  const all=Object.entries(d.exif||{})
    .map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');

  // Where a clip came from — only there for someone who may see it.
  const cl=d.clip;
  // Into the source's own preview: here if it is on this page, in this view
  // if it is in it, and only otherwise on a page of its own day.
  const from=cl?(cl.in_view
    ? location.pathname+location.search+'#open:'+encodeURIComponent(cl.key)
    : cl.open):'';
  const clipHtml=cl?`<div class="rail-h">${cl.start===cl.end?'Still':'Clip'}</div>`
    + kv([['Cut from',`<a class="to-source" data-key="${esc(cl.key)}" `
                      +`href="${esc(from)}">${esc(cl.source)}</a>`,null,true],
          [cl.start===cl.end?'At':'Range',
           cl.start===cl.end?secs(cl.start):secs(cl.start)+' – '+secs(cl.end)],
          ...(cl.splice?[['Clips',`<a href="${esc(cl.splice)}">edit on its timeline</a>`,
                          null,true]]:[])])
    :'';
  // The clips cut from this video — or, on a clip, the others cut from its
  // source — each a way to its preview, the same way *cut from* is.
  const toPreview=x=>x.in_view
    ? location.pathname+location.search+'#open:'+encodeURIComponent(x.key)
    : x.open;
  const list=(d.clips||[]).map(x=>`<a class="to-source" data-key="${esc(x.key)}" `
      +`href="${esc(toPreview(x))}">${x.start===x.end
        ?'Still at '+secs(x.start):secs(x.start)+' – '+secs(x.end)}</a>`)
    .join('<br>');
  const listHtml=list?`<div class="rail-h">Clips and photos`
      +` (${d.clips.length})</div><div class="cliplist">${list}</div>`:'';
  return clipHtml + listHtml + `<div class="rail-h">Decisions</div>`
    + kv([['Status',d.tier||'undecided',d.tier?null:'was'],
          ...eventRow])
    + (tags?`<div style="margin-top:6px">${tags}</div>`
          :'<div class="dim" style="margin-top:4px">no tags</div>')
    + (folk?`<div class="rail-h">People</div><div>${folk}</div>`:'')
    + (d.has_sidecar?'':'<div class="dim" style="margin-top:6px">'
        +'no sidecar &mdash; nothing decided yet</div>')
    + `<div class="rail-h">Date</div>` + kv(dateRows)
    + `<div class="rail-h">File</div>`
    + kv([['Name',d.name],['Folder',d.folder],['Size',bytes(d.size)],
          ['Kind',d.kind],['Band',d.band],
          ['Pixels',d.width&&d.height?`${d.width} × ${d.height}`:null],
          ['Length',secs(d.duration)],
          ['Render',d.kind==='video'?(d.has_render?'yes':'no'):null]])
    + (d.facts&&d.facts.length
        ? `<div class="rail-h">Capture</div>`
          + kv(d.facts.map(f=>[f.label,f.value])) : '')
    + (all?`<details><summary>All metadata`
           +` (${Object.keys(d.exif).length})</summary>`
           +`<dl class="kv">${all}</dl></details>`:'');
}

// Looking is not choosing. Opening the viewer used to reset the selection to
// the single photograph you opened, so one mis-aimed click on a thumbnail
// threw away a selection that had taken hundreds of gestures to build, with
// nothing that could bring it back.
//
// So the viewer leaves a selection alone: it is a bigger look at the cursor,
// not a place that decides anything. With nothing selected it still selects
// what you opened, because otherwise the actions would have nothing to apply
// to and the count would sit at none while you looked straight at the file
// you meant.
//
// Looking changes nothing at all. The viewer used to select what you opened
// when nothing was selected — so that the actions had something to apply to
// while you were in there — and leave the selection alone otherwise. That was
// two behaviours for one gesture and it showed: the first photograph you
// opened got ticked and the next one did not.
//
// It bought nothing any more. The rule existed for `S`, which wrote to the
// photograph on screen, and the viewer has had no controls of its own since
// the keyboard went. So opening and paging are pure looking, and the only way
// to choose a file is still to click its circle.
function openViewer(n){
  viewer.classList.add('on');
  setCur(n===undefined?(cur<0?0:cur):n, true);
}
function closeViewer(){
  viewer.classList.remove('on'); vvid.pause();
  if(vActing){vActing=null; closeMenu();}
}
// The stage fills the viewer, so clicking beside the picture lands on it
// rather than on the viewer itself — the old check never matched and there
// was no way back out except the keyboard.
if(viewer) viewer.addEventListener('click',e=>{
  if(e.target===viewer||e.target===stage||e.target===vmeta) closeViewer();
});
if(rail) rail.addEventListener('click',e=>{
  e.stopPropagation();
  // A clip's source, when it is on this page already, is one step of the
  // viewer away rather than a page load.
  const a=e.target&&e.target.closest?e.target.closest('a.to-source'):null;
  if(!a) return;
  const n=cells.findIndex(x=>keyOf(x)===a.dataset.key);
  if(n<0) return;
  e.preventDefault();
  setCur(n,true);
});
// A page restored from the back/forward cache comes back exactly as it left,
// an open viewer included. Leaving a grid with one open is ordinary, and
// arriving back at that grid to find a photograph over it reads as the app
// having opened something on its own. Belt to the braces above: that stops
// the viewer opening on the way out, this closes it whatever opened it.
// A page restored from the back/forward cache comes back exactly as it left it,
// and what it left open it should not be holding open. The takeover is the one
// that matters: leaving for the share sheet and coming back can strand it over
// the whole screen with nothing that dismisses it, because Stop only acts while
// a write is running and by then none is.
window.addEventListener('pageshow',e=>{
  if(!e.persisted) return;
  if(viewer) closeViewer();
  closeMenu();
  busy=false; stopping=false; workClose();
});

// Android has no `-webkit-touch-callout`, so the stylesheet rule that closes
// this on iOS closes nothing there: long-pressing a thumbnail opens Chrome's
// own image menu, and its *Save image* saves the four-hundred-pixel
// derivative — or, in the viewer, the sixteen-hundred-pixel preview. Somebody
// walks off believing they have the photograph either way, and Save is how you
// get the photograph.
//
// Refusing the menu is the only lever there is, and it is taken only where a
// finger is doing the pressing: *Save image as* on a right-click is an
// ordinary thing to want at a desk, and nothing about it is wrong there.
if(COARSE){
  const noMenu=e=>e.preventDefault();
  if(grid&&typeof grid.addEventListener==='function')
    grid.addEventListener('contextmenu',noMenu);
  if(stage&&typeof stage.addEventListener==='function')
    stage.addEventListener('contextmenu',noMenu);
}

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

// --- writing -----------------------------------------------------------------
// Loud, because the alternative has bitten twice: a write that fails without
// saying so is indistinguishable from one that worked, and the curator only
// finds out much later that nothing was recorded.
function say(text,bad){
  if(!note) return;
  note.textContent=text||'';
  note.hidden=!text;
  note.classList.toggle('loud',!!bad);
}

// A page script that throws takes every handler with it and leaves a grid that
// simply ignores clicks. Saying so beats looking broken.
window.addEventListener('error',e=>say('page error: '+e.message,true));
window.addEventListener('unhandledrejection',
  e=>say('page error: '+(e.reason&&e.reason.message||e.reason),true));

// Exactly what is ticked — no implicit extra. A count that says none while
// an action changes something is the one thing a selection must never do.
// What an action acts on: the selection — or, while an action was asked for
// from the viewer, the one photograph on show. One place, so every action,
// its menu and its suggestions agree about which files they mean.
let vActing=null;
function targets(){
  if(vActing) return [...vActing];
  return [...picked];
}

// --- the landing page selects folders ----------------------------------------
// One zoom out, the same gesture. A folder *is* a set of filters — its own
// link says which — so a decision about a folder is a decision about every
// file that link would open, and the way to make one is to ask for those files
// and then do exactly what the grid does.
//
// Which is why nothing below writes anything. It turns folders into the same
// shape a thumbnail has, and `applyToSelection` takes it from there: the
// cascade into stacks, the field the person is allowed to write, the chunking,
// the log and the revert are all the ones that were already there.
const FOLDERS = PAGE==='/';
// Which filter each kind of chip narrows by, kept in step with the server's
// own map by the test that renders a card and presses one.
const SPREAD_FILTER={audience:'audience', people:'person', tags:'tag'};
const SPREAD_LABEL={audience:'Access', people:'People', tags:'Tag'};

// An event and its sub-event are one name at two widths — *Sicily* and
// *Sicily > Taormina* — so splitting and rejoining it is something the page
// does as often as the server, and in exactly the same two places.
function splitEvent(v){
  const at=String(v||'').indexOf(EVENT_SEP);
  return at<0?[v||'',null]:[v.slice(0,at),v.slice(at+EVENT_SEP.length)||null];
}
function joinEvent(head,leaf){
  return !head?'':(leaf?head+EVENT_SEP+leaf:head);
}
const tiles = FOLDERS && grid
  ? [...grid.querySelectorAll('.tile')].filter(t=>t.getAttribute('href')) : [];
const pickedFolders = new Set();
// Which files sit behind each chosen folder. Kept per folder rather than in
// one heap, because a card is redrawn from its own — a percentage of
// everything selected is a percentage of the wrong thing.
const expandedBy = new Map();
// The files behind the chosen folders, once anything has asked. Thrown away
// whenever the selection changes, because it is an answer about that
// selection and nothing else.
let expanded = null;

// A fetched row, wearing enough of a thumbnail to be one. `applyToSelection`
// reads `dataset` and paints what it just wrote back onto the cell; there is
// no cell here, so the paint lands on nothing and the write is unaffected.
function ghost(row){
  return {
    dataset:{folder:row.folder, name:row.name,
             tags:row.tags||'', audience:row.audience||'',
             people:row.people||'',
             event:row.event||'', deleted:row.deleted?'1':''},
    classList:{add(){}, remove(){}, toggle(){}, contains(){return false;}},
    querySelector(){return null;}, appendChild(){}, remove(){},
  };
}

// Every file the chosen folders hold. Paged, because a folder is not bounded
// by what fits on a screen the way a selection of thumbnails is — an event
// can be thousands — and the takeover says so while it reads.
const PAGE_SIZE = 2000;
async function readFolders(){
  if(expanded) return expanded;
  const chosen=[...pickedFolders];
  if(!chosen.length) return [];
  if(busy){say('still writing…');return null;}
  busy=true;
  workOpen(chosen.length===1?'Reading the folder'
                            :`Reading ${chosen.length} folders`, chosen.length);
  const out=[], seen=new Set();
  try{
    for(let i=0;i<chosen.length;i++){
      if(stopping) break;
      const from=out.length;
      const q=new URLSearchParams(
        (chosen[i].getAttribute('href').split('?')[1])||'');
      // How the library is cut up says nothing about which files are in it,
      // and asking for a grouping the file listing does not use would only
      // give the server something to ignore.
      q.delete('group');
      for(let offset=0;;){
        q.set('limit',String(PAGE_SIZE)); q.set('offset',String(offset));
        const r=await fetch('/api/files?'+q);
        if(!r.ok) throw new Error((await r.text()).slice(0,200));
        const rows=await r.json();
        for(const row of rows){
          const key=row.folder+'\n'+row.name;
          // Two folders can only overlap if the grouping lets them, but a
          // file written twice in one gesture is written twice in the log.
          if(seen.has(key)) continue;
          seen.add(key); out.push(ghost(row));
        }
        if(rows.length<PAGE_SIZE) break;
        offset+=rows.length;
      }
      expandedBy.set(chosen[i],out.slice(from));
      workProgress(i+1,chosen.length,'folder','folders');
    }
  }catch(e){
    busy=false; workClose();
    say('could not read that folder: '+e.message,true);
    return null;
  }
  const halted=stopping;
  busy=false;
  if(halted){workClose();say('stopped — nothing was written');return null;}
  // **Left open on purpose.** Reading is the first half of one gesture and
  // the write is the second; closing here and opening again in `send` puts a
  // gap of at least the takeover's own delay between them, so the screen
  // blinks empty in the middle of a job that never stopped. Whoever asked
  // closes it if they turn out not to write — there are three of them and
  // they are all in this file.
  expanded=out;
  return out;
}

// What an action applies to, whichever page is asking.
async function acting(){
  return FOLDERS ? await readFolders() : targets();
}

// A card is redrawn from its own files, which is where the answer already is:
// `applyToSelection` writes what it wrote onto each of them, so counting is
// all that is left. Nothing is fetched and nothing reloads — the menu stays
// open and the card underneath it changes, which is the whole point of a
// checklist you tick more than once.
function redrawFolder(t){
  const gs=expandedBy.get(t);
  if(!gs||!gs.length) return;
  const n=gs.length;
  const kinds=ADMIN?['audience','people','tags']:['people','tags'];
  // The same chip the server draws, filter and all: without `data-col` a
  // redrawn card would look the same and do nothing when pressed.
  // The same title the server writes: what kind of thing this is, and how
  // many of them — never the value, which is the word being pointed at.
  const chip=(kind,v,c,cls,on)=>`<i class="${cls||kind}"`
    +` data-col="${SPREAD_FILTER[kind]}" data-val="${esc(on||v)}"`
    +` title="${cls==='none'?'No access yet':SPREAD_LABEL[kind]}`
    +` — ${c} file${c===1?'':'s'}">${esc(v)}</i>`;
  // One run for all of them, in the order the server writes them: the colour
  // is on each chip, so the kinds stay legible without a break between them.
  let html='';
  for(const kind of kinds){
    const counts=new Map();
    for(const g of gs)
      for(const v of valuesOf(g,kind)) counts.set(v,(counts.get(v)||0)+1);
    // Commonest first, so what the whole folder carries leads and the partial
    // ones follow — the same order the server draws them in.
    const sorted=[...counts].sort((a,b)=>b[1]-a[1]||(a[0]<b[0]?-1:1));
    if(kind==='audience'){
      const none=gs.filter(g=>!g.dataset.audience).length;
      if(none) html+=chip('audience','undecided',none,'none',UNREVIEWED);
    }
    html+=sorted.map(([v,c])=>chip(kind,v,c)).join('');
  }
  let el=t.querySelector('.spread');
  if(!html){ if(el) el.remove(); return; }
  if(!el){
    el=document.createElement('span');
    el.className='spread';
    t.appendChild(el);
  }
  el.innerHTML=html;
}
function redrawFolders(){ pickedFolders.forEach(redrawFolder); }

function drawFolderSel(){
  tiles.forEach(t=>t.classList.toggle('picked',pickedFolders.has(t)));
  if(!actions) return;
  const n=pickedFolders.size;
  if(!n&&menuCtx&&(menuCtx.mode==='set'||menuCtx.mode==='date')) closeMenu();
  for(const g of actions.querySelectorAll('.grp')) g.hidden=!n;
  actions.dataset.state = !n ? 'none' : n===tiles.length ? 'all' : 'some';
  if(selcount) selcount.textContent = `${n} selected`;
}

// A chip on a card opens the folder narrowed to itself: the card's own link
// plus the one filter the chip names. Inside that link, so like the select
// circle it has to say it is not it.
tiles.forEach(t=>{
  // One listener on the card rather than one per chip: a write redraws the
  // chips from the files it just changed, and handlers hung on the old ones
  // go into the bin with them — so the chips would open the folder until
  // the first edit and then stop, which is the kind of thing nobody reports
  // because it looks like they never worked.
  t.addEventListener('click',e=>{
    const chip=e.target&&e.target.closest&&e.target.closest('[data-col]');
    if(!chip) return;
    e.preventDefault(); e.stopPropagation();
    const [path,query]=(t.getAttribute('href')||'').split('?');
    const q=new URLSearchParams(query||'');
    q.set(chip.dataset.col,chip.dataset.val);
    location.href=path+'?'+q;
  });
  const pick=t.querySelector('.pick');
  if(!pick) return;
  pick.addEventListener('click',e=>{
    // Inside the link that opens the folder, so it has to say it is not that.
    e.preventDefault(); e.stopPropagation();
    if(pickedFolders.has(t)) pickedFolders.delete(t); else pickedFolders.add(t);
    expanded=null; expandedBy.clear();
    drawFolderSel();
  });
});
if(FOLDERS) drawFolderSel();

// Which kind of file an action is about. A deleted file cannot be deleted
// again and a living one cannot be restored, so every action has a side and
// acts only on that side of the selection. Select a day that holds both and
// Delete takes the living ones while Purge takes the deleted ones — each does
// what it says to the files it means, rather than refusing the whole gesture
// because the selection was not pure.
const ACT_SIDE={access:'live', tags:'live', event:'live', date:'live',
                delete:'live', stack:'live', top:'live', unstack:'live',
                restore:'gone', purge:'gone'};
function gone(c){ return !!c.dataset.deleted; }
function sideOf(act,value){
  // One flag, two buttons: deleting is something you do to a living file and
  // restoring to a deleted one, so the field name alone cannot say which.
  if(act==='deleted') return value?'live':'gone';
  return ACT_SIDE[act]||'live';
}

// --- stacks ------------------------------------------------------------------
// Eight takes of one photograph, one shown and the rest folded behind it. Each
// of the others records which file it defers to; the top records nothing,
// because being spoken for is the decision and speaking is what is left.
function keyOf(c){ return c.dataset.folder+'/'+c.dataset.name; }
// **Decided**, and only decided. Taking a file out of a stack is undoing a
// decision, and there is no decision to undo about a guess — refusing is what
// a guess answers to.
function stacked(c){ return !!c.dataset.under; }
// In a stack at all, whoever put it there. Which of these photographs should
// be the one that shows is the same question either way, and answering it is
// what turns a guess into a decision.
function inStack(c){ return !!(c.dataset.under||c.dataset.proposedUnder); }
// Anything folded behind this one, however it got there. A guessed stack opens
// like a decided one — the question *which of these do I keep* is the same
// question, and the answer to it is what turns one into the other.
function tops(c){ return +(c.dataset.behind||0)+ +(c.dataset.proposed||0) > 0; }
function guessed(c){ return +(c.dataset.proposed||0) > 0; }
// In a guessed group, speaking for it or standing in it. *This one does not
// belong* is a thing to be able to say about any of them, and it was only
// askable of the one the shelf is drawn on — so opening a guess and pointing
// at the photograph that does not fit left nothing to press.
function inGuess(c){ return guessed(c) || !!c.dataset.proposedUnder; }
// Whether the stack this file belongs to is already open on the page — inside
// `?within=…`, or grouped by stack. The server reads it the same way, and the
// two have to agree: what it means is *the members are in front of the
// curator*, which decides both whether a write has to follow them and whether
// the page has anything left to fetch.
// Whether the members of a stack are on the page rather than folded away.
// One stack has a page of its own; the shelf is the grid grouped by stack,
// which opens every one of them in the view at once.
const OPENED = !!STACK || GROUPING.includes('stack');
// Whether the rest of this file's stack is on the page with it. Naming a top
// writes to the others, so the gesture can only be offered where the others
// are — which is every open view, and is never the folded grid, where not
// having them on screen is the whole point of a stack.
function familyOn(c){
  const key=stackKey(c);
  return !!key&&cells.some(o=>o!==c&&stackKey(o)===key);
}

// Stacking asks which one to show, rather than taking the first ticked and
// hoping. The rule was invisible: nothing on screen said that the order you
// happened to click in had decided which photograph spoke for the rest.
//
// So the grid narrows to the files being stacked and waits. No round trip and
// no address of its own — they are already on screen, so *filter to these* is
// hiding the others and *back to where you were* is showing them again, with
// the scroll never having moved.
let choosing=null, fetched=[], opened=[], wasPicked=[], wasScrolled=0;
// The pills saying which files were speaking. Borrowed for the question and
// taken back with everything else it borrowed.
let wereTops=[];
let choiceBtns=[];

// Merging two stacks has to offer every photograph in both of them as the one
// to show. Choosing between the two that happen to be speaking is choosing
// between two of ten, and the other eight are only hidden because stacking
// them is what hid them.
// A stack says *these are the same shot*, so it cannot hold two kinds of
// thing: a photograph and a clip are not the same shot whatever else they
// share, and neither can speak for the other. The server refuses it too —
// this is so that nobody is asked which of them to show first.
function mixed(cs){
  return new Set(cs.map(c=>c.dataset.kind||'')).size>1;
}
const MIXED='a stack is one shot — photographs and video cannot be stacked '
           +'together';
// Video does not stack at all, for now (spec/clips.md §4): the server refuses
// it, and the bar does not offer it.
function hasVideo(cs){
  return cs.some(c=>c.dataset.kind==='video');
}
const NO_VIDEO='video cannot be stacked yet';

async function stackSelection(){
  const cs=targetsOn('live');
  // One is enough when that one is a stack, or a guess at one: the question
  // is *which of these shows*, and the photograph a group is drawn as is how
  // you point at the group. The bar has offered this on a single top for as
  // long as `tops` has been in the rule that shows it, and the handler asked
  // for two and said so — a button whose whole answer was that it should not
  // have been pressed, standing on exactly the suggestion somebody was trying
  // to accept.
  if(!cs.length||(cs.length<2&&!tops(cs[0]))){
    say('select the ones to stack');return;}
  if(mixed(cs)){say(MIXED,true);return;}
  if(hasVideo(cs)){say(NO_VIDEO,true);return;}
  choosing=cs; fetched=[]; opened=[]; wasPicked=cs.slice();
  // Where you were, because it is about to be taken from you. Hiding the rest
  // of the grid collapses the page to a few rows, and a browser will not hold
  // a scroll position past the bottom of a document — so it clamps to the top,
  // and putting the cells back afterwards does not put you back with them.
  wasScrolled=window.scrollY;
  const keep=new Set(cs);
  cells.forEach(c=>{c.hidden=!keep.has(c);});
  if(grid) grid.dataset.choosing='1';
  choiceBtns=[];
  // Not `forEach(offerChoice)`: `forEach` hands its callback the index and the
  // array as well, so the second argument became the label and the third the
  // *standing* flag — every candidate's button quietly left off the list
  // `endChoosing` clears, and a refused press left the chooser's controls
  // scattered over the grid behind it.
  cs.forEach(c=>offerChoice(c));
  document.querySelectorAll('.group').forEach(h=>{h.hidden=true;});
  // Nothing is selected while a top is being chosen. The question is *which
  // one of these*, and leaving the files you arrived with ringed while the
  // ones fetched out of a stack are not says they are two kinds of candidate.
  // They are not: any of them can be the one that shows.
  say(''); clearPicks();
  // An open stack brings its members with it rather than fetching them: they
  // are on the page already, and `/api/behind` answers about the stack rather
  // than about the view, so asking put a second copy of every one of them
  // into the grid — the same photograph offered twice as the one to show.
  if(OPENED){
    const keys=new Set(cs.map(stackKey).filter(Boolean));
    for(const c of cells){
      if(keep.has(c)||!keys.has(stackKey(c))) continue;
      c.hidden=false; choosing.push(c); offerChoice(c);
    }
  } else {
    for(const head of cs.filter(c=>tops(c))) await expand(head);
  }
}

async function expand(head){
  let html='';
  try{
    const r=await fetch('/api/behind/'+encodeURIComponent(head.dataset.folder)
                        +'/'+encodeURIComponent(head.dataset.name));
    if(!r.ok) throw new Error(await r.text());
    html=(await r.json()).cells||'';
  }catch(e){say('could not open that stack: '+e.message,true);return;}
  if(!choosing||!html) return;
  // The depth badge means *there are more of these, somewhere else*. Once they
  // are sitting beside it that is no longer true, and leaving it there says
  // the stack is still closed while its files are on screen being chosen
  // between.
  const badge=head.querySelector('.stack');
  if(badge){badge.hidden=true; opened.push(badge);}
  // In the badge's place, what it was really saying. Re-stacking something
  // already stacked asks the question again over every file in it, and this
  // file is the answer that was given last time — worth keeping on screen,
  // because merging three stacks puts three of them among thirty photographs
  // that look alike, and the one to keep is usually one of the three.
  //
  // The same pill an opened stack uses for the same fact, rather than a
  // second way of saying *this is the one that shows*.
  const was=document.createElement('span');
  was.className='top-mark';
  was.title='The one this stack has been showing';
  was.textContent='Top';
  head.appendChild(was);
  wereTops.push(was);
  const holder=document.createElement('div');
  holder.innerHTML=html;
  const added=[...holder.children];
  let after=head;
  for(const c of added){
    after.insertAdjacentElement('afterend',c);
    after=c;
    cells.push(c); choosing.push(c); fetched.push(c); wire(c); useSource(c);
  offerChoice(c);
  }
}

// Clicking a photograph opens it, here as everywhere else. It used to mean
// *this one* while a top was being chosen, and the cost of that was finding
// out by having chosen: you click one to see it properly, and instead the
// question is answered and the grid comes back.
//
// So choosing has a control of its own, on the photograph, appearing when the
// pointer is over it. There is one of them per candidate and one candidate per
// click, which is why it can be a button rather than a mode.
function offerChoice(c,word,standing){
  // Idempotent, because these arrive two ways now: put on every photograph in
  // an opened guess when the page loads, and put on the candidates when a top
  // is being chosen. Pressing Stack inside a guess is both at once, and a
  // second button under the first is two answers to one question.
  if(c.querySelector('.choose')) return;
  const b=document.createElement('button');
  b.className='choose';
  b.textContent=word||'Show this one';
  b.onclick=e=>{e.stopPropagation();chooseTop(c);};
  c.appendChild(b);
  // A standing one is not part of a session and must not be cleared with it:
  // `endChoosing` takes away everything it put up, and that list is how it
  // knows what it put up.
  if(!standing) choiceBtns.push(b);
}

// A guess, refused. Remembered against every photograph in it, so the same
// group is not offered again tomorrow, and recorded like any other decision —
// which is the way back when a shelf of them is waved off by mistake.
//
// What it was hiding comes back out onto the page. In a view of nothing but
// guesses there is nothing to come back to: the whole section stops matching
// and leaves, which the server already reports. In the mixed view the files
// are still here and still match, and leaving them off the grid until the next
// reload would be the page quietly holding some of the library back.
// Refusing the whole group and refusing one photograph of it are the same
// write: `no_stack` is a fact about a file, and the server carries it to the
// members of anything the file speaks for. Said of the one that speaks it
// takes the group with it; said of one standing in it, that one leaves and
// the rest are still a guess.
// A stack of one is not a stack, so once a page is down to that there is
// nothing left in it to ask about. Refusing the whole group empties it,
// refusing all but one leaves nothing to compare, and taking the top out of a
// decided stack dissolves it — three ways to the same place, which is out.
function stillAStack(){
  if(STACK&&cells.length<2) leaveStack(STACK);
}

async function notAStack(){
  let cs=targetsOn('live').filter(inGuess);
  if(!cs.length){say('nothing selected that the app guessed at');return;}
  // **On a stack's page, refusing the one that speaks names the rest.**
  //
  // The server deliberately does not follow the members of an opened stack,
  // on the grounds that they are in front of the curator and following them
  // would be a second write to a file they can see they picked. They are in
  // front of them; they were not necessarily *picked*. Said of the photograph
  // the group is drawn as, the answer is about the group — and left to
  // itself the rest would re-form behind a new leader and be offered again
  // tomorrow, which is the one thing refusing a guess is supposed to stop.
  if(STACK&&cs.some(guessed)) cs=cells.filter(inGuess);
  // Only in the ordinary view. Where the view is *only stacks* or *only
  // suggested*, refusing takes the whole thing out of it — the server says so
  // and the grid drops it — so there is nothing to fan back out into.
  // Only what was hiding something has anything to fan back out.
  //
  // And never on a stack's page, where nothing is hidden: the photographs a
  // refusal releases are the ones already standing on it, and fetching them
  // put a second copy of every one of them into the grid — refuse three and
  // watch five arrive.
  const fan=(!STACK&&!VIEW.stacks)
    ? new Map(await Promise.all(
        cs.filter(guessed).map(async c=>[c,await behind(c)])))
    : null;
  const out=await applyToSelection('no_stack',true,undefined,cs);
  if(!out||!out.done) return;
  if(fan) cs.forEach(c=>fanOut(c,fan.get(c)));
  stillAStack();
}

// Fetched before the refusal is written: afterwards they are nothing's
// members, and the page would have no way left to ask what it had been hiding.
async function behind(head){
  try{
    const r=await fetch('/api/behind/'+encodeURIComponent(head.dataset.folder)
                        +'/'+encodeURIComponent(head.dataset.name));
    if(!r.ok) throw new Error(await r.text());
    return (await r.json()).cells||'';
  }catch(e){say('could not open that stack: '+e.message,true);return '';}
}

function fanOut(head,html){
  if(!html) return;
  const holder=document.createElement('div');
  holder.innerHTML=html;
  const added=[...holder.children];
  let after=head;
  for(const c of added){after.insertAdjacentElement('afterend',c);after=c;}
  // Spliced where they sit rather than appended: `cells` is the reading order
  // the viewer and the arrow keys walk, and a file that is on screen here and
  // last in the order is a preview that opens the wrong photograph.
  const at=cells.indexOf(head);
  cells.splice(at<0?cells.length:at+1,0,...added);
  added.forEach(c=>{wire(c);useSource(c);});
  // In the view now, and in the server's order ahead of the next page.
  served+=added.length; total+=added.length;
  const badge=head.querySelector('.stack');
  if(badge) badge.remove();
  head.dataset.proposed='0';
  head.classList.remove('marked');
  resection(); drawSel();
}

// --- downloading -------------------------------------------------------------
// Two things can be meant by *the file*: what came off the camera, and the
// H.264 copy the app made so a browser can play it. They differ only for the
// clips a browser will not play as they are — so the choice is offered only
// when the selection holds one, and the rest of the time pressing Download
// downloads.
// *Download* names one of the two places a phone can put a file, and not the
// one this app is for. The sheet the button opens offers Photos and Files
// both, so the word has to cover both.
if(COARSE&&CAN_SHARE&&actions){
  const b=actions.querySelector('[data-act="download"] .word')
        ||actions.querySelector('[data-act="download"]');
  if(b) b.textContent='Save';
}

async function downloadMenu(anchorEl){
  const cs=await acting();
  if(!cs) return;
  // Closed either way: a download is the browser's job from here, and on a
  // desktop it shows nothing of its own to take the takeover's place.
  workClose();
  if(!cs.length){say('nothing selected');return;}
  if(!cs.some(c=>c.dataset.copy)){closeMenu();getFiles(cs,false);return;}
  const key='download';
  if(menuCtx&&menuCtx.key===key&&!menu.hidden){closeMenu();return;}
  menu.innerHTML='<div id="menulist"></div>';
  const list=menu.querySelector('#menulist');
  const head=document.createElement('div');
  head.className='band';
  head.textContent='Download';
  list.appendChild(head);
  for(const [label,orig] of [['Playable copies',false],['Originals',true]]){
    const d=document.createElement('div');
    d.className='opt';
    d.innerHTML=`<span>${esc(label)}</span>`;
    d.onclick=e=>{e.stopPropagation();closeMenu();getFiles(cs,orig);};
    list.appendChild(d);
  }
  placeMenu(anchorEl);
  menuCtx={key};
}

function fileUrl(c,original){
  return '/download/'+encodeURIComponent(c.dataset.folder)
        +'/'+encodeURIComponent(c.dataset.name)+(original?'?original=1':'');
}

// What the system will call this, worked out here rather than read off the
// response. The fallback paths never see a header, and the share sheet will
// only offer *Save Image* for something it has been told is an image — so the
// one place that must agree about a file's type is the client.
const MIME={jpg:'image/jpeg',jpeg:'image/jpeg',png:'image/png',
            gif:'image/gif',webp:'image/webp',heic:'image/heic',
            heif:'image/heif',avif:'image/avif',tif:'image/tiff',
            tiff:'image/tiff',dng:'image/x-adobe-dng',
            mp4:'video/mp4',m4v:'video/x-m4v',mov:'video/quicktime',
            avi:'video/x-msvideo',mkv:'video/x-matroska',webm:'video/webm',
            mts:'video/mp2t',m2ts:'video/mp2t','3gp':'video/3gpp'};
function mimeOf(name){
  const at=String(name).lastIndexOf('.');
  return (at<0?'':MIME[name.slice(at+1).toLowerCase()])||'application/octet-stream';
}

// Sharing holds every byte in memory — the fetched blob, the File made from
// it, and the sheet's own copy of the same bytes, in a process the phone will
// kill around a gigabyte. So there is a ceiling, and going over it is not a
// failure: it is the ordinary download, said plainly.
const SHARE_MAX_FILES=10;
const SHARE_MAX_BYTES=150*1024*1024;

// One file is a link; a selection is a posted form. Not a fetch either way:
// the browser has to own the transfer, or every byte of a selection of video
// is held in this page's memory before a file appears anywhere.
//
// Except on a phone, where the browser owning it means the file goes to Files
// if it goes anywhere at all, and the library's whole point is the photographs
// being *in Photos*. There, the bytes come here and go out through the share
// sheet — see `saveFiles`.
function getFiles(cs,original){
  if(COARSE&&CAN_SHARE&&cs.length<=SHARE_MAX_FILES){
    saveFiles(cs,original);
    return;
  }
  downloadFiles(cs,original);
}

function downloadFiles(cs,original){
  if(cs.length===1){
    location.href=fileUrl(cs[0],original);
    say('downloading 1 file');
    return;
  }
  const form=document.createElement('form');
  form.method='post';
  form.action='/download.zip';
  const files=document.createElement('input');
  files.type='hidden'; files.name='files';
  files.value=JSON.stringify(cs.map(
    c=>({folder:c.dataset.folder,name:c.dataset.name})));
  form.appendChild(files);
  if(original){
    const flag=document.createElement('input');
    flag.type='hidden'; flag.name='original'; flag.value='1';
    form.appendChild(flag);
  }
  document.body.appendChild(form);
  form.submit();
  form.remove();
  say(`downloading ${cs.length.toLocaleString()} files as a zip`);
}

// Down here, then out through the share sheet — which on a phone is where
// *Save to Photos* lives, and the only place it lives.
//
// **Two taps, always.** The sheet may only be opened by a gesture, and the
// gesture that started this was spent somewhere inside the fetch: a browser
// keeps the permission alive across a promise for about a second, so sharing
// straight after the await works for a photograph on wifi and fails for a
// forty-megabyte clip on a VPN. That is one button behaving two ways depending
// on the file and the network, and the failure is silent. So the fetch is one
// press and the sheet is another, every time, and the second one is a button
// that is not there until the bytes are.
let ready=null;
function saveFiles(cs,original){
  if(busy) return;
  busy=true; ready=null; say('');
  workOpen(cs.length===1?'Fetching':'Fetching '+cs.length+' files',cs.length);
  if(workSave) workSave.hidden=true;
  const files=[];
  let bytes=0;
  (async()=>{
    for(let i=0;i<cs.length;i++){
      if(stopping) break;
      const c=cs[i];
      const r=await fetch(fileUrl(c,original),{credentials:'same-origin'});
      if(!r.ok) throw new Error(c.dataset.name+': '+r.status);
      // Read before buffering. The header is there on every one of these —
      // they are files on a disk — so a selection that is too big to hold can
      // be turned down without first holding it.
      bytes+=+(r.headers&&r.headers.get&&r.headers.get('content-length'))||0;
      if(bytes>SHARE_MAX_BYTES) throw new Error('too big');
      files.push(new File([await r.blob()],c.dataset.name,
                          {type:mimeOf(c.dataset.name)}));
      workProgress(i+1,cs.length);
    }
    if(stopping){busy=false;workClose();return;}
    // The sheet decides what it will take, and it is the authority: a `.insv`
    // or a raw file is something no phone has an opinion about.
    if(!navigator.canShare({files:files})) throw new Error('not shareable');
    ready=files;
    if(workWhat) workWhat.innerHTML=files.length===1?'Ready to save'
      :'Ready to save '+files.length+' files';
    if(workTally) workTally.textContent=
      'Photos, Files, or anywhere else you send it.';
    if(workSave){workSave.hidden=false;}
  })().catch(err=>{
    // Whatever the reason — too big to hold, a type the sheet will not take,
    // a fetch that failed — the ordinary download is still there. Saying where
    // the file is going to land instead is the difference between a fallback
    // and a button that did something else without mentioning it.
    busy=false; workClose();
    downloadFiles(cs,original);
    // After, not before: `downloadFiles` says *downloading 1 file*, which is
    // true and is not the part worth reading.
    say(err&&err.message==='too big'
        ? 'too large to hand to Photos — downloading to Files instead'
        : 'Photos will not take this one — downloading to Files instead',true);
  });
}

// The second press. Synchronous from the tap: nothing is awaited between the
// gesture and the sheet, which is the whole reason the fetch was a separate
// press.
function shareReady(){
  if(!ready) return;
  const files=ready;
  navigator.share({files:files}).then(()=>{
    say(files.length===1?'saved 1 file':`saved ${files.length} files`);
  },err=>{
    // Closing the sheet is an answer, not a fault.
    if(!err||err.name!=='AbortError') say('could not save: '+(err&&err.message),true);
  });
  ready=null; busy=false; workClose();
}

function endChoosing(restore){
  if(!choosing) return;
  choosing=null;
  choiceBtns.forEach(b=>b.remove());
  choiceBtns=[];
  if(grid) delete grid.dataset.choosing;
  // Whatever was fetched belongs to a stack, and a stack's files do not sit in
  // the grid — that is the whole point of one. They were borrowed to be
  // chosen between.
  fetched.forEach(c=>{picked.delete(c); c.remove();});
  cells=cells.filter(c=>!fetched.includes(c));
  fetched=[];
  // Closed again, so the badge means what it says again — and says it in the
  // place the pill was standing in.
  opened.forEach(b=>{b.hidden=false;});
  opened=[];
  wereTops.forEach(m=>m.remove());
  wereTops=[];
  cells.forEach(c=>{c.hidden=false;});
  document.querySelectorAll('.group').forEach(h=>{h.hidden=false;});
  // The page is tall again, so the position it was holding means something
  // again. Whatever leaves the grid next is anchored from here.
  window.scrollTo(0,wasScrolled);
  // Changing your mind puts back what you had, not an empty grid: the files
  // were selected before this asked anything, and cancelling asked for none
  // of it to have happened.
  if(restore) wasPicked.forEach(c=>{
    const n=cells.indexOf(c);
    if(n>=0) togglePick(n,true);
  });
  wasPicked=[];
  drawSel();
}

// Out of the opened stack and back to the grid it was folded into, asked for
// afresh.
//
// **Not `history.back()`**, which is how you leave a stack you were only
// looking at — and is right there, because the same address reached again is
// the same photographs at the top of the page rather than where you were
// standing. It is wrong the moment something has been written: the page behind
// is the one that has just stopped being true, and whether a browser hands
// back a live copy or the one it cached is a question with a different answer
// in each of the three this runs in. Going back and then reloading is exactly
// what somebody had to do by hand, twice, for every suggestion they agreed
// with.
// **Standing on the photograph, not at the top of the page.** Every other
// stack gesture keeps your place because none of them navigates — the grid
// loses a few cells and redraws a badge, and you are still looking at what you
// were looking at. This one has to fetch, and a fetched page starts at the
// top, three thousand pixels above a review pass somebody was part way
// through.
//
// So it says which photograph to land on, in the fragment, and the page
// scrolls to it. The fragment rather than a filter because it is not one:
// *where you are in a page* is the one thing a `#` has always meant, and a
// view is still the same view with or without it. A pixel offset would do
// here — a guess already folds to one cell, so agreeing with it changes no
// layout — but the photograph is the thing that was actually meant, and it
// survives a different thumbnail size or a window that changed width on the
// way.
function leaveStack(key){
  // Where it was opened from, carried in rather than guessed at. The browser
  // would do it on the way out and cannot do it on the way in: a stack
  // reached from a bookmark, a shared link or the operation log has nothing
  // behind it. `BACK` is also what survives a write, which `history.back()`
  // does not — the page behind is the one that has just stopped being true.
  location.href=(BACK||'/')+'#'+encodeURIComponent(key||'');
}
// The same stack under a new name. Promoting renames it — a stack is named by
// the file that speaks for it — and the way out has to come along.
function stackUrl(key){
  // Where it is being opened from: the grid this page is, or — on a stack's
  // page already, which is where promoting one lands — the grid that one came
  // from. The way out has to survive being handed along.
  const back=BACK||url({});
  const at=key.indexOf('/');
  return '/stack/'+encodeURIComponent(key.slice(0,at))
        +'/'+encodeURIComponent(key.slice(at+1))
        +(back?'?back='+encodeURIComponent(back):'');
}

// Where the page goes once one photograph has been made the one that shows.
//
// **One rule, because there are two ways in.** The chooser and the bar both
// end in the same write, and each had its own copy of this — one navigated
// only when the stack had been renamed, the other whenever it was open at
// all. The same rule written twice, already drifting, which is how the two
// gestures came to need thinking about separately when they are one decision.
//
// A guess agreed with is the question the opened stack was asking, answered:
// there is nothing left in here to look at, and what changed is out in the
// grid it was folded into, which is still drawing it as a suggestion.
//
// Rearranging a stack somebody already made is not a question being answered
// — it is one edit among several you may want to go on making — so it stays,
// under the name the stack now has. A stack is named by the file that speaks
// for it, so promoting one renames it, and an open stack's address is that
// name: stay on the old one and the page asks for a stack whose files have
// all just gone somewhere else.
//
// **Nothing at all outside an opened stack**, which is the ordinary grid —
// where stacking has always kept your place by not going anywhere. The cells
// that went behind the top leave on their own and the badge is redrawn where
// it stands, and that is the behaviour the other two are measured against.
function afterStacking(top,wasGuess){
  if(!STACK) return;
  if(wasGuess) leaveStack(keyOf(top));
  else if(STACK!==keyOf(top)) location.href=stackUrl(keyOf(top));
}

async function chooseTop(top){
  // Who is being chosen between. The candidates of a chooser session, or —
  // where the button is standing on the photograph rather than summoned onto
  // it — whatever is on the page in the same group. There is no session in an
  // opened guess: the page *is* the question, so it never had to be entered.
  const pool=choosing||cells.filter(c=>stackKey(c)===stackKey(top));
  // Whether this was the app's suggestion rather than somebody's stack, asked
  // before `endChoosing` takes the candidates away.
  const wasGuess=pool.some(inGuess);
  // Anything already behind this one is where it should be; writing it again
  // would be an edit that changes nothing and a line in the log saying so.
  const key=keyOf(top);
  const family=pool.filter(c=>c!==top&&c.dataset.under!==key);
  if(!family.length){endChoosing(true);return;}
  const behind=+(top.dataset.behind||0)+family.length;
  // If the one chosen came out of a stack it stays — it is a file that speaks
  // for others now, which is exactly what the grid shows. Everything else
  // borrowed goes back. The server takes it out of whatever it was behind.
  fetched=fetched.filter(c=>c!==top);
  top.dataset.under='';
  endChoosing();
  const out=await applyToSelection('stacked_under',keyOf(top),undefined,family);
  if(out&&out.done){
    // The files that went behind it leave the grid on their own — they
    // stopped matching the moment they were stacked — but the one left
    // standing has to start saying how many it now speaks for.
    markStack(top,behind);
    afterStacking(top,wasGuess);
  }
  // And the selection is spent. It used to survive, holding the file that had
  // just become a top — so the next things ticked were stacked *with it*, and
  // its own members ended up a level down behind a file that was itself behind
  // something. Nothing on screen said that was about to happen.
  clearPicks();
}

// Built here rather than fetched, the way the access and tag chips are: it is
// one anchor, and a round trip to redraw a badge would be a round trip to
// redraw a badge.
function markStack(c,behind){
  c.dataset.behind=String(behind);
  // Not a guess any more. The badge keeps its element and its place in the
  // corner, so without saying so it keeps the amber that means *these look
  // alike and nobody has said yet* over a number somebody has just decided —
  // and the bar goes on offering to refuse a suggestion that is now a stack.
  c.dataset.proposed='0';
  let badge=c.querySelector('.stack');
  if(!badge){
    badge=document.createElement('a');
    badge.className='stack';
    c.appendChild(badge);
  }
  badge.classList.remove('guessed');
  // The whole address, the same as the one the server draws: a badge that
  // said only `?within=` threw away every filter and the grouping with them,
  // so opening a stack four filters deep in a review pass came back out to
  // the undivided library.
  badge.href=stackUrl(keyOf(c));
  badge.title=(behind+1)+' photographs stacked here';
  badge.textContent=String(behind+1);
}

// Which stack a file belongs to, named by the file that currently speaks for
// it. Empty for a file in no stack — and that emptiness is the whole point:
// comparing `dataset.under` directly made every unstacked file on screen look
// like a member of the same stack as every other, because they all share the
// empty string. Selecting a stack's top in the ordinary grid and promoting it
// swept the entire visible grid underneath it.
function stackKey(c){
  return c.dataset.under || c.dataset.proposedUnder
      || (tops(c) ? keyOf(c) : '');
}

async function makeTop(){
  const cs=targetsOn('live');
  if(cs.length!==1){say('select the one to show');return;}
  const top=cs[0];
  const key=stackKey(top);
  if(!key){say('that one is not in a stack');return;}
  // Asked before the write, which is the one that stops it being true.
  const wasGuess=inGuess(top);
  // Saying *this one shows* of the file that already shows is nothing to do
  // — unless nobody has said it yet.
  //
  // A guess is not a decision. Naming its top is what turns it into one, and
  // for the photograph the guess is already drawn on, agreeing is the only
  // thing left to say about it. Refused, there was no gesture for *yes, this
  // is a stack* at all: you could open a suggestion, look at it, see which
  // one it had picked to show — and then refuse it in one click or promote
  // some other photograph, but not simply agree. The two answers a guess
  // takes were not the same shape, and only one of them was there.
  if(key===keyOf(top)&&!guessed(top)){say('that one already shows');return;}
  // Everything else in this stack, the old top included: it stops speaking and
  // starts deferring, which is the same write as any other member. They are on
  // screen because promoting happens inside an opened stack.
  const family=cells.filter(c=>c!==top&&stackKey(c)===key);
  if(!family.length){say('nothing else is in that stack');return;}
  // A stack made before this rule existed can still be taken apart — that
  // write has no top to be the same kind as — but it cannot be rearranged
  // into another one.
  if(mixed([top,...family])){say(MIXED,true);return;}
  if(hasVideo([top,...family])){say(NO_VIDEO,true);return;}
  // One write. The other half — taking the new top out of what it was behind —
  // is the server's, because a file everything defers to cannot be left
  // deferring to one of them whoever asks for it.
  const out=await applyToSelection('stacked_under',keyOf(top),undefined,family);
  if(!out||!out.done) return;
  // The same write the chooser makes, so the same rule about where to go
  // afterwards. Whichever photograph was named, because both answers are the
  // same answer: *these are one photograph, and this is the one that shows*.
  afterStacking(top,wasGuess);
}

async function unstack(){
  // The file that speaks for a stack counts as being in one. Selecting it and
  // being told to select something in a stack was the tool disagreeing with
  // the screen, which showed a depth badge on the thing it was refusing.
  const cs=targetsOn('live').filter(c=>stacked(c)||tops(c));
  if(!cs.length){say('select files that are in a stack');return;}
  const dissolving=cs.some(c=>tops(c));
  const out=await applyToSelection('stacked_under',null,undefined,cs);
  if(!out) return;
  cs.forEach(c=>{c.dataset.under='';});
  // On a stack's page there is nothing to put back — the photographs are on
  // it — and nothing to reload: the stack it is a page of has just stopped
  // existing, and asking for it again is asking for a page that is gone.
  if(STACK){ stillAStack(); return; }
  // Taking a whole stack apart puts photographs *back* into the grid, and the
  // grid can only ever lose cells on its own — the ones that come back were
  // never sent to it. This is the one gesture that needs the page again.
  if(dissolving) location.reload();
}
function targetsOn(side){
  return targets().filter(c=>side==='gone'?gone(c):!gone(c));
}

// A file that no longer matches the filters leaves the grid. Keeping it on
// screen would be showing a view that is no longer true, and the next click
// would act on a photograph the filters say is somewhere else.
function drop(gone){
  // Nothing to do one zoom out: this tidies away *cells*, and a folder page
  // has none — so every test below is about an empty list, and the last of
  // them would have replaced the folders with *nothing matches these filters
  // any more*.
  //
  // Files leaving the view is the one thing a card cannot be redrawn through,
  // because the folder now holds a different set than the one that was read.
  // That is rare and it is real, so it is the one case that reloads.
  if(FOLDERS){ if(gone.length) location.reload(); return; }
  if(!gone.length) return;
  const keys=new Set(gone.map(g=>g.folder+'\n'+g.name));
  const at=cells[cur];
  const leaving=cells.filter(
    c=>keys.has(c.dataset.folder+'\n'+c.dataset.name));
  // Work through the files at the top of the screen and they vanish from
  // above you: the grid shortens over your head and everything left slides up,
  // which reads as the page having scrolled down on its own. So a survivor is
  // measured before and after, and the scroll corrected by the difference —
  // which puts the work you had *not* done yet back where you left it.
  //
  // Anchored to an element rather than to a count of rows, because how much
  // height leaves depends on where the gaps fall and how many cells fit a row,
  // neither of which this knows and both of which change with the window.
  const anchorCell=cells.find(
    c=>!leaving.includes(c)&&c.getBoundingClientRect().bottom>0);
  const wasAt=anchorCell?anchorCell.getBoundingClientRect().top:null;
  leaving.forEach(c=>{picked.delete(c); c.remove();});
  // Out of the server's order as well as the grid's.
  served=Math.max(0,served-leaving.length);
  total=Math.max(0,total-leaving.length);
  const was=cells.indexOf(at);
  cells=cells.filter(c=>!leaving.includes(c));
  resection();
  // Land where the cursor was, not where it would have been pushed to — and
  // land without selecting. Moving the cursor normally *is* a selection, which
  // is right when a person pressed an arrow key and wrong here: nobody asked
  // for this move. The files left because they stopped matching the filters,
  // and ticking whatever slid into the gap would invent a selection out of
  // that — one nobody made, easy to miss, and waiting to be caught up in the
  // next edit.
  cur=-1; anchor=-1;
  if(cells.length)
    setCur(cells.includes(at)?cells.indexOf(at):Math.max(0,was), true);
  if(!cells.length&&grid) grid.innerHTML=
    '<p class="empty">Nothing matches these filters any more.</p>';
  // After every change to the grid, the headings `resection` took away
  // included. Skipped while the viewer is open: it is full-screen, the grid
  // behind it is not what anybody is looking at, and `setCur` has already
  // scrolled to the file on show.
  if(wasAt!==null&&anchorCell&&!viewer.classList.contains('on')){
    const nowAt=anchorCell.getBoundingClientRect().top;
    if(nowAt!==wasAt) window.scrollBy(0,nowAt-wasAt);
  }
  drawSel();
}

// Which multi-valued field each action edits, and whether it adds or removes.
// Which cell attribute each multi-valued action edits, and the two request
// fields that add to it and take from it.
const MULTI={tags:['tags','add_tags','remove_tags'],
            people:['people','add_people','remove_people'],
             access:['audience','add_audience','remove_audience']};

function valuesOf(c,field){
  return c.dataset[field]?c.dataset[field].split('\n'):[];
}

// How much of the selection already carries a value: all of it, some of it,
// or none. The three states Explorer's tree uses, for the same reason — a
// selection is not one thing, and pretending otherwise means every bulk
// edit silently overwrites what you could not see.
function shareState(cs,field,value){
  const has=c=>field==='event' ? (c.dataset.event||'')===value
                               : valuesOf(c,field).includes(value);
  const n=cs.filter(has).length;
  return n===0?'none':(n===cs.length?'all':'some');
}

// What the selection already says, so a menu opens showing the answer
// rather than asking a question whose answer is on screen behind it.
function currentValues(cs,field){
  const seen=new Set();
  cs.forEach(c=>{
    if(field==='event'){ if(c.dataset.event) seen.add(c.dataset.event); }
    else valuesOf(c,field).forEach(v=>seen.add(v));
  });
  return [...seen].sort();
}

// Optimistic: the cell changes now and the write follows, because a cull is a
// rhythm and waiting on SMB between gestures destroys it. A failure puts the
// old value back rather than leaving the screen claiming something untrue.
// `only` names the files to write to when they are not simply *the selection
// on this side*. Stacking writes to everything except the keeper; making a new
// top writes to the rest of its stack and then to itself. Both are one gesture
// over a selection, and neither is the whole of it.
// Whether this change would change *this* file. The page already knows what
// each one says — the grid reads it off the cell, the folder page off the
// files it fetched — so a file that already carries the value being added is
// a file with nothing to do.
//
// Only where the answer is certain. A date override, a stacking or a refusal
// is not written on the cell, so those are always sent: guessing *no change*
// wrongly is a decision silently not made, which is far worse than a write
// that turns out to be a no-op.
function changes(c,act,value,add){
  const multi=MULTI[act];
  if(multi) return valuesOf(c,multi[0]).includes(value)!==!!add;
  if(act==='event') return (c.dataset.event||'')!==(value||'');
  if(act==='deleted') return !!c.dataset.deleted!==!!value;
  return true;
}

async function applyToSelection(act,value,add,only,batch){
  const cs=only||targetsOn(sideOf(act,value));
  if(!cs.length){say('nothing selected');return;}
  const multi=MULTI[act];
  if(multi&&value===null){say('pick a name');return;}
  // Sharing a folder where all but two files are already shared is two
  // writes, not eight hundred and sixty-six. Each one it skips is a sidecar
  // read, a sidecar rewrite and an index row it never has to touch — and a
  // line in the progress it never has to count.
  const todo=cs.filter(c=>changes(c,act,value,add));
  if(!todo.length){say('already set on all of them');return {done:0};}
  const body = multi ? {[add?multi[1]:multi[2]]:[value]} : {[act]:value};
  // Everything each cell said before, so the ones that never got written can
  // be put back. All five fields rather than the one being edited: it costs
  // nothing and means the restore cannot be wrong about which was in play —
  // and `people` was missing from the list, so a half-written People change
  // left the cells it never reached claiming the name.
  const before=todo.map(c=>({tags:c.dataset.tags||'',
                           people:c.dataset.people||'',
                           audience:c.dataset.audience||'',
                           event:c.dataset.event||'',
                           deleted:c.dataset.deleted||''}));
  if(multi) todo.forEach(c=>paint(c,multi[0],value,add));
  else if(act==='event') todo.forEach(c=>{c.dataset.event=value||'';
                                          paintPart(c);});
  // Half a name: the other half is what the cell already says, so the cell
  // is where it is worked out.
  else if(act==='event_head'||act==='event_leaf') todo.forEach(c=>{
    const [head,leaf]=splitEvent(c.dataset.event||'');
    c.dataset.event=joinEvent(act==='event_head'?value:head,
                              act==='event_leaf'?value:leaf);
    paintPart(c);
  });
  // Under `Including deleted` a restored file stays on screen, so the cross
  // has to go the moment the decision does. Under `Only deleted` it leaves
  // instead, and `drop` takes the cell with it.
  else if(act==='deleted') todo.forEach(c=>{
    c.dataset.deleted=value?'1':'';
    c.classList.toggle('gone',!!value);
  });
  const out=await send(todo,body,actLabel(act,value,add),batch);
  // Only the tail. A write that stops half way — cancelled, or a share that
  // dropped — has really written the first part, and painting all of it back
  // would leave the screen denying what is on disk. The cells that were
  // written keep what they now say; the rest go back to what they said.
  const wrote=out?out.done:0;
  todo.slice(wrote).forEach((c,i)=>{
    const was=before[wrote+i];
    c.dataset.tags=was.tags; c.dataset.people=was.people;
    c.dataset.audience=was.audience;
    c.dataset.event=was.event; c.dataset.deleted=was.deleted;
    c.classList.toggle('gone',!!was.deleted);
    repaint(c,'tags'); repaint(c,'people'); repaint(c,'audience');
    paintPart(c);
  });
  return out;
}

// The grid shows people, tags and audience, so all three have to change the
// moment the gesture lands rather than when the round trip finishes.
function paint(c,field,value,add){
  const set=new Set(c.dataset[field]?c.dataset[field].split('\n'):[]);
  add?set.add(value):set.delete(value);
  // Hidden stands alone, as the server keeps it (`decisions._exclusive`):
  // hiding clears the grants, and granting anything clears the hiding.
  if(field==='audience'&&add){
    if(value===ARCHIVED){set.clear();set.add(ARCHIVED);}
    else set.delete(ARCHIVED);
  }
  c.dataset[field]=[...set].sort().join('\n');
  repaint(c,field);
}
// The same rule the server draws by, because a cell edited here and a cell
// fetched fresh must not be able to look different.
function partSaid(){
  return GROUPING.includes('subevent');
}
function paintPart(c){
  const whole=c.dataset.event||'';
  const leaf=splitEvent(whole)[1];
  let el=c.querySelector('.part');
  if(!leaf||partSaid()||VIEW.event===whole){ if(el) el.remove(); return; }
  if(!el){el=document.createElement('span');el.className='part';
          c.appendChild(el);}
  el.setAttribute('title','Sub-event — '+whole);
  el.textContent=leaf;
}
// Which chips on a cell each field is drawn as. It used to be a ternary
// reading *tags, or else access* — so painting `people` wrote the names of
// the people in the photograph into the access chips, and took away the mark
// saying nobody could see it. Three fields is one too many for *or else*.
const PAINTS={tags:'tags', people:'folk', audience:'who'};
function repaint(c,field){
  const cls=PAINTS[field];
  if(!cls) return;
  const all=valuesOf(c,field);
  // Each kind is silent about what the view has already said. Access says
  // only what is unusual, and nobody-at-all gets a mark instead; people leave
  // off the one the view is filtered to, because filtered to `person:Ana`
  // every thumbnail on screen carries Ana. Same rules the server renders by,
  // so a cell edited here and a cell fetched fresh cannot look different.
  const list=cls==='who'?all.filter(v=>v!==USUAL)
            :cls==='folk'?all.filter(v=>v!==VIEW.person)
            :all;
  let mark=c.querySelector('.unshared');
  if(cls==='who'){
    if(!all.length&&!mark){
      mark=document.createElement('span');
      mark.className='unshared';
      mark.setAttribute('title','Nobody has access yet');
      c.appendChild(mark);
    }else if(all.length&&mark){ mark.remove(); }
  }
  let el=c.querySelector('.'+cls);
  if(!list.length){if(el) el.remove(); return;}
  if(!el){el=document.createElement('span');el.className=cls;c.appendChild(el);}
  el.setAttribute('title',list.join(', '));
  el.innerHTML=list.map(v=>`<i title="${esc(v)}">${esc(v)}</i>`).join('');
}

// --- the takeover ------------------------------------------------------------
// Hundreds of sidecars over SMB is seconds of writing, and the only thing that
// ever said so was a 12px note in the corner — which appeared *after* the first
// hundred had already been written, and not at all below that. Between the
// click and the first reply the page looked idle and finished.
//
// So the screen stops answering. Nothing underneath is live while it is up,
// because the grid is mid-change: cells are about to leave it and their
// counts are about to be wrong, and a click into that is a decision taken
// against a view that has already stopped being true.
//
// **Painted on a delay, not on the click.** Most writes are one file and come
// back inside the threshold, and a full-screen takeover flashing on every S
// press would wreck exactly the cull rhythm that the optimistic write exists
// to protect. If a single file does stall, the delay expires and the takeover
// is simply the truth.
const working=document.getElementById('working');
const workWhat=document.getElementById('workwhat');
const workBar=document.getElementById('workbar');
const workTally=document.getElementById('worktally');
const workStop=document.getElementById('workstop');
const workSave=document.getElementById('worksave');
const TAKEOVER_MS=180;
let workTimer=null;
// Asked for, not done yet. A write is a run of requests and this is checked
// between them, never inside one — see `send`.
let stopping=false;

// Enough to tell one gesture from another in the log; it never leaves this
// session and nothing is decided by it.
function newBatch(){
  return Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8);
}

function workOpen(label,total){
  stopping=false;
  if(workStop) workStop.disabled=false;
  if(workWhat) workWhat.innerHTML=label;
  workProgress(0,total);
  clearTimeout(workTimer);
  workTimer=setTimeout(()=>{if(working) working.classList.add('on');},
                       TAKEOVER_MS);
}
function workProgress(done,total,one,many){
  if(workBar) workBar.style.width=(total?Math.round(done/total*100):0)+'%';
  if(workTally) workTally.textContent=
    `${done.toLocaleString()} of ${total.toLocaleString()} `+
    (total===1?(one||'file'):(many||'files'));
}
// The standing count of what is waiting in the bin. It is rendered with the
// page, so every delete, restore and purge has to say what it is now — a
// number that only refreshes on reload is worse than no number, because it
// looks current.
function drawBin(n){
  if(binEl===null||n===null||n===undefined) return;
  binEl.textContent=`${n.toLocaleString()} deleted`;
  binEl.hidden=!n;
  // The dot is the whole of what you see without asking, so it follows the
  // count rather than the page load: deleting something has to light it up.
  //
  // It looked up `activity`, which is the name of a *variable* in the header
  // builder and the id of nothing at all. So this found null on every page
  // and the dot stayed at whatever it was rendered with — which is the one
  // failure it exists to prevent, and it looked exactly like working, because
  // the count beside it was being updated in the line above.
  const me=document.getElementById('me');
  if(me) me.dataset.any=n?'1':'';
}

function workClose(){
  clearTimeout(workTimer); workTimer=null;
  if(working) working.classList.remove('on');
  // Whatever was fetched and never sent goes with it. Holding a hundred
  // megabytes of blobs against a Save button that is no longer on screen is
  // the kind of thing a phone notices.
  ready=null;
  if(workSave) workSave.hidden=true;
}

// Stopping is a decision about the rest of the work, not about the request in
// flight. That one has already reached the server and its files are either
// written or not; tearing it up here would lose the log entry saying which,
// and the whole point of stopping is to be able to go to History and put back
// exactly what did land.
function stopWork(){
  if(!busy||stopping) return;
  stopping=true;
  if(workStop) workStop.disabled=true;
  if(workTally) workTally.textContent='finishing the files already sent…';
}
if(workStop) workStop.onclick=e=>{e.stopPropagation();stopWork();};
if(workSave) workSave.onclick=e=>{e.stopPropagation();shareReady();};

const chooseCancel=document.getElementById('choosecancel');
if(chooseCancel) chooseCancel.onclick=e=>{e.stopPropagation();
                                          endChoosing(true);};

// The takeover says the word the control you pressed says — read off the
// button itself rather than kept as a second vocabulary for the same four
// actions, which would be free to drift from the one on screen.
function actLabel(act,value,add){
  // Neither half has a button of its own — they are two steps of the one
  // marked *Event* — so the word has to be said here rather than read off it.
  if(act==='event_head') return value?'Event &mdash; <b>'+esc(value)+'</b>'
                                     :'Event &mdash; clearing';
  if(act==='event_leaf') return value?'Sub-event &mdash; <b>'+esc(value)+'</b>'
                                     :'Sub-event &mdash; clearing';
  // `deleted` is the one action whose button is not named after its field:
  // one flag, two controls, and the word for it depends on which way it is
  // going.
  if(act==='deleted') return value?'Delete':'Restore';
  const b=actions&&(actions.querySelector('[data-act="'+act+'"] .word')
                   ||actions.querySelector('[data-act="'+act+'"]'));
  const word=esc(b?b.textContent.replace(/\u2026|\.\.\./,'').trim():act);
  if(!value) return word+' &mdash; clearing';
  return word+(add===false?' &mdash; removing <b>':' &mdash; <b>')
             +esc(value)+'</b>';
}

async function send(cs,body,label,sharedBatch){
  if(busy){say('still writing…');return null;}
  busy=true; say('');
  workOpen(label||'Writing', cs.length);
  // One id for the whole gesture. The chunking below is about keeping each
  // request short; the log should not learn about it. A caller doing a gesture
  // in more than one write passes its own, so the log does not learn about
  // that either.
  const batch=sharedBatch||newBatch();
  let done=0, failed=0, gone=[], total=null, binned=null, trouble=null;
  for(let s=0;s<cs.length;s+=CHUNK){
    // Checked between requests, never inside one. The chunk in flight is
    // allowed to finish so that the server records what it wrote, which is
    // what History then has to offer back.
    if(stopping) break;
    const chunk=cs.slice(s,s+CHUNK);
    try{
      // The filters ride along so the server can say which files left the
      // view; it owns the matching rules, and a second copy here would drift.
      const p=new URLSearchParams();
      putView(p,VIEW);
      // Which stack this was decided in. Not one of the filters, and the one
      // thing about *where* a write was made that changes what it does: with
      // the members in front of the curator a cascade must not follow them
      // again, and what left the view is measured against the stack rather
      // than against the library.
      if(STACK) p.set('within',STACK);
      const r=await fetch('/api/decide/bulk?'+p,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...body, batch,
          files:chunk.map(c=>({folder:c.dataset.folder,name:c.dataset.name}))})});
      if(!r.ok) throw new Error((await r.text()).slice(0,200));
      const out=await r.json();
      failed+=out.failed.length;
      gone=gone.concat(out.dropped||[]);
      if(out.total!==null&&out.total!==undefined) total=out.total;
      if(out.binned!==null&&out.binned!==undefined) binned=out.binned;
    }catch(e){
      trouble=e.message;
      break;
    }
    done+=chunk.length;
    workProgress(done,cs.length);
  }
  const stopped=stopping;
  busy=false; workClose();
  drawSel();
  cs.forEach(c=>details.delete(c.dataset.folder+'\n'+c.dataset.name));
  if(viewer&&viewer.classList.contains('on')&&cells[cur]) fill(cells[cur]);
  drop(gone);
  if(total!==null&&countEl) countEl.textContent=`${total.toLocaleString()} files`;
  drawBin(binned);
  if(trouble) say(`stopped after ${done} of ${cs.length}: ${trouble}`,true);
  else if(stopped) say(`stopped — ${done.toLocaleString()} of `
                      +`${cs.length.toLocaleString()} written. `
                      +`History has what landed.`,true);
  else if(failed) say(`${failed} file(s) could not be written`,true);
  else if(gone.length) say(`${gone.length} file(s) no longer match — removed`);
  else say('');
  // How many were actually written, so the caller can put back the ones that
  // were not. It used to report only pass or fail, and a failure half way
  // undid the half that had already been recorded.
  return {done};
}

// What each action edits, and which column its suggestions come from. The
// two are not the same word: `unshare` writes the audience field and offers
// audience values, and using the action name as the column asked the server
// for a column called `share` — a 400, and an empty list every time.
const ACT_COLUMN={tags:'tag', people:'person', access:'audience',
                  event:'event'};
// Which action a button asks for, done to whatever `targets` says.
function runAct(act,b){
    if(act==='delete'){closeMenu();deleteSelection();return;}
    if(act==='stack'){closeMenu();stackSelection();return;}
    if(act==='top'){closeMenu();makeTop();return;}
    if(act==='unstack'){closeMenu();unstack();return;}
    if(act==='nostack'){closeMenu();notAStack();return;}
    if(act==='download'){downloadMenu(b);return;}
    if(act==='restore'){closeMenu();applyToSelection('deleted',false);return;}
    if(act==='purge'){closeMenu();purgeSelection();return;}
    if(act==='splice'){closeMenu();spliceSelection();return;}
    openMenu(b, act==='date'
      ? {mode:'date'}
      : {column:ACT_COLUMN[act]||act, mode:'set', as:act});
}
(actions?[...actions.querySelectorAll('[data-act]')]:[]).forEach(b=>{
  b.onclick=e=>{
    e.stopPropagation();
    if(vActing){vActing=null; closeMenu();}
    if(menu) menu.classList.remove('over');
    runAct(b.dataset.act,b);
  };
});
// The same actions from the viewer, done to the photograph on show and to
// nothing else — whatever happens to be selected in the grid behind it.
function viewAct(act,b){
  const c=cells[cur];
  if(!c) return;
  if(!vActing||vActing[0]!==c){closeMenu(); vActing=[c];}
  if(menu) menu.classList.add('over');
  runAct(act,b);
}
document.querySelectorAll('[data-vact]').forEach(b=>{
  b.onclick=e=>{e.stopPropagation(); viewAct(b.dataset.vact,b);};
});
// A binned file is restored or purged; a living one is everything else.
function drawViewActs(c){
  const dead=!!(c&&gone(c));
  document.querySelectorAll('.vgrp').forEach(g=>{
    g.hidden=(g.dataset.side==='gone')!==dead;
  });
}

// A page of its own rather than a menu: a timeline is not a thing a menu can
// hold. A clip opens its source's, standing on the clip.
function spliceSelection(){
  const c=targetsOn('live')[0];
  if(!c||!c.dataset.splice) return;
  const own=c.dataset.splice===c.dataset.name;
  location.href='/splice/'+encodeURIComponent(c.dataset.folder)+'/'
    +encodeURIComponent(c.dataset.splice)
    +(own?'':'#'+encodeURIComponent(c.dataset.name));
}

// The one action with no value to pick, so it asks instead of opening a menu.
// A confirm rather than a ceremony: this is the soft delete, it writes
// `deleted` into the sidecar like any other judgement, and History puts it
// back. Destroying the file itself is somewhere else entirely, and admin only.
//
// The question says none of that. *Deleted* is what the curator meant and what
// they should be told; that it is recoverable, and by whom, is how the app
// keeps its promise rather than a caveat on it. Answering "are you sure?" with
// "well, sort of" invites a yes that was never really given.
async function deleteSelection(){
  const cs=targetsOn('live');
  if(!cs.length){say('nothing selected');return;}
  // A video with clips cannot simply go: a clip is its source's bytes plus a
  // range (spec/clips.md §5). Unless every one of its clips is going too, the
  // question is what to do instead.
  const blocked=cs.filter(c=>{
    const n=+(c.dataset.clips||0);
    if(!n) return false;
    const going=cs.filter(o=>o!==c&&o.dataset.folder===c.dataset.folder
                             &&o.dataset.splice===c.dataset.name).length;
    return going<n;
  });
  let rest=cs;
  if(blocked.length){
    const choice=await clipsChoice(blocked);
    if(!choice) return;
    if(choice==='hide'){
      await applyToSelection('access',ARCHIVED,true,blocked);
    }else{
      for(const c of blocked){
        const r=await fetch('/api/clips/free',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({folder:c.dataset.folder,
                               source:c.dataset.name})});
        if(!r.ok){
          let why=''; try{why=(await r.json()).detail;}catch(e){}
          say(why||'could not keep the clips',true); return;
        }
      }
      location.reload(); return;
    }
    rest=cs.filter(c=>!blocked.includes(c));
    if(!rest.length) return;
  }
  const what=rest.length===1?'this file':`these ${rest.length.toLocaleString()} files`;
  if(!confirm(`Are you sure you want to delete ${what}?`)) return;
  applyToSelection('deleted',true,undefined,rest);
}

// Three answers, so not `confirm`: hide it (nothing is lost, and it is the
// usual intent), bin it and keep its clips as files of their own, or leave it.
function clipsChoice(blocked){
  return new Promise(done=>{
    const d=document.createElement('dialog');
    d.className='choice';
    const one=blocked.length===1;
    const n=blocked.reduce((t,c)=>t+(+(c.dataset.clips||0)),0);
    const p=document.createElement('p');
    p.textContent=(one?blocked[0].dataset.name+' has ':'These videos have ')
      +n+' clip'+(n===1?'':'s')+' cut from '+(one?'it':'them')
      +'. A clip is a piece of its video, and cannot outlive it.';
    d.appendChild(p);
    const row=document.createElement('div'); row.className='choices';
    const add=(word,value,cls)=>{
      const b=document.createElement('button'); b.textContent=word;
      if(cls) b.className=cls;
      b.onclick=()=>{d.close(); d.remove(); done(value);};
      row.appendChild(b);
    };
    add(one?'Archive it instead':'Archive them instead','hide','primary');
    add('Bin '+(one?'it':'them')+', keep the clips as files','free');
    add('Cancel',null);
    d.appendChild(row);
    d.oncancel=()=>{d.remove(); done(null);};
    document.body.appendChild(d);
    if(d.showModal) d.showModal(); else d.setAttribute('open','');
  });
}

// The end of a file, so the question names the thing that cannot be taken
// back rather than asking politely. It goes to its own endpoint: purging is
// not a decision about a photograph, it is the end of one, and a shape
// `decide` could accept would make it one field of a routine edit.
async function purgeSelection(){
  const cs=targetsOn('gone');
  if(!cs.length){say('nothing selected');return;}
  const what=cs.length===1?'1 file':`${cs.length.toLocaleString()} files`;
  if(!confirm(`Permanently destroy ${what}? The originals and everything `
             +`made from them are removed. This cannot be undone.`)) return;
  if(busy){say('still writing…');return;}
  busy=true; say(''); workOpen('Purge', cs.length);
  const batch=newBatch();
  let purged=0, failed=0, gone=[], total=null, binned=null;
  try{
    for(let s0=0;s0<cs.length;s0+=CHUNK){
      if(stopping) break;
      const chunk=cs.slice(s0,s0+CHUNK);
      const p=new URLSearchParams();
      putView(p,VIEW);
      if(STACK) p.set('within',STACK);
      const r=await fetch('/api/purge?'+p,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({batch,
          files:chunk.map(c=>({folder:c.dataset.folder,name:c.dataset.name}))})});
      if(!r.ok) throw new Error((await r.text()).slice(0,200));
      const out=await r.json();
      purged+=out.purged; failed+=out.failed.length;
      gone=gone.concat(out.dropped||[]);
      if(out.total!==null&&out.total!==undefined) total=out.total;
      if(out.binned!==null&&out.binned!==undefined) binned=out.binned;
      workProgress(Math.min(s0+chunk.length,cs.length),cs.length);
    }
  }catch(e){
    busy=false; workClose();
    say(`stopped after ${purged} of ${cs.length}: ${e.message}`,true);
    return;
  }
  busy=false; workClose();
  drop(gone);
  if(total!==null&&countEl) countEl.textContent=`${total.toLocaleString()} files`;
  drawBin(binned);
  if(failed) say(`${failed} file(s) could not be purged`,true);
  else say(`${purged.toLocaleString()} file(s) destroyed`);
}

// --- keyboard ----------------------------------------------------------------
// The mouse is the interface. Keyboard navigation of the grid — arrows that
// moved and selected, shift to extend, ctrl to move without selecting, S to
// repeat a share — is gone rather than patched. Every one of those keys had
// to decide what it meant for the selection, each answered slightly
// differently, and between them they kept producing selections nobody had
// made. None of them could do anything the mouse cannot, so none of them was
// worth the ambiguity. Keyboard support is worth designing on purpose later,
// not accreting a key at a time.
//
// What stays is what only a key can say once the viewer is full-screen:
// which way to go, and stop.
// Whether a photograph is open, on a page that may have nothing to open one
// in. Asked from the key handler, which is bound to the document and so runs
// on every page the script is served to.
function open_(){ return !!viewer&&viewer.classList.contains('on'); }

document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT') return;
  if(e.key==='Escape'){
    // Dismissal rather than navigation. A full-screen viewer with no key out
    // is a trap, even though clicking beside the picture also closes it.
    if(busy){stopWork();return;}
    if(choosing){endChoosing(true);return;}
    // No viewer on the landing page: it is folders, not photographs. Escape
    // there is still the way out of a menu, and reaching for a viewer that is
    // not on the page threw every time somebody pressed it.
    if(open_()) closeViewer();
    else if(!menu.hidden) closeMenu();
    return;
  }
  if(!open_()) return;
  const step=e.key==='ArrowRight'?1:e.key==='ArrowLeft'?-1:0;
  if(!step) return;
  e.preventDefault();
  const to=nextShown(cur,step);
  if(to<0) return;
  // Paging is looking, not choosing, so whatever is selected stays selected.
  setCur(to, true);
  drawSel();
});

// --- grouping ----------------------------------------------------------
// The heading is the control: the thing you want to regroup is the thing
// you click, and it costs no row at the top — every row of chrome up there
// is a row of photographs pushed off the screen.
function groupUrl(levels){
  const q=new URLSearchParams();
  putView(q,VIEW);
  q.set('group',levels.join(',')||'none');
  return PAGE+'?'+q;
}

function groupMenu(anchorEl,level,insert){
  const levels=[...GROUPING];
  menu.innerHTML='<div id="menulist"></div>';
  const list=menu.querySelector('#menulist');
  const row=(label,fn,cls)=>{
    const d=document.createElement('div');
    d.className='opt'+(cls?' '+cls:'');
    d.innerHTML=`<span>${esc(label)}</span>`;
    d.onclick=e=>{e.stopPropagation();closeMenu();fn();};
    list.appendChild(d);
  };
  const head=document.createElement('div');
  head.className='band';
  head.textContent=insert?'Then group by':'Group by';
  list.appendChild(head);
  // What is already in the order, and so not on offer again. Replacing a
  // level does not count its own key as taken — that is the row showing which
  // choice is current, ticked. Inserting replaces nothing, so there every
  // level counts, including the one the + hangs off: it was exempt before,
  // which left the innermost grouping offered again in its own *Then group
  // by* menu, and picking it did nothing at all.
  const taken=levels.filter((_,i)=>insert||i!==level);
  const field=k=>ONE_FIELD[k]||k;
  for(const [key,label] of GRID_GROUPS){
    if(key==='none') continue;
    if(taken.some(k=>field(k)===field(key))) continue;
    row(label,()=>{
      const next=[...levels];
      if(insert) next.splice(level+1,0,key); else next[level]=key;
      location.href=groupUrl(next);
    },levels[level]===key&&!insert?'cur':'');
  }
  placeMenu(anchorEl);
  menuCtx={key:'group:'+level+':'+insert};
}


// Every cell under a heading, down to the next one. There is one heading per
// section now, so this is simply "until the next heading".
// A section is a box: its heading and its cells are inside it together, which
// is what lets the heading stay at the top of the screen for as long as you
// are in it. It used to be a run of siblings walked until the next heading —
// which is the same set of cells and a good deal more to get wrong.
function sectionCells(h){
  const box=h.parentNode;
  return box?[...box.querySelectorAll('.cell')]:[];
}

// A section is its heading and the cells beneath it, so when files leave the
// view both have to answer for it: the count says what is there now, and a
// heading whose files have all gone is a label for nothing.
//
// Recounted from the DOM rather than decremented by the number dropped. The
// grid is one render with nothing lazily loaded, so the cells present *are*
// the section — a running tally would be a second account of the same thing,
// free to drift from it.
function resection(){
  document.querySelectorAll('.group').forEach(h=>{
    const mine=sectionCells(h);
    // The whole box, not the heading out of it: what would be left is an
    // empty section holding the gap where a section used to be.
    if(!mine.length){ (h.parentNode||h).remove(); return; }
    const n=h.querySelector('.dim');
    if(!n) return;
    // The grid arrives a page at a time, so the cells present are not the
    // section. The heading keeps the server's count for the whole of it and
    // moves by what has come and gone since (`seen` is how many cells were
    // here when that count was true).
    if(n.dataset.base===undefined){
      n.dataset.base=String(parseInt((n.textContent||'').replace(/[^0-9]/g,''),10)
                            ||mine.length);
      n.dataset.seen=String(mine.length);
    }
    const now=+n.dataset.base+(mine.length-(+n.dataset.seen));
    n.textContent=Math.max(now,mine.length).toLocaleString();
  });
}

function drawGroupPicks(){
  document.querySelectorAll('.group').forEach(h=>{
    const mine=sectionCells(h);
    const n=mine.filter(c=>picked.has(c)).length;
    h.dataset.state=n===0?'none':(n===mine.length?'all':'some');
  });
}

function wireHeading(h){
  // Each crumb is two controls: the name changes that level, the cross drops
  // it. Removal is on the crumb rather than inside the menu because *take this
  // away* is a thing you should be able to see, not go and find.
  h.querySelectorAll('.crumb').forEach(crumb=>{
    const level=+crumb.dataset.level;
    crumb.querySelector('.grpname').onclick=e=>{
      e.stopPropagation(); groupMenu(crumb,level,false);};
    const rm=crumb.querySelector('.rmgrp');
    if(rm) rm.onclick=e=>{
      e.stopPropagation();
      const next=[...GROUPING]; next.splice(level,1);
      location.href=groupUrl(next);
    };
  });
  const add=h.querySelector('.addgrp');
  if(add) add.onclick=e=>{
    e.stopPropagation(); groupMenu(add,GROUPING.length-1,true);};
  // The landing page's heading is the same control minus the selecting: it
  // is a page of folders, and there is nothing on it to tick.
  const gp=h.querySelector('.grppick');
  if(gp) gp.onclick=e=>{
    e.stopPropagation();
    const pick=()=>{
      const mine=sectionCells(h);
      const on=mine.some(c=>!picked.has(c));
      mine.forEach(c=>togglePick(cells.indexOf(c),on));
      if(on&&mine.length) setCur(cells.indexOf(mine[0]),true);
      drawSel();
    };
    // The whole group, not the part of it that has arrived: a day is
    // selected as a day. Waiting only when there is something to wait for.
    const sec=h.parentNode;
    if(moreToCome()&&sec&&!sec.nextElementSibling) loadSection(h).then(pick);
    else pick();
  };
}
document.querySelectorAll('.group').forEach(wireHeading);
resection();

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
