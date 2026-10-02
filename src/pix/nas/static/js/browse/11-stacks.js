// --- stacks ------------------------------------------------------------------
// Eight takes of one photograph, one shown and the rest folded behind it. Each
// of the others records which file it defers to; the top records nothing,
// because being spoken for is the decision and speaking is what is left.
function keyOf(c){ return c.dataset.folder+'/'+c.dataset.name; }
// **Decided**, and only decided. Taking a file out of a stack is undoing a
// decision, and there is no decision to undo about a guess — refusing is what
// a guess answers to.
function stacked(c){ return !!c.dataset.under; }
// In a stack at all, whoever put it there. Which of these photographs should
// be the one that shows is the same question either way, and answering it is
// what turns a guess into a decision.
function inStack(c){ return !!(c.dataset.under||c.dataset.proposedUnder); }
// Anything folded behind this one, however it got there. A guessed stack opens
// like a decided one — the question *which of these do I keep* is the same
// question, and the answer to it is what turns one into the other.
function tops(c){ return +(c.dataset.behind||0)+ +(c.dataset.proposed||0) > 0; }
function guessed(c){ return +(c.dataset.proposed||0) > 0; }
// In a guessed group, speaking for it or standing in it. *This one does not
// belong* is a thing to be able to say about any of them, and it was only
// askable of the one the shelf is drawn on — so opening a guess and pointing
// at the photograph that does not fit left nothing to press.
function inGuess(c){ return guessed(c) || !!c.dataset.proposedUnder; }
// Whether the stack this file belongs to is already open on the page — inside
// `?within=…`, or grouped by stack. The server reads it the same way, and the
// two have to agree: what it means is *the members are in front of the
// curator*, which decides both whether a write has to follow them and whether
// the page has anything left to fetch.
// Whether the members of a stack are on the page rather than folded away.
// One stack has a page of its own; the shelf is the grid grouped by stack,
// which opens every one of them in the view at once.
const OPENED = !!STACK || GROUPING.includes('stack');
// Whether the rest of this file's stack is on the page with it. Naming a top
// writes to the others, so the gesture can only be offered where the others
// are — which is every open view, and is never the folded grid, where not
// having them on screen is the whole point of a stack.
function familyOn(c){
  const key=stackKey(c);
  return !!key&&cells.some(o=>o!==c&&stackKey(o)===key);
}

// Stacking asks which one to show, rather than taking the first ticked and
// hoping. The rule was invisible: nothing on screen said that the order you
// happened to click in had decided which photograph spoke for the rest.
//
// So the grid narrows to the files being stacked and waits. No round trip and
// no address of its own — they are already on screen, so *filter to these* is
// hiding the others and *back to where you were* is showing them again, with
// the scroll never having moved.
let choosing=null, fetched=[], opened=[], wasPicked=[], wasScrolled=0;
// The pills saying which files were speaking. Borrowed for the question and
// taken back with everything else it borrowed.
let wereTops=[];
let choiceBtns=[];

// Merging two stacks has to offer every photograph in both of them as the one
// to show. Choosing between the two that happen to be speaking is choosing
// between two of ten, and the other eight are only hidden because stacking
// them is what hid them.
// A stack says *these are the same shot*, so it cannot hold two kinds of
// thing: a photograph and a clip are not the same shot whatever else they
// share, and neither can speak for the other. The server refuses it too —
// this is so that nobody is asked which of them to show first.
function mixed(cs){
  return new Set(cs.map(c=>c.dataset.kind||'')).size>1;
}
const MIXED='a stack is one shot — photographs and video cannot be stacked '
           +'together';
// Video does not stack at all, for now (spec/clips.md §4): the server refuses
// it, and the bar does not offer it.
function hasVideo(cs){
  return cs.some(c=>c.dataset.kind==='video');
}
const NO_VIDEO='video cannot be stacked yet';

