// --- writing -----------------------------------------------------------------
// Loud, because the alternative has bitten twice: a write that fails without
// saying so is indistinguishable from one that worked, and the curator only
// finds out much later that nothing was recorded.
function say(text,bad){
  if(!note) return;
  note.textContent=text||'';
  note.hidden=!text;
  note.classList.toggle('loud',!!bad);
}

// A page script that throws takes every handler with it and leaves a grid that
// simply ignores clicks. Saying so beats looking broken.
window.addEventListener('error',e=>say('page error: '+e.message,true));
window.addEventListener('unhandledrejection',
  e=>say('page error: '+(e.reason&&e.reason.message||e.reason),true));

// Exactly what is ticked — no implicit extra. A count that says none while
// an action changes something is the one thing a selection must never do.
// What an action acts on: the selection — or, while an action was asked for
// from the viewer, the one photograph on show. One place, so every action,
// its menu and its suggestions agree about which files they mean.
let vActing=null;
function targets(){
  if(vActing) return [...vActing];
  return [...picked];
}

