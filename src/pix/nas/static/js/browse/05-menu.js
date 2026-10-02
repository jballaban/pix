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