async function stackSelection(){
  const cs=targetsOn('live');
  // One is enough when that one is a stack, or a guess at one: the question
  // is *which of these shows*, and the photograph a group is drawn as is how
  // you point at the group. The bar has offered this on a single top for as
  // long as `tops` has been in the rule that shows it, and the handler asked
  // for two and said so — a button whose whole answer was that it should not
  // have been pressed, standing on exactly the suggestion somebody was trying
  // to accept.
  if(!cs.length||(cs.length<2&&!tops(cs[0]))){
    say('select the ones to stack');return;}
  if(mixed(cs)){say(MIXED,true);return;}
  if(hasVideo(cs)){say(NO_VIDEO,true);return;}
  choosing=cs; fetched=[]; opened=[]; wasPicked=cs.slice();
  // Where you were, because it is about to be taken from you. Hiding the rest
  // of the grid collapses the page to a few rows, and a browser will not hold
  // a scroll position past the bottom of a document — so it clamps to the top,
  // and putting the cells back afterwards does not put you back with them.
  wasScrolled=window.scrollY;
  const keep=new Set(cs);
  cells.forEach(c=>{c.hidden=!keep.has(c);});
  if(grid) grid.dataset.choosing='1';
  choiceBtns=[];
  // Not `forEach(offerChoice)`: `forEach` hands its callback the index and the
  // array as well, so the second argument became the label and the third the
  // *standing* flag — every candidate's button quietly left off the list
  // `endChoosing` clears, and a refused press left the chooser's controls
  // scattered over the grid behind it.
  cs.forEach(c=>offerChoice(c));
  document.querySelectorAll('.group').forEach(h=>{h.hidden=true;});
  // Nothing is selected while a top is being chosen. The question is *which
  // one of these*, and leaving the files you arrived with ringed while the
  // ones fetched out of a stack are not says they are two kinds of candidate.
  // They are not: any of them can be the one that shows.
  say(''); clearPicks();
  // An open stack brings its members with it rather than fetching them: they
  // are on the page already, and `/api/behind` answers about the stack rather
  // than about the view, so asking put a second copy of every one of them
  // into the grid — the same photograph offered twice as the one to show.
  if(OPENED){
    const keys=new Set(cs.map(stackKey).filter(Boolean));
    for(const c of cells){
      if(keep.has(c)||!keys.has(stackKey(c))) continue;
      c.hidden=false; choosing.push(c); offerChoice(c);
    }
  } else {
    for(const head of cs.filter(c=>tops(c))) await expand(head);
  }
}

async function expand(head){
  let html='';
  try{
    const r=await fetch('/api/behind/'+encodeURIComponent(head.dataset.folder)
                        +'/'+encodeURIComponent(head.dataset.name));
    if(!r.ok) throw new Error(await r.text());
    html=(await r.json()).cells||'';
  }catch(e){say('could not open that stack: '+e.message,true);return;}
  if(!choosing||!html) return;
  // The depth badge means *there are more of these, somewhere else*. Once they
  // are sitting beside it that is no longer true, and leaving it there says
  // the stack is still closed while its files are on screen being chosen
  // between.
  const badge=head.querySelector('.stack');
  if(badge){badge.hidden=true; opened.push(badge);}
  // In the badge's place, what it was really saying. Re-stacking something
  // already stacked asks the question again over every file in it, and this
  // file is the answer that was given last time — worth keeping on screen,
  // because merging three stacks puts three of them among thirty photographs
  // that look alike, and the one to keep is usually one of the three.
  //
  // The same pill an opened stack uses for the same fact, rather than a
  // second way of saying *this is the one that shows*.
  const was=document.createElement('span');
  was.className='top-mark';
  was.title='The one this stack has been showing';
  was.textContent='Top';
  head.appendChild(was);
  wereTops.push(was);
  const holder=document.createElement('div');
  holder.innerHTML=html;
  const added=[...holder.children];
  let after=head;
  for(const c of added){
    after.insertAdjacentElement('afterend',c);
    after=c;
    cells.push(c); choosing.push(c); fetched.push(c); wire(c); useSource(c);
  offerChoice(c);
  }
}

// Clicking a photograph opens it, here as everywhere else. It used to mean
// *this one* while a top was being chosen, and the cost of that was finding
// out by having chosen: you click one to see it properly, and instead the
// question is answered and the grid comes back.
//
// So choosing has a control of its own, on the photograph, appearing when the
// pointer is over it. There is one of them per candidate and one candidate per
// click, which is why it can be a button rather than a mode.
function offerChoice(c,word,standing){
  // Idempotent, because these arrive two ways now: put on every photograph in
  // an opened guess when the page loads, and put on the candidates when a top
  // is being chosen. Pressing Stack inside a guess is both at once, and a
  // second button under the first is two answers to one question.
  if(c.querySelector('.choose')) return;
  const b=document.createElement('button');
  b.className='choose';
  b.textContent=word||'Show this one';
  b.onclick=e=>{e.stopPropagation();chooseTop(c);};
  c.appendChild(b);
  // A standing one is not part of a session and must not be cleared with it:
  // `endChoosing` takes away everything it put up, and that list is how it
  // knows what it put up.
  if(!standing) choiceBtns.push(b);
}

