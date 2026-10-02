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

