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

