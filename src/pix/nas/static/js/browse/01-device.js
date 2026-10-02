// --- what kind of thing is this being used on --------------------------------
// Asked once, of the browser rather than of its name. A user-agent string says
// what somebody wants you to believe; `pointer: coarse` says a finger is what
// will be aiming at these twenty pixels, which is the only thing any of this
// turns on.
//
// `typeof` because the page script is run under a DOM stub in the tests, where
// `matchMedia` does not exist — and a bare reference would throw on load and
// take every handler on the page with it, which is the exact failure the stub
// was written to catch.
function media(q){
  try{ return typeof matchMedia==='function'&&matchMedia(q).matches; }
  catch(e){ return false; }
}
const COARSE=media('(pointer: coarse)');
// Whether the file can be handed to the system rather than downloaded. On iOS
// this is the only route into Photos at all: a download — in Safari, and
// assuming it happens at all in an installed app — lands in Files and nowhere
// else. Feature-tested, so a desktop browser without it simply never takes
// this path.
const CAN_SHARE=(typeof navigator!=='undefined'
                 &&typeof navigator.canShare==='function'
                 &&typeof navigator.share==='function');

let cells=[...document.querySelectorAll('.cell')];
let cur=-1, anchor=-1, busy=false;
// Keyed by element rather than index: cells leave the grid when a change
// pushes them out of the filters, and indices would then quietly re-point
// a selection at whatever slid into the gap.
const picked=new Set();

