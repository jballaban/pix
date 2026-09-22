// Saving a photograph on a phone, actually executed.
//
// On a desktop the browser owns a download and the page just points it at a
// URL. On a phone that lands the file in Files if it lands anywhere at all,
// and the whole point of the library is the photographs being in Photos — so
// the bytes come through the page and go out through the share sheet instead.
//
// Worth driving rather than reading, because every part of it is a consequence:
// which of the two paths a press takes, that the sheet is opened from its own
// tap rather than from the one that started the fetch, and that a selection too
// big to hold in memory turns back into an ordinary download rather than into
// a dead button.
//
// Usage: node save.js <path to the extracted browse script>
'use strict';
const fs = require('fs');
const { El, document } = require('./dom.js');

// The stub's `focus` does nothing, which is exactly what a phone must not be
// asked to do: raising the keyboard is the whole question. Recorded, and it
// gives the page an `activeElement` to ask about too.
const focused = [];
El.prototype.focus = function focus() {
  focused.push(this);
  document.activeElement = this;
};

const js = fs.readFileSync(process.argv[2], 'utf8');
const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail ? ': ' + detail : ''));
}

// --- the page ----------------------------------------------------------------
function mk(id, cls) {
  const e = new El('div');
  e.id = id;
  if (cls) e.className = cls;
  document.byId[id] = e;
  document.appendChild(e);
  return e;
}

function cell(name, copy) {
  const c = new El('div');
  c.className = 'cell';
  Object.assign(c.dataset, {
    folder: 'f', name, kind: 'image', audience: '', tags: '',
    date: '2026-01-01', copy: copy || '',
  });
  const pick = new El('button');
  pick.className = 'pick';
  c.appendChild(pick);
  return c;
}

const grid = mk('grid');
// Three, not two: a range across adjacent circles and two separate taps on
// them are the same answer, so a two-cell grid cannot tell a hold that was
// cancelled from one that was not.
const cells = [cell('a.jpg'), cell('b.heic'), cell('c.jpg')];
cells.forEach(c => grid.appendChild(c));

const actions = mk('actions');
const tick = new El('button');
tick.id = 'selall';
tick.className = 'tick';
document.byId.selall = tick;
actions.appendChild(tick);
const live = new El('span');
live.className = 'grp';
live.dataset.side = 'live';
for (const act of ['event', 'tags', 'date', 'access', 'stack', 'top',
                   'unstack', 'nostack', 'download', 'delete']) {
  const b = new El('button');
  b.dataset.act = act;
  b.textContent = act === 'download' ? 'Download' : act;
  live.appendChild(b);
}
actions.appendChild(live);
const actBtn = name => actions.querySelectorAll('[data-act]')
                              .find(b => b.dataset.act === name);

for (const id of ['menu', 'chips', 'selcount', 'count', 'note', 'viewer',
                  'vimg', 'vvid', 'vmeta', 'rail', 'railtoggle', 'viewclose',
                  'viewget', 'sizepick', 'working', 'workwhat', 'workbar',
                  'worktally', 'workstop', 'worksave', 'bincount']) mk(id);
const stage = new El('div');
stage.className = 'stage';
document.byId.viewer.appendChild(stage);
Object.assign(document.byId.vvid,
              { pause() {}, load() {}, play: () => Promise.resolve() });

const realQsa = document.querySelectorAll.bind(document);
document.querySelectorAll = sel => (sel === '.cell' ? cells
                                  : sel === '.stage' ? [stage] : realQsa(sel));
document.querySelector = sel => document.querySelectorAll(sel)[0] || null;

// --- a phone -----------------------------------------------------------------
// Coarse, and able to hand a file to the system. Both are asked of the browser
// rather than of its name, so both are stubbed the way a browser answers them.
let coarse = true;
const matchMedia = q => ({
  matches: q === '(pointer: coarse)' ? coarse
         : q === '(max-width: 720px)' ? window.innerWidth <= 720
         : false,
});

