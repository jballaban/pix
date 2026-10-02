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
// The grid's section headings — the h3 headings over each run of
// thumbnails. Only those: the account menu has `.group` rows of its own, and
// asking for `.group` alone counted them as empty sections and removed the
// menu's whole panel with them.
function headings(){
  return [...document.querySelectorAll('.group')].filter(h=>h.tagName==='H3');
}

function resection(){
  // Thumbnails only. A folder page's sections hold folders, which are not
  // cells — counted as cells every section is empty, and removed: the
  // landing page drew its folders and then took every one of them away.
  if(FOLDERS) return;
  headings().forEach(h=>{
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
  headings().forEach(h=>{
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
headings().forEach(wireHeading);
resection();