// A guess, refused. Remembered against every photograph in it, so the same
// group is not offered again tomorrow, and recorded like any other decision —
// which is the way back when a shelf of them is waved off by mistake.
//
// What it was hiding comes back out onto the page. In a view of nothing but
// guesses there is nothing to come back to: the whole section stops matching
// and leaves, which the server already reports. In the mixed view the files
// are still here and still match, and leaving them off the grid until the next
// reload would be the page quietly holding some of the library back.
// Refusing the whole group and refusing one photograph of it are the same
// write: `no_stack` is a fact about a file, and the server carries it to the
// members of anything the file speaks for. Said of the one that speaks it
// takes the group with it; said of one standing in it, that one leaves and
// the rest are still a guess.
// A stack of one is not a stack, so once a page is down to that there is
// nothing left in it to ask about. Refusing the whole group empties it,
// refusing all but one leaves nothing to compare, and taking the top out of a
// decided stack dissolves it — three ways to the same place, which is out.
function stillAStack(){
  if(STACK&&cells.length<2) leaveStack(STACK);
}

async function notAStack(){
  let cs=targetsOn('live').filter(inGuess);
  if(!cs.length){say('nothing selected that the app guessed at');return;}
  // **On a stack's page, refusing the one that speaks names the rest.**
  //
  // The server deliberately does not follow the members of an opened stack,
  // on the grounds that they are in front of the curator and following them
  // would be a second write to a file they can see they picked. They are in
  // front of them; they were not necessarily *picked*. Said of the photograph
  // the group is drawn as, the answer is about the group — and left to
  // itself the rest would re-form behind a new leader and be offered again
  // tomorrow, which is the one thing refusing a guess is supposed to stop.
  if(STACK&&cs.some(guessed)) cs=cells.filter(inGuess);
  // Only in the ordinary view. Where the view is *only stacks* or *only
  // suggested*, refusing takes the whole thing out of it — the server says so
  // and the grid drops it — so there is nothing to fan back out into.
  // Only what was hiding something has anything to fan back out.
  //
  // And never on a stack's page, where nothing is hidden: the photographs a
  // refusal releases are the ones already standing on it, and fetching them
  // put a second copy of every one of them into the grid — refuse three and
  // watch five arrive.
  const fan=(!STACK&&!VIEW.stacks)
    ? new Map(await Promise.all(
        cs.filter(guessed).map(async c=>[c,await behind(c)])))
    : null;
  const out=await applyToSelection('no_stack',true,undefined,cs);
  if(!out||!out.done) return;
  if(fan) cs.forEach(c=>fanOut(c,fan.get(c)));
  stillAStack();
}

// Fetched before the refusal is written: afterwards they are nothing's
// members, and the page would have no way left to ask what it had been hiding.
async function behind(head){
  try{
    const r=await fetch('/api/behind/'+encodeURIComponent(head.dataset.folder)
                        +'/'+encodeURIComponent(head.dataset.name));
    if(!r.ok) throw new Error(await r.text());
    return (await r.json()).cells||'';
  }catch(e){say('could not open that stack: '+e.message,true);return '';}
}

function fanOut(head,html){
  if(!html) return;
  const holder=document.createElement('div');
  holder.innerHTML=html;
  const added=[...holder.children];
  let after=head;
  for(const c of added){after.insertAdjacentElement('afterend',c);after=c;}
  // Spliced where they sit rather than appended: `cells` is the reading order
  // the viewer and the arrow keys walk, and a file that is on screen here and
  // last in the order is a preview that opens the wrong photograph.
  const at=cells.indexOf(head);
  cells.splice(at<0?cells.length:at+1,0,...added);
  added.forEach(c=>{wire(c);useSource(c);});
  // In the view now, and in the server's order ahead of the next page.
  served+=added.length; total+=added.length;
  const badge=head.querySelector('.stack');
  if(badge) badge.remove();
  head.dataset.proposed='0';
  head.classList.remove('marked');
  resection(); drawSel();
}

