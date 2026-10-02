// --- thumbnail size ----------------------------------------------------------
// A preference about looking, not about which photographs — so it lives in the
// browser rather than the URL, beside the details rail. A view is a link; how
// big you like the thumbnails is not part of where you are.
// Small, medium, large. The letters and the words for them are in the markup
// the server sends — the script only has to know which names are real, so a
// stale value in `localStorage` cannot put the grid into a size that has no
// rule behind it.
const SIZES=['small','medium','large'];
let thumbSize='small';
try{
  const saved=localStorage.getItem('pix2.thumb');
  // `big` and `huge` are what these were called for an afternoon.
  const known={big:'medium',huge:'large'}[saved]||saved;
  if(SIZES.includes(known)) thumbSize=known;
}catch(e){}

// The biggest size is bigger than the thumbnail tier has pixels for, so it
// reads from `large` — sized for exactly this and nothing else. The media
// routes take the same path after the tier name, so this swaps one segment
// rather than building an address a second time.
// How wide a cell is drawn, in the pixels the screen actually has. Measured
// once rather than per cell: every cell in the grid is the same width, and
// asking two thousand of them costs a layout each.
// Capped at two on a phone, and it is not a compromise about sharpness.
// A modern phone reports three, so a 174px cell asked for 520 real pixels,
// which no thumbnail has — and every cell in the grid came from `large` at a
// thousand pixels, or from `preview` at sixteen hundred at the other two
// sizes. The biggest derivative in the library, ten times the bytes it can
// show, over wifi or a VPN, on the most memory-constrained thing in the house.
// Two is past the point anyone can see on a cell this size and it is what the
// thumbnail tier was built to cover.
function cellPixels(){
  const c=cells.find(x=>!x.hidden)||cells[0];
  const w=c?c.getBoundingClientRect().width:0;
  const dpr=window.devicePixelRatio||1;
  return Math.round((w||150)*(media('(max-width: 720px)')?Math.min(dpr,2):dpr));
}

// The smallest tier that can fill it. Not a fixed tier per size: the same
// grid on a retina screen needs twice the pixels for the same inch of glass,
// and reading `large` there was asking a 563-pixel square to cover 830 — soft
// in exactly the way a photograph never is in the viewer.
// With a fifth to spare, not to the pixel. A source that only just covers the
// cell is being shown at very nearly 1:1, which on a video frame — already
// soft, already compressed once — looks nothing like the same cell filled
// from a photograph with half again as many pixels to give away. The margin
// is what makes the two look alike.
const SPARE=1.2;

function sourceFor(c,px){
  const ar=+(c.dataset.ar||1)||1;
  for(const [dir,cap] of TIERS) if(cap*ar>=px*SPARE) return dir;
  return TIERS[TIERS.length-1][0];
}

function useSource(c,px){
  const img=c.querySelector('img');
  if(!img) return;
  const have=img.getAttribute('src')||'';
  const at=have.indexOf('/',1);
  if(at<0) return;
  const want=sourceFor(c,px===undefined?cellPixels():px);
  if(!have.startsWith(want)) img.setAttribute('src',want+have.slice(at+1));
}

function drawSize(){
  if(grid) grid.dataset.size=thumbSize;
  // Which one is on, on every copy. `aria-pressed` rather than a class: these
  // are three buttons of which exactly one is the state, which is what that
  // attribute means — and it is what the styling reads, so there is one fact
  // here rather than two that can disagree.
  sizeOpts.forEach(b=>b.setAttribute(
    'aria-pressed',b.dataset.size===thumbSize?'true':'false'));
  // After the grid has been told its new size, or every cell is measured at
  // the width it is about to stop being.
  const px=cellPixels();
  cells.forEach(c=>useSource(c,px));
  if(typeof clampInfo==='function') clampInfo(cells);
}

function chooseSize(next){
  if(!SIZES.includes(next)||next===thumbSize) return;
  // Anchored the way a write is: the row heights are about to change under
  // whatever you were looking at, and the point of a bigger thumbnail is to
  // look harder at the one you had already found.
  const at=cells.find(c=>c.getBoundingClientRect().bottom>0);
  const was=at?at.getBoundingClientRect().top:null;
  thumbSize=next;
  try{localStorage.setItem('pix2.thumb',thumbSize);}catch(e){}
  drawSize();
  if(at&&was!==null){
    const now=at.getBoundingClientRect().top;
    if(now!==was) window.scrollBy(0,now-was);
  }
}

sizeOpts.forEach(b=>{
  // Not letting the click reach the document: the menu copy sits inside the
  // account menu, and the shell dismisses that on any click outside it. This
  // one is inside, but the grid's own handlers are on the way past.
  b.onclick=e=>{e.stopPropagation();chooseSize(b.dataset.size);};
});
drawSize();