// What each fetched file claims to weigh, so the ceiling can be driven without
// allocating a hundred megabytes to reach it.
let weigh = 1000;
const fetched = [];
const fetch = async (url) => {
  fetched.push(url);
  if (url.startsWith('/api/suggest')) {
    return { ok: true, json: async () => [{ value: 'ghost', n: 1, scope: 'all' }] };
  }
  return {
    ok: true,
    headers: { get: k => (k === 'content-length' ? String(weigh) : null) },
    blob: async () => new Blob([new Uint8Array(8)]),
    json: async () => ({ name: 'a.jpg', exif: {}, facts: [] }),
  };
};

// What the sheet was given, and whether it would take it.
let shared = null;
let takes = true;
let refuse = null;
const navigator = {
  canShare: spec => takes && !!(spec && spec.files && spec.files.length),
  share: spec => {
    if (refuse) { const e = refuse; refuse = null; return Promise.reject(e); }
    shared = spec;
    return Promise.resolve();
  },
};

const stored = {};
const localStorage = { getItem: k => (k in stored ? stored[k] : null),
                       setItem: (k, v) => { stored[k] = String(v); } };
const listeners = {};
const window = { innerWidth: 390, scrollY: 0, devicePixelRatio: 3,
                 scrollBy() {}, scrollTo() {},
                 addEventListener: (t, fn) => ((listeners[t] ||= []).push(fn)) };
let went = null;
const location = { get href() { return went === null ? '/browse' : went; },
                   set href(v) { went = v; }, reload() {} };
const confirm = () => true;

const VIEW = { event: null, year: null, tag: null, audience: null, kind: null,
               band: null };
const GRID_GROUPS = [['day', 'By day'], ['none', 'Ungrouped']];
const ONE_FIELD = { event: 'event', subevent: 'event' };
const GROUPING = ['day'];
const CHIPS = [['event', 'Event']];
const FIXED = {};
const EXTRA = {};
const ADMIN = true;
const USERS = ['family'];
const GROUPS = ['family'];
const USUAL = 'family';
// The audience value meaning *nobody yet*, as the server sends it.
const UNREVIEWED = 'new';
// What separates an event from a sub-event in one name.
const EVENT_SEP = ' > ';
const NO_EVENT = '(none)';
const PAGE = '/browse';
const TIERS = [['/thumb/', 400], ['/large/', 1000], ['/preview/', 1600]];

// Everything the page defers runs at once, which is what every other driver
// here assumes — except the press-and-hold, which is *about* the delay: the
// whole question is whether the finger was still down. Those are held back so
// a test can answer it, and cancelling one has to work, or every tap on a
// circle becomes a range.
const pending = [];
const timer = (fn, ms) => {
  if (ms >= 200) { const h = { fn, live: true }; pending.push(h); return h; }
  fn();
  return null;
};
const cancelTimer = h => { if (h && h.live !== undefined) h.live = false; };
const heldOut = () => pending.splice(0).forEach(h => { if (h.live) h.fn(); });

const settle = () => new Promise(r => setImmediate(r));
// The fetch is a run of awaits, so a few turns of the loop rather than one.
const settled = async () => { for (let i = 0; i < 12; i++) await settle(); };

