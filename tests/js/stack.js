// The same script, standing inside an opened stack.
//
// A third stage rather than more of review.js's, because this one is about a
// grid that is *somewhere* — `VIEW.within` set — and about the two ways out of
// it. Both of the things checked here were, until now, one thing: the only way
// to leave a stack was the browser's own back button, and pressing it put a
// photograph on screen that nobody had asked to see.
const fs = require('fs');
const { El, document } = require('./dom.js');

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
const badge = new El('a');
badge.className = 'stack';
badge.attrs.href = '/browse?within=f%2Flead.jpg';
cells[0].appendChild(badge);
cells.forEach(c => grid.appendChild(c));

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
mk('sizepick');
for (const id of ['menu', 'chips', 'selcount', 'count', 'note', 'viewer',
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
const fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body });
  return { ok: true,
           json: async () => ({ name: 'lead.jpg', exif: {}, facts: [],
                                failed: [], dropped: [], total: 3 }) };
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
const GROUPING = ['day'];
const CHIPS = [['event', 'Event'], ['stacks', 'Stacks']];
const FIXED = { stacks: [['only', 'Only stacks']] };
const EXTRA = {};
const ADMIN = true;
const USERS = ['family'];
const GROUPS = ['family'];
const USUAL = 'family';
const PAGE = '/browse';
const TIERS = [['/thumb/', 400], ['/large/', 1000],
               ['/preview/', 1600]];

const settle = () => new Promise(r => setImmediate(r));
const chips = document.byId.chips;
const marker = () => chips.children.find(c => c._classes.has('from-op'));
const viewerOpen = () => document.byId.viewer._classes.has('on');

(async () => {
  try {
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'history',
      'confirm', 'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS',
      'GROUPS', 'USUAL', 'GRID_GROUPS', 'GROUPING', 'PAGE', 'TIERS',
      'setTimeout', js,
    )(document, window, fetch, localStorage, location, history, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS,
      GROUPING, PAGE, TIERS, fn => fn());
  } catch (e) {
    console.log('FAIL the script threw on load: ' + e.message);
    process.exit(1);
  }

  // --- where you are ----------------------------------------------------------
  // A filter you cannot see is a library that looks smaller than it is, and an
  // opened stack is the narrowest filter there is.
  const m = marker();
  check('an opened stack says so on the bar', m !== undefined);
  check('and says what it is', m && m.textContent === 'in a stack',
        m && m.textContent);
  check('and how much is in it',
        m && m.children.some(k => k._classes.has('val')
                                  && k.textContent === '3'));

  const out = m && m.children.find(k => k._classes.has('x'));
  check('with a way out', out !== undefined);
  // The stack is dropped and everything else about the view is kept: leaving
  // one is not the same gesture as clearing the filters you arrived with.
  check('that drops the stack and keeps the rest',
        out && out.href === '/browse?group=day', out && out.href);

  // --- the way out ------------------------------------------------------------
  document.referrer = 'https://pix.ballaban.ca/browse?group=day';
  out.click();
  check('leaving goes back the way you came in', wentBack === 1);
  check('rather than navigating afresh',
        location.href === '/browse?within=f%2Flead.jpg&group=day',
        location.href);

  // Arrived by a bookmark, a shared link, or from the operation log: there is
  // nothing behind this page worth going back to, so nothing is intercepted
  // and the browser simply follows the link. That it stays a real link is the
  // point — it is also what makes *open in a new tab* work on it.
  document.referrer = '';
  out.click();
  check('and is left to the browser when you arrived some other way',
        wentBack === 1, String(wentBack));
  check('which has somewhere to go', out.href === '/browse?group=day',
        out.href);

  // --- the badge is a place to go, not a photograph ---------------------------
  location.href = '/browse?within=f%2Flead.jpg&group=day';
  badge.click();
  await settle();
  // The badge used to do both: the viewer opened over the grid and then the
  // page left for the stack, so the browser's back button restored a page with
  // a photograph on it.
  check('the stack badge does not also open the viewer', !viewerOpen());

  cells[1].click();
  await settle();
  check('but a plain click on a cell still does', viewerOpen());

  // What a restore from the back/forward cache brings back is the page exactly
  // as it left, open viewer included.
  (listeners.pageshow || []).forEach(fn => fn({ persisted: true }));
  check('and a page restored by going back has it closed', !viewerOpen());

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok');
})().catch(e => { console.log('FAIL ' + (e && e.stack)); process.exit(1); });
