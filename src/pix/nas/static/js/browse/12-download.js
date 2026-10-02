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
  headings().forEach(h=>{h.hidden=false;});
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

