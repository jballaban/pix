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
// An action from the bar, done to the selection. `anchor` is what a menu it
// opens hangs from: the button itself, or the `+` when the button is one of
// the ones folded away behind it.
function barAct(act,anchor){
  if(vActing){vActing=null; closeMenu();}
  if(menu) menu.classList.remove('over');
  runAct(act,anchor);
}
(actions?[...actions.querySelectorAll('[data-act]')]:[]).forEach(b=>{
  b.onclick=e=>{e.stopPropagation(); barAct(b.dataset.act,b);};
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

