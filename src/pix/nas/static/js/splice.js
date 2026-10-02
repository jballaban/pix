
(function(){
const S=SPLICE;
const $=id=>document.getElementById(id);
const v=$('sv'), wrap=$('tlwrap'), track=$('track'), bars=$('bars'),
      ph=$('ph'), note=$('note');
let zoom=1, dur=S.duration||0, busy=false, dragging=false, shown=null,
    keys=null, hidden=!!S.hidden, sel=null;
const frame=1/(S.fps||30);
const ms=t=>Math.round(t*1000)/1000;
const EPS=0.0005;

// **The draft.** The clips as they will be saved, each carrying who it is:
// a saved clip keeps its name however it is trimmed, a clip made here is
// `new…` until Save names it, a split part says which clip it was cut from,
// and a join says which clips it absorbed. Nothing reaches the server until
// Save, and then the draft is sent as it ends — so moving an end about and
// back again is only ever a trim.
let saved=[], draft=[], deleted=[], undos=[], temp=0;
// Making a new clip: `null` when not, and otherwise its start once set.
let creating=null;

function say(text,bad){
  if(!note) return;
  note.textContent=text||''; note.hidden=!text;
  note.classList.toggle('loud',!!bad);
  clearTimeout(say.t);
  if(text) say.t=setTimeout(()=>{note.hidden=true;},bad?7000:2500);
}
window.addEventListener('error',e=>say('page error: '+e.message,true));
function fmt(t){
  t=Math.max(0,t||0);
  const m=Math.floor(t/60), s=t-m*60;
  return m+':'+(s<10?'0':'')+s.toFixed(2);
}
if(v.requestVideoFrameCallback){
  const tick=(n,meta)=>{shown=meta.mediaTime; v.requestVideoFrameCallback(tick);};
  v.requestVideoFrameCallback(tick);
}
function here(){
  return ms(v.paused&&!v.seeking&&shown!=null?shown:(v.currentTime||0));
}
const pct=t=>(dur>0?t/dur*100:0)+'%';
const ranges=()=>draft.filter(c=>c.end>c.start).sort((a,b)=>a.start-b.start);
const stills=()=>draft.filter(c=>c.end===c.start);
const selected=()=>draft.find(c=>c.id===sel)||null;
const isSaved=id=>saved.some(c=>c.name===id);
const savedOf=id=>saved.find(c=>c.name===id)||null;
const inside=t=>ranges().find(c=>c.start<t-EPS&&t<c.end-EPS)||null;
const newId=()=>'new'+(++temp);

function fromServer(list){
  saved=list.filter(c=>!c.deleted);
  draft=saved.map(c=>({id:c.name,start:c.start,end:c.end,copy_of:null,
                       absorbs:[]}));
  deleted=[]; undos=[]; creating=null;
  if(sel&&!selected()) sel=null;
}
fromServer(S.clips);
// How many things Save would do — what the bar counts and leaving warns of.
function changes(){
  let n=deleted.length;
  for(const c of draft){
    const was=savedOf(c.id);
    if(!was) n++;
    else if(Math.abs(was.start-c.start)>EPS||Math.abs(was.end-c.end)>EPS) n++;
    n+=c.absorbs.length;
  }
  return n;
}
function changed(c){
  const was=savedOf(c.id);
  return !was||Math.abs(was.start-c.start)>EPS||Math.abs(was.end-c.end)>EPS
         ||c.absorbs.length>0;
}
function remember(){
  undos.push(JSON.stringify({draft,deleted,temp}));
  if(undos.length>200) undos.shift();
}
function undo(){
  const last=undos.pop();
  if(!last){say('nothing to undo');return;}
  const st=JSON.parse(last);
  draft=st.draft; deleted=st.deleted; temp=st.temp;
  if(sel&&!selected()) sel=null;
  draw(); say('undone');
}

async function send(url,body){
  if(busy){say('still saving');return null;}
  busy=true;
  try{
    const r=await fetch(url,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const text=await r.text();
    let json=null; try{json=JSON.parse(text);}catch(e){}
    if(!r.ok){say((json&&json.detail)||text||('failed: '+r.status),true);
              return null;}
    return json||{};
  }catch(e){say('could not reach the server',true);return null;}
  finally{busy=false;}
}
async function fetchSaved(){
  try{
    const r=await fetch('/api/clips/'+encodeURIComponent(S.folder)+'/'
                        +encodeURIComponent(S.source));
    if(r.ok) return await r.json();
  }catch(e){}
  return null;
}

// --- drawing ----------------------------------------------------------------
function draw(){
  track.style.width=(zoom*100)+'%';
  // Nothing to scroll to until it is wider than its box.
  wrap.style.overflowX=zoom>1?'auto':'hidden';
  bars.innerHTML='';
  for(const c of ranges()){
    const b=document.createElement('div');
    const was=savedOf(c.id);
    b.className='bar'+(c.id===sel?' on':'')+(changed(c)?' draft':'')
      +(was&&!changed(c)&&was.cut===false?' uncut':'');
    b.style.left=pct(c.start); b.style.width=pct(c.end-c.start);
    b.title=fmt(c.start)+' – '+fmt(c.end);
    b.onpointerdown=e=>e.stopPropagation();
    b.onclick=e=>{e.stopPropagation(); if(!dragging) pick(c.id);};
    if(c.id===sel) for(const side of ['l','r']){
      const h=document.createElement('div');
      h.className='h '+side;
      h.onpointerdown=e=>drag(e,c,side);
      h.onclick=e=>e.stopPropagation();
      b.appendChild(h);
    }
    bars.appendChild(b);
  }
  for(const c of stills()){
    const p=document.createElement('div');
    p.className='pin'+(c.id===sel?' on':'');
    p.style.left=pct(c.start);
    p.title='Photo at '+fmt(c.start);
    p.onpointerdown=e=>e.stopPropagation();
    p.onclick=e=>{e.stopPropagation(); pick(c.id);};
    bars.appendChild(p);
  }
  if(creating!==null){
    const l=document.createElement('div');
    l.className='newstart'; l.style.left=pct(creating);
    bars.appendChild(l);
  }
  $('tdur').textContent=fmt(dur);
  drawStrip(); drawKeys(); playhead(false); drawBars();
}
function drawBars(){
  const c=selected(), n=changes();
  $('newbar').hidden=!newMode;
  $('newsay').textContent=creating===null
    ?'drag across the timeline, or set its start and end at the playhead'
    :'starts at '+fmt(creating)+' — now set where it ends';
  $('selbar').hidden=!c||newMode;
  if(c&&!newMode){
    const still=c.end===c.start;
    $('selname').textContent=still?'Photo at '+fmt(c.start)
      :'Clip '+fmt(c.start)+' – '+fmt(c.end);
    for(const id of ['bstart','bend','bsplit','bjoin']) $(id).hidden=still;
    $('bjoin').hidden=still||!nextOf(c);
    const was=savedOf(c.id);
    $('bopen').hidden=!was;
    if(was){
      const day=(was.date||'').slice(0,10);
      $('bopen').href='/browse'+(day?'?date='+encodeURIComponent(day):'')
        +'#open:'+encodeURIComponent(S.folder+'/'+c.id);
    }
  }
  $('draftbar').hidden=!n;
  $('dirty').textContent=n+' unsaved change'+(n===1?'':'s');
  $('bundo').hidden=!undos.length;
}
function drawStrip(){
  const box=$('strip'), st=S.strip;
  if(!box) return;
  box.innerHTML='';
  if(!st||!st.n||!st.h) return;
  const W=track.offsetWidth||0, H=box.offsetHeight||52;
  const fw=H*st.w/st.h;
  if(!W||!fw) return;
  const k=Math.max(1,Math.min(st.n,Math.floor(W/Math.max(fw,64))));
  const tw=W/k;
  for(let j=0;j<k;j++){
    const idx=Math.min(st.n-1,Math.floor((j+0.5)*st.n/k));
    const tile=document.createElement('i');
    tile.style.left=(j*tw)+'px'; tile.style.width=tw+'px';
    const img=document.createElement('b');
    img.style.left=((tw-fw)/2)+'px'; img.style.width=fw+'px';
    img.style.backgroundImage='url("'+st.url+'")';
    img.style.backgroundSize=(st.n*fw)+'px '+H+'px';
    img.style.backgroundPosition=(-idx*fw)+'px 0';
    tile.appendChild(img); box.appendChild(tile);
  }
}
window.addEventListener('resize',()=>drawStrip());
function drawKeys(){
  const box=$('keys');
  box.innerHTML='';
  if(!keys||!dur) return;
  for(const k of keys){
    const i=document.createElement('i'); i.style.left=pct(k); box.appendChild(i);
  }
}
function snapStart(t,lo,hi){
  if(!keys||!keys.length) return t;
  let best=null;
  for(const k of keys){
    if(k<lo-EPS||k>=hi-EPS) continue;
    if(best===null||Math.abs(k-t)<Math.abs(best-t)) best=k;
  }
  return best===null?t:best;
}
// The first keyframe or the last one: the two ends of the footage a cut can
// start from.
function toKeyEnd(dir){
  const ends=keys&&keys.length?keys:[0,v.duration||0];
  v.pause(); v.currentTime=dir>0?ends[ends.length-1]:ends[0];
}
function toKey(dir){
  if(!keys||!keys.length){step(dir*1);return;}
  const t=v.currentTime||0;
  const next=dir>0?keys.find(k=>k>t+0.002)
                  :[...keys].reverse().find(k=>k<t-0.002);
  if(next!==undefined){v.pause(); v.currentTime=next;}
}
fetch('/api/keyframes/'+encodeURIComponent(S.folder)+'/'
      +encodeURIComponent(S.source))
  .then(r=>r.ok?r.json():null)
  .then(j=>{ if(j&&Array.isArray(j.keys)){keys=j.keys; draw();} })
  .catch(()=>{});
function playhead(follow){
  ph.style.left=pct(v.currentTime||0);
  $('tcur').textContent=fmt(v.currentTime||0);
  if(!follow) return;
  const x=ph.offsetLeft, w=wrap.clientWidth;
  if(x<wrap.scrollLeft||x>wrap.scrollLeft+w-8)
    wrap.scrollLeft=Math.max(0,x-w/2);
}
function pick(id){
  if(newMode) return;
  sel=id;
  const c=selected();
  if(c){v.pause(); v.currentTime=c.start;}
  draw();
}
const nextOf=c=>ranges().find(o=>o!==c&&o.start>=c.end-EPS)||null;

// --- what a range does to the clips it lands on ------------------------------
// A clip made or moved over others **wins**: one it overlaps is trimmed back
// to its edge, one it covers is removed, and one it lands inside is split
// around it. Worked out first, so the question can say exactly what will
// happen before anything does.
function effects(a,b,except){
  const out={trim:[],remove:[],split:[]};
  for(const c of ranges()){
    if(c===except) continue;
    if(c.end<=a+EPS||c.start>=b-EPS) continue;
    if(c.start>=a-EPS&&c.end<=b+EPS) out.remove.push(c);
    else if(c.start<a&&c.end>b) out.split.push(c);
    else out.trim.push(c);
  }
  return out;
}
function allowed(fx,what){
  const said=[];
  if(fx.remove.length) said.push('removes '+fx.remove.length+' clip'
    +(fx.remove.length===1?'':'s')+' it covers (to the bin)');
  if(fx.split.length) said.push('splits '+(fx.split.length===1?'the clip'
    :fx.split.length+' clips')+' it lands inside, in two around it');
  if(!said.length) return true;
  return confirm(what+' '+said.join(' and ')+'. Continue?');
}
function apply(fx,a,b){
  for(const c of fx.remove) drop(c);
  for(const c of fx.trim){
    if(c.start<a) c.end=a;
    else c.start=ms(snapStart(b,b,c.end));
  }
  for(const c of fx.split){
    const end=c.end;
    c.end=a;
    draft.push({id:newId(),start:ms(snapStart(b,b,end)),end,copy_of:c.id,
                absorbs:[]});
  }
}
// Take a clip out of the draft: deleted if it was saved, and so is whatever
// it had absorbed, since those were only living on inside it.
function drop(c){
  draft=draft.filter(x=>x!==c);
  if(isSaved(c.id)) deleted.push(c.id);
  for(const a of c.absorbs) if(isSaved(a)) deleted.push(a);
  if(sel===c.id) sel=null;
}

// --- New clip ---------------------------------------------------------------
let newMode=false;
// Started at the playhead: the next thing done after *New clip* is almost
// always *start it here*, and a drag across the timeline still replaces it.
function startNew(){ newMode=true; creating=ms(here()); sel=null; draw(); }
function cancelNew(){ newMode=false; creating=null; draw(); }
function newStart(t){ if(!newMode) startNew(); creating=ms(t); draw(); }
function newEnd(t){
  if(!newMode||creating===null){say('set where the clip starts first',true);return;}
  makeClip(Math.min(creating,t),Math.max(creating,t));
}
function makeClip(a,b){
  if(b-a<frame){say('a clip needs an end after its start',true);return;}
  a=ms(snapStart(a,0,b)); b=ms(b);
  const fx=effects(a,b,null);
  if(!allowed(fx,'This new clip')) return;
  remember();
  apply(fx,a,b);
  const id=newId();
  draft.push({id,start:a,end:b,copy_of:null,absorbs:[]});
  newMode=false; creating=null; sel=id;
  draw(); say('new clip — Save keeps it');
}

// --- editing the selected clip --------------------------------------------------
function setEnds(c,a,b){
  if(b-a<frame){say('a clip needs an end after its start',true);draw();return;}
  if(Math.abs(a-c.start)>EPS) a=ms(snapStart(a,0,b));
  b=ms(b);
  const fx=effects(a,b,c);
  if(!allowed(fx,'Moving this clip')){draw();return;}
  remember();
  apply(fx,a,b);
  c.start=a; c.end=b;
  draw();
}
function split(){
  const c=selected(), t=here();
  if(!c||c.end===c.start) return;
  const at=ms(snapStart(t,c.start+0.001,c.end));
  if(at-c.start<frame||c.end-at<frame){
    say('move the playhead inside the clip to split it',true);return;}
  if(!confirm('Split this clip in two at '+fmt(at)+'? The second part '
             +'becomes a new clip, with a copy of its tags.')) return;
  remember();
  draft.push({id:newId(),start:at,end:c.end,copy_of:c.id,absorbs:[]});
  c.end=at;
  draw();
}
function join(){
  const c=selected(), next=c&&nextOf(c);
  if(!next) return;
  if(!confirm('Join the next clip into this one? It stops being a clip of '
             +'its own, and its tags merge into this one.')) return;
  remember();
  c.end=next.end;
  if(isSaved(next.id)) c.absorbs.push(next.id);
  c.absorbs.push(...next.absorbs);
  draft=draft.filter(x=>x!==next);
  draw();
}
function del(){
  const c=selected();
  if(!c) return;
  const still=c.end===c.start;
  if(!confirm(still?'Delete this photo?':'Delete this clip? It goes to the '
                     +'bin when you save.')) return;
  remember();
  drop(c);
  draw();
}
function still(){
  v.pause();
  remember();
  const id=newId(), t=here();
  draft.push({id,start:t,end:t,copy_of:null,absorbs:[]});
  draw(); say('photo at '+fmt(t)+' — Save keeps it');
}

// --- saving ---------------------------------------------------------------------
async function save(){
  if(!changes()) return;
  const out=await send('/api/clips/save',{folder:S.folder,source:S.source,
    clips:draft.map(c=>({id:c.id,start:c.start,end:c.end,
                         copy_of:c.copy_of,absorbs:c.absorbs})),
    deleted});
  if(!out) return;
  const list=await fetchSaved();
  if(list){
    const was=sel;
    fromServer(list);
    sel=was&&out.names&&out.names[was]?out.names[was]:null;
  }
  draw(); say('saved');
}
async function discard(){
  if(changes()&&!confirm('Throw away every unsaved change?')) return;
  const list=await fetchSaved();
  fromServer(list||saved);
  sel=null; newMode=false;
  draw(); say('changes discarded');
}
window.addEventListener('beforeunload',e=>{
  if(changes()||creating!==null){e.preventDefault(); e.returnValue='';}
});

// --- the video ---------------------------------------------------------------------
v.addEventListener('loadedmetadata',()=>{
  if(isFinite(v.duration)&&v.duration>0) dur=v.duration;
  v.playbackRate=rate;
  draw(); fromHash();
});
v.addEventListener('timeupdate',()=>{
  const c=selected();
  if(c&&c.end>c.start&&!v.paused&&v.currentTime>=c.end-0.03)
    v.currentTime=c.start;
  playhead(!v.paused);
});
v.addEventListener('play',()=>{
  $('bplay').classList.add('playing');
  $('bplay').setAttribute('aria-label','Pause');
  const c=selected();
  if(c&&c.end>c.start&&(v.currentTime<c.start||v.currentTime>=c.end-0.03))
    v.currentTime=c.start;
});
v.addEventListener('pause',()=>{
  $('bplay').classList.remove('playing');
  $('bplay').setAttribute('aria-label','Play');
});
v.addEventListener('seeked',()=>playhead(true));
(function loop(){ if(!v.paused) playhead(true); requestAnimationFrame(loop); })();

function timeAt(x){
  const r=track.getBoundingClientRect();
  return Math.min(dur,Math.max(0,(x-r.left)/(r.width||1)*dur));
}
// The timeline: a click moves the playhead and leaves the clip there picked;
// with New clip on, a drag across it is the new clip.
track.addEventListener('pointerdown',e=>{
  if(e.pointerType==='touch'&&touches.size>0) return;
  if(e.button!==undefined&&e.button!==0) return;
  const x0=e.clientX, t0=timeAt(x0);
  let drawing=false, t1=t0;
  const box=document.createElement('div'); box.className='newrange';
  const move=ev=>{
    if(!newMode) return;
    if(!drawing&&Math.abs(ev.clientX-x0)<6) return;
    if(!drawing){drawing=true; dragging=true; v.pause(); bars.appendChild(box);}
    t1=timeAt(ev.clientX);
    const a=Math.min(t0,t1), b=Math.max(t0,t1);
    box.style.left=pct(a); box.style.width=pct(b-a);
    v.currentTime=t1;
  };
  const up=ev=>{
    track.removeEventListener('pointermove',move);
    track.removeEventListener('pointerup',up);
    track.removeEventListener('pointercancel',up);
    if(drawing){
      setTimeout(()=>{dragging=false;},0);
      box.remove();
      makeClip(Math.min(t0,t1),Math.max(t0,t1));
      return;
    }
    v.pause(); v.currentTime=timeAt(ev.clientX);
    if(!newMode){ const c=inside(v.currentTime); sel=c?c.id:null; }
    draw();
  };
  track.addEventListener('pointermove',move);
  track.addEventListener('pointerup',up);
  track.addEventListener('pointercancel',up);
});
// Dragging an end of the selected clip. Past a neighbour it trims it back,
// and over one it asks before removing it — the same rule as a new clip.
function drag(e,c,side){
  e.preventDefault(); e.stopPropagation();
  dragging=true; v.pause();
  const h=e.currentTarget, bar=h.parentNode;
  if(h.setPointerCapture) h.setPointerCapture(e.pointerId);
  const lo=side==='l'?0:c.start+frame, hi=side==='r'?dur:c.end-frame;
  let t=side==='l'?c.start:c.end;
  const move=ev=>{
    t=Math.min(hi,Math.max(lo,timeAt(ev.clientX)));
    if(side==='l') t=snapStart(t,0,c.end);
    const a=side==='l'?t:c.start, b=side==='r'?t:c.end;
    bar.style.left=pct(a); bar.style.width=pct(b-a);
    v.currentTime=t;
  };
  const up=()=>{
    h.removeEventListener('pointermove',move);
    h.removeEventListener('pointerup',up);
    h.removeEventListener('pointercancel',up);
    setTimeout(()=>{dragging=false;},0);
    const a=ms(side==='l'?t:c.start), b=ms(side==='r'?t:c.end);
    if(a===c.start&&b===c.end){draw();return;}
    setEnds(c,a,b);
  };
  h.addEventListener('pointermove',move);
  h.addEventListener('pointerup',up);
  h.addEventListener('pointercancel',up);
}

function drawHide(){
  const b=$('bhide');
  b.setAttribute('aria-label',hidden?'Unarchive original':'Archive original');
  b.classList.toggle('on',hidden);
  b.title=(hidden
    ?'The original is archived, out of every view; its clips are not. '
     +'Bring it back'
    :'Archive the original — out of every view, leaving its clips')+' (H)';
}
// Immediate, unlike the clips: this is a decision about the video itself.
async function hide(){
  const body={folder:S.folder,name:S.source};
  body[hidden?'remove_audience':'add_audience']=[S.hiddenName];
  if(await send('/api/decide',body)){
    hidden=!hidden; drawHide();
    say(hidden?'original archived':'original back in view');
  }
}
function step(dt){
  v.pause();
  v.currentTime=Math.min(dur,Math.max(0,(v.currentTime||0)+dt));
}
function toggle(){ if(v.paused) v.play().catch(()=>{}); else v.pause(); }

const RATES=[0.5,1,1.5,2];
let rate=1;
function setRate(r){
  rate=r; v.playbackRate=r;
  const box=$('rate');
  if(box) box.value=String(r);
}
if($('rate')) $('rate').onchange=e=>{
  setRate(parseFloat(e.target.value)||1); e.target.blur();
};
function nudgeRate(dir){
  const i=RATES.indexOf(rate), j=Math.min(RATES.length-1,Math.max(0,i+dir));
  setRate(RATES[j]); say('speed '+RATES[j]+'×');
}

// The picture scrubs: its whole width is the whole video; a tap plays.
v.addEventListener('pointerdown',e=>{
  if(e.button!==undefined&&e.button!==0) return;
  const x0=e.clientX, t0=v.currentTime||0;
  const width=(v.getBoundingClientRect().width)||1;
  let moved=false;
  const was=!v.paused;
  if(v.setPointerCapture) v.setPointerCapture(e.pointerId);
  const move=ev=>{
    if(!moved&&Math.abs(ev.clientX-x0)<6) return;
    if(!moved){moved=true; v.pause();}
    v.currentTime=Math.min(dur,Math.max(0,t0+(ev.clientX-x0)/width*dur));
  };
  const up=()=>{
    v.removeEventListener('pointermove',move);
    v.removeEventListener('pointerup',up);
    v.removeEventListener('pointercancel',up);
    if(!moved){ if(was) v.pause(); else v.play().catch(()=>{}); }
  };
  v.addEventListener('pointermove',move);
  v.addEventListener('pointerup',up);
  v.addEventListener('pointercancel',up);
});

function zoomBy(f,cx){
  const old=zoom;
  zoom=Math.min(64,Math.max(1,zoom*f));
  if(zoom===old) return;
  const r=wrap.getBoundingClientRect();
  const x=cx==null?r.width/2:cx-r.left;
  const frac=(wrap.scrollLeft+x)/(track.offsetWidth||1);
  track.style.width=(zoom*100)+'%';
  wrap.style.overflowX=zoom>1?'auto':'hidden';
  wrap.scrollLeft=frac*track.offsetWidth-x;
  drawStrip(); playhead(false);
}
wrap.addEventListener('wheel',e=>{
  if(!e.ctrlKey) return;
  e.preventDefault(); zoomBy(e.deltaY<0?1.25:0.8,e.clientX);
},{passive:false});
const touches=new Map(); let pinch=0;
wrap.addEventListener('pointerdown',e=>{
  if(e.pointerType==='touch') touches.set(e.pointerId,e.clientX);},true);
wrap.addEventListener('pointermove',e=>{
  if(!touches.has(e.pointerId)) return;
  touches.set(e.pointerId,e.clientX);
  if(touches.size!==2) return;
  const [a,b]=[...touches.values()], d=Math.abs(a-b);
  if(pinch&&d) zoomBy(d/pinch,(a+b)/2);
  pinch=d;
});
const lift=e=>{touches.delete(e.pointerId); if(touches.size<2) pinch=0;};
wrap.addEventListener('pointerup',lift);
wrap.addEventListener('pointercancel',lift);

// I and O mean the clip in hand: the new one while making one, the picked
// one otherwise — and with neither, I starts a new clip here.
function markIn(){
  const c=selected(), t=here();
  if(newMode||!c) return newStart(t);
  if(c.end===c.start) return;
  setEnds(c,t,c.end);
}
function markOut(){
  const c=selected(), t=here();
  if(newMode) return newEnd(t);
  if(!c||c.end===c.start){say('pick a clip, or start a new one',true);return;}
  setEnds(c,c.start,t);
}

const on=(id,fn)=>{const b=$(id); if(b) b.onclick=e=>{e.stopPropagation(); fn();};};
on('bplay',toggle); on('bstill',still); on('bhide',hide);
on('bnew',()=>newMode?cancelNew():startNew());
on('bnewin',()=>newStart(here())); on('bnewout',()=>newEnd(here()));
on('bnewcancel',cancelNew);
on('bprevf',()=>step(-frame)); on('bnextf',()=>step(frame));
on('bprevk',()=>toKey(-1)); on('bnextk',()=>toKey(1));
on('bfirstk',()=>toKeyEnd(-1)); on('blastk',()=>toKeyEnd(1));
on('bstart',markIn); on('bend',markOut);
on('bsplit',split); on('bjoin',join); on('bdel',del);
on('bundo',undo); on('bdiscard',discard); on('bsave',save);
on('zin',()=>zoomBy(2)); on('zout',()=>zoomBy(0.5));
document.addEventListener('keydown',e=>{
  const tag=e.target&&e.target.tagName;
  if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT') return;
  const k=e.key;
  if((e.ctrlKey||e.metaKey)&&(k==='z'||k==='Z')){undo(); e.preventDefault(); return;}
  if((e.ctrlKey||e.metaKey)&&(k==='s'||k==='S')){save(); e.preventDefault(); return;}
  if(e.ctrlKey||e.metaKey||e.altKey) return;
  if(k===' ') toggle();
  else if(k==='n'||k==='N') newMode?cancelNew():startNew();
  else if(k==='i'||k==='I') markIn();
  else if(k==='o'||k==='O') markOut();
  else if(k==='s'||k==='S') split();
  else if(k==='j'||k==='J') join();
  else if(k==='p'||k==='P') still();
  else if(k==='Home'){e.preventDefault(); toKeyEnd(-1);}
  else if(k==='End'){e.preventDefault(); toKeyEnd(1);}
  else if(k===','&&!e.shiftKey) step(-frame);
  else if(k==='.'&&!e.shiftKey) step(frame);
  else if(k==='<'||k===',') toKey(-1);
  else if(k==='>'||k==='.') toKey(1);
  else if(k==='['||k===']') nudgeRate(k===']'?1:-1);
  else if(k==='Delete'||k==='Backspace') del();
  else if(k==='h'||k==='H') hide();
  else if(k==='Escape'){ if(newMode) cancelNew(); else {sel=null; draw();} }
  else return;
  e.preventDefault();
});
function fromHash(){
  let want='';
  try{want=decodeURIComponent(String(location.hash||'').slice(1));}catch(e){}
  if(want&&draft.some(c=>c.id===want)) pick(want);
}
// While a saved clip is still being cut, and nothing is unsaved, look again
// now and then so its dashes resolve on their own.
setInterval(async()=>{
  if(busy||changes()||!saved.some(c=>c.cut===false&&c.end>c.start)) return;
  const list=await fetchSaved();
  if(list&&!changes()){ fromServer(list); draw(); }
},4000);
drawHide(); draw();
})();
