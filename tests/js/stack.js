// The same script, standing inside an opened stack.
//
// A third stage rather than more of review.js's, because this one is about a
// grid that is *somewhere* — `VIEW.within` set — and about the two ways out of
// it. Both of the things checked here were, until now, one thing: the only way
// to leave a stack was the browser's own back button, and pressing it put a
// photograph on screen that nobody had asked to see.
const fs = require('fs');
const { El, document, sizeset, section } = require('./dom.js');

const js = fs.readFileSync(process.argv[2], 'utf8');
const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail ? ' — ' + detail : ''));
}

function mk(id, cls) {
  const e = new El('div');
  e.id = id;
  if (cls) e.className = cls;
  document.byId[id] = e;
  document.appendChild(e);
  return e;
}

function cell(name, extra) {
  const c = new El('div');
  c.className = 'cell';
  Object.assign(c.dataset, {
    folder: 'f', name, kind: 'image', audience: '', tags: '',
    date: '2026-08-30', under: '', behind: '0', proposed: '0',
  }, extra || {});
  const pick = new El('button');
  pick.className = 'pick';
  c.appendChild(pick);
  return c;
}

const grid = mk('grid');
// The stack as it is seen from inside: the one that speaks for it, carrying
// the badge, and the two it speaks for.
const cells = [cell('lead.jpg', { behind: '2' }),
               cell('one.jpg', { under: 'f/lead.jpg' }),
               cell('two.jpg', { under: 'f/lead.jpg' })];
section(null, cells, grid);

const actions = mk('actions');
const tick = new El('button');
tick.id = 'selall';
tick.className = 'tick';
document.byId.selall = tick;
actions.appendChild(tick);
function actGroup(side, acts) {
  const g = new El('span');
  g.className = 'grp';
  g.dataset.side = side;
  g.hidden = true;
  for (const act of acts) {
    const b = new El('button');
    b.dataset.act = act;
    g.appendChild(b);
  }
  actions.appendChild(g);
  return g;
}
actGroup('live', ['event', 'tags', 'date', 'access', 'stack', 'top', 'unstack',
                  'nostack', 'download', 'delete']);
actGroup('gone', ['restore', 'purge']);
const chooseActs = actGroup('choose', []);
const cancelBtn = new El('button');
cancelBtn.id = 'choosecancel';
document.byId.choosecancel = cancelBtn;
chooseActs.appendChild(cancelBtn);
sizeset();
// No `chips`: a stack's page has no filter bar, and the script has to run
// without one.
for (const id of ['menu', 'selcount', 'count', 'note', 'viewer',
                  'vimg', 'vvid', 'vmeta', 'rail', 'railtoggle', 'viewclose',
                  'working', 'workwhat', 'workbar', 'worktally',
                  'workstop', 'bincount']) mk(id);
const stage = new El('div');
stage.className = 'stage';
document.byId.viewer.appendChild(stage);
Object.assign(document.byId.vvid, {
  pause() {}, load() {}, play: () => Promise.resolve(),
});

const realQsa = document.querySelectorAll.bind(document);
document.querySelectorAll = sel => (sel === '.cell' ? cells
                                  : sel === '.stage' ? [stage] : realQsa(sel));
document.querySelector = sel => document.querySelectorAll(sel)[0] || null;

const calls = [];
let dropping = [];
// What `/api/behind` really answers with: the cells of the files in the
// stack, rendered by the server. On a stack's page they are already on the
// page — which is the whole bug, and a stub that answered with nothing could
// not show it.
const behindCells =
  ['one.jpg', 'two.jpg'].map(n =>
    '<div class="cell" data-folder="f" data-name="' + n + '"'
    + ' data-kind="image" data-audience="" data-event="" data-tags=""'
    + ' data-date="2026-08-30" data-deleted="" data-under="" data-behind="0">'
    + '<button class="pick"></button></div>').join('');
const fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body });
  if (url.startsWith('/api/behind/')) {
    return { ok: true, json: async () => ({ cells: behindCells }) };
  }
  return { ok: true,
           json: async () => ({ name: 'lead.jpg', exif: {}, facts: [],
                                failed: [], dropped: dropping, total: 3 }) };
};
const stored = {};
const localStorage = {
  getItem: k => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
};
const listeners = {};
const window = {
  innerWidth: 1400, scrollY: 0, devicePixelRatio: 1,
  scrollBy: () => {}, scrollTo: () => {},
  addEventListener: (t, fn) => ((listeners[t] ||= []).push(fn)),
};
const location = { href: '/browse?within=f%2Flead.jpg&group=day',
                   reload: () => {} };
