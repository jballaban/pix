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