const PARAMS = ['document', 'window', 'fetch', 'localStorage', 'location',
                'confirm', 'matchMedia', 'navigator', 'VIEW', 'CHIPS', 'FIXED',
                'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL', 'GRID_GROUPS', 'ONE_FIELD',
                'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'NO_EVENT', 'setTimeout', 'clearTimeout'];
function run() {
  new Function(...PARAMS, js)(
    document, window, fetch, localStorage, location, confirm, matchMedia,
    navigator, VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
    GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, NO_EVENT, timer, cancelTimer);
}

(async () => {
  try { run(); } catch (e) {
    console.log('FAIL the script threw on load: ' + e.message);
    process.exit(1);
  }

  const save = document.byId.worksave;

  // --- the word ---------------------------------------------------------------
  // The sheet offers Photos and Files both, and *Download* names one of them.
  check('the button says what the sheet actually does',
        actBtn('download').textContent === 'Save',
        actBtn('download').textContent);

  // --- one photograph ---------------------------------------------------------
  cells[0].children[0].click();
  actBtn('download').click();
  await settled();

  check('the file is fetched into the page rather than navigated to',
        fetched.length === 1 && fetched[0] === '/download/f/a.jpg',
        fetched.join(', '));
  check('and the page itself does not go anywhere', went === null, String(went));
  check('nothing has been handed to the sheet yet', shared === null);
  check('because that is a second press, and here is the button for it',
        save.hidden === false, 'hidden=' + save.hidden);

  save.click();
  await settled();
  check('which opens the sheet', shared !== null);
  check('with the file in it',
        shared && shared.files.length === 1
        && shared.files[0].name === 'a.jpg',
        shared && JSON.stringify(shared.files.map(f => f.name)));
  check('named as the kind of thing it is, or Photos will not offer to keep it',
        shared && shared.files[0].type === 'image/jpeg',
        shared && shared.files[0].type);
  check('and the button is gone once it has been used', save.hidden === true);

  // --- what the sheet refuses --------------------------------------------------
  // Closing it is an answer, not a fault.
  shared = null; fetched.length = 0;
  refuse = Object.assign(new Error('cancelled'), { name: 'AbortError' });
  actBtn('download').click();
  await settled();
  save.click();
  await settled();
  check('dismissing the sheet says nothing about it',
        document.byId.note.textContent === ''
        || document.byId.note.hidden === true,
        document.byId.note.textContent);

  // --- a heic keeps its own name -----------------------------------------------
  shared = null; fetched.length = 0;
  cells[0].children[0].click();   // off
  cells[1].children[0].click();   // on
  actBtn('download').click();
  await settled();
  save.click();
  await settled();
  check('an iPhone photograph is offered to the sheet as one',
        shared && shared.files[0].type === 'image/heic',
        shared && shared.files[0].type);

  // --- more than can be held ---------------------------------------------------
  // Read off the header before a single byte is buffered, so a selection too
  // big to hold is turned down rather than held and then dropped.
  shared = null; fetched.length = 0; went = null;
  weigh = 200 * 1024 * 1024;
  actBtn('download').click();
  await settled();
  check('too big for the sheet, so the ordinary download happens instead',
        went === '/download/f/b.heic', String(went));
  check('and it says where the file is going to land',
        /Files/.test(document.byId.note.textContent),
        document.byId.note.textContent);
  check('rather than leaving a Save button that cannot work',
        save.hidden === true);
  weigh = 1000;

  // --- a type no phone has an opinion about ------------------------------------
  shared = null; fetched.length = 0; went = null; takes = false;
  actBtn('download').click();
  await settled();
  check('a file the sheet will not take is still a file you can download',
        went === '/download/f/b.heic', String(went));
  takes = true;

  // --- a selection bigger than the sheet is for --------------------------------
  shared = null; fetched.length = 0; went = null;
  document.submitted = [];
  cells[0].children[0].click();   // both now
  check('two are selected',
        document.byId.selcount.textContent === '2 selected',
        document.byId.selcount.textContent);
  actBtn('download').click();
  await settled();
  save.click();
  await settled();
  check('a handful goes to the sheet together',
        shared && shared.files.length === 2,
        shared && shared.files.length);

  // --- long-pressing a photograph -----------------------------------------
  // The stylesheet closes this on iOS with `-webkit-touch-callout`, which
  // Android does not have — so there, long-pressing a thumbnail opens the
  // browser's own image menu and its *Save image* saves the 400px derivative.
  {
    let stopped = 0;
    const menu = () => (grid._listeners.contextmenu || [])
      .forEach(fn => fn({ preventDefault() { stopped += 1; } }));
    menu();
    check('a finger holding a thumbnail is not offered the browser\'s own '
          + 'menu, which would save the derivative', stopped === 1,
          String(stopped));
  }

  // --- a range, without a shift key -------------------------------------------
  // Shift-click is the only way to select more than one at a time, and a phone
  // has no shift key. 61,846 files one circle at a time is not a job anybody
  // finishes, which made this the difference between the library being
  // cullable on a phone and not.
  {
    const on = c => c.children.find(k => k._classes.has('pick'));
    const fire = (c, type) => (on(c)._listeners[type] || []).forEach(fn => fn({}));
    // A finger that stays down: the hold comes due before it lifts.
    const hold = c => { fire(c, 'touchstart'); heldOut(); fire(c, 'touchend');
                        on(c).click(); };
    // And one that does not: down, up, and the hold was cancelled on the way.
    const tap = c => { fire(c, 'touchstart'); fire(c, 'touchend'); heldOut();
                       on(c).click(); };
    const count = () => document.byId.selcount.textContent;

    if (count() !== '0 selected') tick.click();
    tap(cells[0]);
    check('a tap on a circle ticks one', count() === '1 selected', count());
    hold(cells[2]);
    check('and holding another takes everything between it and the last',
          count() === '3 selected', count());
    check('the far end included, rather than ticked by the press and unticked '
          + 'again by the tap that ends it', cells[2]._classes.has('picked'));

    // The cancel is the part that has to work: without it every tap is a
    // range, and one gesture doing both jobs is worse than only shift-click
    // doing one of them.
    tick.click();
    tap(cells[0]);
    tap(cells[2]);
    check('two taps two apart tick two, not the one between them',
          count() === '2 selected', count());
    check('which is the whole of the difference a cancelled hold makes',
          !cells[1]._classes.has('picked'));
    tap(cells[2]);
    check('and a tap unticks again', count() === '1 selected', count());
    tick.click();
  }

  // --- swiping between photographs --------------------------------------------
  // The arrow keys are the desktop's answer and a phone has none, so without
  // this the only way from one photograph to the next is to close the viewer,
  // find the thumbnail after it, and open that.
  {
    const viewer = document.byId.viewer;
    const touch = (type, x, y) => (stage._listeners[type] || []).forEach(fn =>
      fn(type === 'touchend'
         ? { changedTouches: [{ clientX: x, clientY: y }] }
         : { touches: [{ clientX: x, clientY: y }] }));
    const swipe = (fromX, toX, dy) => {
      touch('touchstart', fromX, 200);
      touch('touchend', toX, 200 + (dy || 0));
    };

    // The tick clears when anything is ticked, and the sections above left a
    // selection behind.
    if (document.byId.selcount.textContent !== '0 selected') tick.click();
    cells[0].click();
    check('the viewer is open on the first photograph',
          viewer.classList.contains('on') && cells[0]._classes.has('cur'));

    swipe(300, 120);
    check('a swipe to the left moves on', cells[1]._classes.has('cur'));
    swipe(120, 300);
    check('and back to the right moves back', cells[0]._classes.has('cur'));

    // More down than across is somebody scrolling, not turning a page.
    swipe(300, 240, 120);
    check('a mostly-vertical drag is not a page turn',
          cells[0]._classes.has('cur'));
    // And a tap is not a swipe.
    swipe(300, 288);
    check('nor is a short one', cells[0]._classes.has('cur'));

    // The edges belong to the system: that is how you go back, and an app
    // that takes the gesture over is one you cannot get out of.
    swipe(10, 300);
    check('a drag from the screen edge is left alone',
          cells[0]._classes.has('cur'));

    check('and looking still decides nothing',
          document.byId.selcount.textContent === '0 selected',
          document.byId.selcount.textContent);
    document.byId.viewclose.click();
  }

  // --- coming back to a page the browser kept ---------------------------------
  // Leaving for the share sheet and returning can strand the takeover over the
  // whole screen with nothing that dismisses it: Stop only acts while a write
  // is running, and by then none is.
  {
    const working = document.byId.working;
    working.classList.add('on');
    document.byId.viewer.classList.add('on');
    (listeners.pageshow || []).forEach(fn => fn({ persisted: true }));
    check('a restored page is not still covered by the takeover',
          !working.classList.contains('on'));
    check('nor by the viewer',
          !document.byId.viewer.classList.contains('on'));
  }

  // --- the menu and the keyboard ----------------------------------------------
  // Opening a menu focused its filter box, the phone scrolled the document to
  // bring the field into view, and the page closes a menu on any scroll — so
  // every Event, Tags, Date and Access opened and vanished. Nothing was broken
  // and none of it worked.
  {
    const menu = document.byId.menu;
    focused.length = 0;
    document.activeElement = null;
    cells[0].children[0].click();
    actBtn('tags').click();
    await settled();

    check('the menu is open', menu.hidden === false);
    check('and nothing asked the phone for its keyboard',
          focused.length === 0, focused.map(e => e.id).join(', '));

    // The browser scrolling the page to reveal a field the reader is typing in
    // is not the reader moving on.
    document.activeElement = document.byId.menuq;
    (listeners.scroll || []).forEach(fn => fn());
    check('a scroll with the cursor still in the menu leaves it open',
          menu.hidden === false);

    // And a real one still dismisses it.
    document.activeElement = null;
    (listeners.scroll || []).forEach(fn => fn());
    check('a scroll of the page itself still closes it', menu.hidden === true);
    cells[0].children[0].click();   // back to nothing selected
  }

  // --- what a phone actually downloads to fill a cell --------------------------
  // The grid was reading the biggest derivative in the library into a cell the
  // width of a thumb. A phone reports three device pixels per CSS pixel, so a
  // 114px cell asked for 342 — past what the 400px tier gives a 4:3 frame on
  // its short edge — and every cell came from `large` at a thousand pixels.
  {
    const img = new El('img');
    img.attrs.src = '/thumb/f/a.jpg';
    img.setAttribute = (k, v) => { img.attrs[k] = v; };
    img.getAttribute = k => img.attrs[k];
    const shown = document.querySelectorAll('.cell')[0];
    shown.appendChild(img);
    shown.dataset.ar = '0.75';
    // Three columns of a 393px screen, which is what the narrow grid gives.
    shown._rect = { left: 0, top: 0, width: 114, height: 114 };

    // There is one control and it cycles, so three presses redraw three times
    // and land back where they started. Pressed rather than merely read: a
    // check on the source the page was *rendered* with proves nothing about
    // the one it would choose.
    const round = () => { for (let i = 0; i < 3; i++) document.byId.sizepick.click(); };
    round();
    check('the grid is back at the size it started', grid.dataset.size === 'small',
          grid.dataset.size);
    check('and a phone fills a thumb-sized cell from the thumbnail tier',
          img.attrs.src === '/thumb/f/a.jpg', img.attrs.src);

    // The cap is about the screen, not about the pointer: a desk with a retina
    // display and the same cell should still get every pixel it can show.
    window.innerWidth = 1400;
    round();
    check('a wide retina screen is not capped and reads the tier above',
          img.attrs.src === '/large/f/a.jpg', img.attrs.src);
    window.innerWidth = 390;
    round();
    check('and back on the phone it goes back down',
          img.attrs.src === '/thumb/f/a.jpg', img.attrs.src);
    img.remove();
    delete shown._rect;
    delete shown.dataset.ar;
  }

  // --- and on a desktop, none of this ------------------------------------------
  coarse = false;
  shared = null; fetched.length = 0; went = null;
  document.submitted = [];
  // The server renders this word; a real desktop page never had it changed in
  // the first place, and only this test runs two pages over one document.
  actBtn('download').textContent = 'Download';
  run();
  check('the word goes back',
        actBtn('download').textContent === 'Download',
        actBtn('download').textContent);
  cells[0].children[0].click();
  actBtn('download').click();
  await settled();
  check('nothing is fetched into the page', fetched.length === 0,
        fetched.join(', '));
  check('the browser is pointed at the file and owns the transfer',
        went === '/download/f/a.jpg', String(went));
  check('and the sheet is never involved', shared === null);

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok all save-on-a-phone checks passed');
})();