// What the browser would do, recorded instead.
let wentBack = 0;
const history = { back: () => { wentBack += 1; } };
const confirm = () => true;
const VIEW = { event: null, date: null, tag: null, audience: null, kind: null,
               band: null, deleted: null, stacks: null, within: 'f/lead.jpg' };
const GRID_GROUPS = [['day', 'By day'], ['stack', 'By stack'],
                     ['none', 'Ungrouped']];
const ONE_FIELD = { event: 'event', subevent: 'event' };
const GROUPING = ['day'];
const CHIPS = [['event', 'Event'], ['stacks', 'Stacks']];
const FIXED = { stacks: [['only', 'Only stacks']] };
const EXTRA = {};
const ADMIN = true;
const USERS = ['family'];
const GROUPS = ['family'];
const USUAL = 'family';
// The audience value meaning *nobody yet*, as the server sends it.
const UNREVIEWED = 'new';
// The audience that takes a file out of every view; global, as in drive.js.
globalThis.HIDDEN = 'hidden';
globalThis.HIDDEN_LABEL = 'Hidden — out of every view';
// What separates an event from a sub-event in one name.
const EVENT_SEP = ' > ';
const NO_EVENT = '(none)';
// Which stack's page this is, and the way out of it. Empty on a grid,
// which is how the script knows it is not on one.
let STACK = 'f/lead.jpg';
let BACK = '/browse?group=day';
const PAGE = '/browse';
const TIERS = [['/thumb/', 400], ['/large/', 1000],
               ['/preview/', 1600]];

const settle = () => new Promise(r => setImmediate(r));
const viewerOpen = () => document.byId.viewer._classes.has('on');

