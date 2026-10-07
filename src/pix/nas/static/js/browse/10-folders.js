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
  fitActions();
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

