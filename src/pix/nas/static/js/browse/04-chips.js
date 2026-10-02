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