(async () => {
  try {
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'history',
      'confirm', 'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS',
      'GROUPS', 'USUAL', 'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'NO_EVENT', 'STACK', 'BACK',
      'setTimeout', js,
    )(document, window, fetch, localStorage, location, history, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS, ONE_FIELD,
      GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, NO_EVENT, STACK, BACK, fn => fn());
  } catch (e) {
    console.log('FAIL the script threw on load: ' + e.message);
    process.exit(1);
  }

  // --- a page with no filters on it -------------------------------------------
  // Eleven questions about the library, on a page that is eight frames of one
  // moment. The stage below has no `#chips` for exactly that reason, and the
  // script has to be able to run on a page without one — it reached for it
  // instead of asking for it, which on a page that has none took every
  // handler down on load.
  check('the script runs on a page with no filter bar',
        document.byId.chips === undefined || document.byId.chips === null,
        'the stage has a chip bar the real page does not');

  // --- the viewer is still the viewer -----------------------------------------
  cells[1].click();
  await settle();
  check('a plain click on a take opens it', viewerOpen());
  // What a restore from the back/forward cache brings back is the page exactly
  // as it left, open viewer included.
  (listeners.pageshow || []).forEach(fn => fn({ persisted: true }));
  check('and a page restored by going back has it closed', !viewerOpen());

  // --- answering from inside an open stack ------------------------------------
  // The members are on the page: that is what being inside one means. The bar
  // has always offered Stack on a single top, and pressing it asked the server
  // for the files it was already looking at — which put a second copy of every
  // one of them in the grid, the same photograph offered twice as the one to
  // show. The handler then refused the press anyway, for having only one thing
  // selected.
  {
    (listeners.pageshow || []).forEach(fn => fn({ persisted: true }));
    const was = calls.length;
    const before = grid.querySelectorAll('.cell').length;
    const actBtn = name => actions.querySelectorAll('[data-act]')
                                  .find(b => b.dataset.act === name);
    cells[0].querySelector('.pick').click();
    check('the photograph a stack is drawn as is enough to press Stack with',
          actBtn('stack').hidden === false);

    actBtn('stack').click();
    await settle(); await settle();
    check('nothing is fetched for files already on the page',
          !calls.slice(was).some(c => c.url.startsWith('/api/behind')),
          calls.slice(was).map(c => c.url).join(','));
    check('so no photograph is offered twice',
          grid.querySelectorAll('.cell').length === before,
          String(grid.querySelectorAll('.cell').length));
    // Every one of them is a candidate, the one that already shows included
    // — which is how a suggestion is agreed with rather than rearranged.
    const offered = c => c.children.some(k => k._classes.has('choose'));
    check('and each of them offers itself as the one to show',
          cells.every(offered),
          cells.map(c => c.dataset.name + ':' + offered(c)).join(','));

    cancelBtn.click();
    await settle();
    check('and changing your mind puts them all back',
          cells.every(c => c.hidden === false));
  }

  // --- and agreeing leaves, because the answer is out there -------------------
  // Agreeing with a suggestion is the question this page was opened to ask,
  // answered. What changed is in the grid it was folded into, which is still
  // drawing it as a guess — so staying put left the answer invisible until
  // somebody went back by hand and then reloaded, because going back alone is
  // a cached copy of the very page that has just stopped being true.
  {
    const actBtn = name => actions.querySelectorAll('[data-act]')
                                  .find(b => b.dataset.act === name);
    // Cancelling put back what had been ticked before it, which is what
    // cancelling means — so this starts by clearing it.
    if (document.byId.selcount.textContent !== '0 selected') tick.click();
    // The same three files, as a guess rather than as a stack.
    cells[0].dataset.behind = '0';
    cells[0].dataset.proposed = '2';
    cells[1].dataset.under = ''; cells[1].dataset.proposedUnder = 'f/lead.jpg';
    cells[2].dataset.under = ''; cells[2].dataset.proposedUnder = 'f/lead.jpg';
    location.href = '/browse?within=f%2Flead.jpg&group=day';

    const n = calls.length;
    cells[0].querySelector('.pick').click();
    check('the one a guess is drawn on can be agreed with',
          actBtn('top').hidden === false);
    actBtn('top').click();
    for (let i = 0; i < 8; i++) await settle();

    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('agreeing writes once', sent.length === 1, String(sent.length));
    if (sent.length === 1) {
      check('putting the others behind the one already showing',
            JSON.parse(sent[0].body).stacked_under === 'f/lead.jpg',
            sent[0].body);
    }
    // Out of the stack, and asked for afresh rather than gone back to: the
    // page behind is the one that has just stopped being true.
    check('and leaves the stack for the grid it was folded into',
          location.href.split('#')[0] === '/browse?group=day', location.href);
    check('without going back to a copy of it', wentBack === 0,
          String(wentBack));
    // Standing on the photograph it was made about. A fetched page starts at
    // the top, and the top is three thousand pixels above a review pass
    // somebody was part way through — which every other stack gesture keeps,
    // by never navigating at all.
    check('and says which photograph to land on',
          location.href.split('#')[1] === encodeURIComponent('f/lead.jpg'),
          location.href);
  }

  // --- the question is asked on the photographs -------------------------------
  // Every file in an opened guess is a candidate for the only thing the page
  // is for, so every one of them offers the answer where you are already
  // looking. It was select-then-press-the-bar: two gestures for a question
  // with a picture of its answer under the pointer, and the control to do it
  // in one already existed — summoned by a mode rather than standing.
  {
    const choose = c => c.children.find(k => k._classes.has('choose'));
    const asGuess = () => {
      cells.forEach(c => { const b = choose(c); if (b) b.remove(); });
      cells[0].dataset.behind = '0'; cells[0].dataset.proposed = '2';
      cells[1].dataset.under = ''; cells[1].dataset.proposedUnder = 'f/lead.jpg';
      cells[2].dataset.under = ''; cells[2].dataset.proposedUnder = 'f/lead.jpg';
      location.href = '/browse?within=f%2Flead.jpg&group=day';
    };
    asGuess();
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'history',
      'confirm', 'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS',
      'GROUPS', 'USUAL', 'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'NO_EVENT', 'STACK', 'BACK',
      'setTimeout', js,
    )(document, window, fetch, localStorage, location, history, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS, ONE_FIELD,
      GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, NO_EVENT, STACK, BACK, fn => fn());

    check('every photograph in an opened guess offers the answer',
          cells.every(c => !!choose(c)),
          cells.map(c => c.dataset.name + ':' + !!choose(c)).join(','));
    // *Show this one* under the photograph that is already the one shown
    // reads as a button that would do nothing — which is exactly the press
    // somebody needs to make, and exactly the one they will not.
    check('and the one already showing says what it would really do',
          choose(cells[0]).textContent === 'Confirm top',
          choose(cells[0]).textContent);
    check('while the others offer to take its place',
          choose(cells[1]).textContent === 'Show this one',
          choose(cells[1]).textContent);

    const n = calls.length;
    choose(cells[0]).click();
    for (let i = 0; i < 8; i++) await settle();
    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('one press agrees with it', sent.length === 1, String(sent.length));
    if (sent.length === 1) {
      check('putting the others behind the one already showing',
            JSON.parse(sent[0].body).stacked_under === 'f/lead.jpg',
            sent[0].body);
    }
    check('and leaves for the grid, standing on the photograph',
          location.href === '/browse?group=day#'
                            + encodeURIComponent('f/lead.jpg'),
          location.href);

    // A stack somebody already made asks the same question, and the takes
    // answer it the same way — this is the page for *which of these shows*,
    // and there is nothing else on the bar that says it. Only the one that
    // already shows stays quiet: it is the answer already given, and a button
    // whose whole reply is that it should not have been pressed is worse than
    // none.
    cells.forEach(c => { const b = choose(c); if (b) b.remove(); });
    cells[0].dataset.behind = '2'; cells[0].dataset.proposed = '0';
    cells[1].dataset.under = 'f/lead.jpg'; cells[1].dataset.proposedUnder = '';
    cells[2].dataset.under = 'f/lead.jpg'; cells[2].dataset.proposedUnder = '';
    location.href = '/browse?within=f%2Flead.jpg&group=day';
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'history',
      'confirm', 'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS',
      'GROUPS', 'USUAL', 'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'NO_EVENT', 'STACK', 'BACK',
      'setTimeout', js,
    )(document, window, fetch, localStorage, location, history, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS, ONE_FIELD,
      GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, NO_EVENT, STACK, BACK, fn => fn());

    check('the takes of a decided stack offer to take its place',
          !!choose(cells[1]) && !!choose(cells[2]),
          cells.map(c => c.dataset.name + ':' + !!choose(c)).join(','));
    check('and the one already showing says nothing, having nothing to say',
          !choose(cells[0]),
          cells.map(c => c.dataset.name + ':' + !!choose(c)).join(','));
  }

  // --- refusing a guess from the page it is on --------------------------------
  // Nothing is hidden here: the photographs a refusal releases are the ones
  // already standing on the page. Fetching them anyway put a second copy of
  // every one of them into the grid — refuse three and watch five arrive —
  // because that is what refusing does in the folded grid, where they really
  // are somewhere else.
  {
    const actBtn = name => actions.querySelectorAll('[data-act]')
                                  .find(b => b.dataset.act === name);
    if (document.byId.selcount.textContent !== '0 selected') tick.click();
    cells.forEach(c => { const b = c.children.find(k => k._classes.has('choose'));
                         if (b) b.remove(); });
    cells[0].dataset.behind = '0'; cells[0].dataset.proposed = '2';
    cells[1].dataset.under = ''; cells[1].dataset.proposedUnder = 'f/lead.jpg';
    cells[2].dataset.under = ''; cells[2].dataset.proposedUnder = 'f/lead.jpg';

    const before = grid.querySelectorAll('.cell').length;
    const n = calls.length;
    cells[0].querySelector('.pick').click();
    actBtn('nostack').click();
    for (let i = 0; i < 8; i++) await settle();

    check('nothing is fetched to put back what is already here',
          !calls.slice(n).some(c => c.url.startsWith('/api/behind')),
          calls.slice(n).map(c => c.url).join(','));
    check('so no photograph arrives twice',
          grid.querySelectorAll('.cell').length === before,
          String(grid.querySelectorAll('.cell').length));

    // Said of the photograph the group is drawn as, the answer is about the
    // group. The server does not follow the members of an opened stack — they
    // are in front of the curator — so the page names them, or the rest
    // re-form behind a new leader and are offered again tomorrow.
    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('refusing the one that speaks refuses the group', sent.length === 1,
          String(sent.length));
    if (sent.length === 1) {
      const body = JSON.parse(sent[0].body);
      check('naming every photograph in it', body.files.length === 3,
            sent[0].body);
      check('and saying so once', body.no_stack === true, sent[0].body);
    }
  }

  // --- and once there is nothing left to compare ------------------------------
  // A stack of one is not a stack, so the page has nothing left to ask about.
  {
    const actBtn = name => actions.querySelectorAll('[data-act]')
                                  .find(b => b.dataset.act === name);
    if (document.byId.selcount.textContent !== '0 selected') tick.click();
    dropping = cells.map(c => ({ folder: 'f', name: c.dataset.name }));
    location.href = '/browse?within=f%2Flead.jpg&group=day';
    cells[0].querySelector('.pick').click();
    actBtn('nostack').click();
    for (let i = 0; i < 8; i++) await settle();
    dropping = [];

    check('an emptied stack leaves for the grid it came from',
          location.href.split('#')[0] === '/browse?group=day', location.href);
  }

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok');
})().catch(e => { console.log('FAIL ' + (e && e.stack)); process.exit(1); });
