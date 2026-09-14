// The same script, on the review page (spec/nas-app.md §15).
//
// A second stage rather than more of drive.js's, because the review page is a
// different page: no sections, no grouping, every photograph in an open
// proposal with a control on it. Driving it from the ordinary grid's stage
// would mean asserting on a page the server never sends.
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

function cell(name) {
  const c = new El('div');
  c.className = 'cell';
  Object.assign(c.dataset, {
    folder: 'f', name, kind: 'image', audience: '', tags: '',
    date: '2026-08-30',
  });
  const pick = new El('button');
  pick.className = 'pick';
  c.appendChild(pick);
  return c;
}

const grid = mk('grid');
// Two proposals, because they are answered one at a time and the one still to
// answer has to survive the one just answered.
const cells = [];
function proposal(names) {
  const h = new El('h3');
  h.className = 'group proposal';
  h.dataset.files = names.map(n => 'f/' + n).join(',');
  const when = new El('span');
  when.className = 'grpname dim';
  when.textContent = '2026 08 30 10:00';
  h.appendChild(when);
  const no = new El('button');
  no.className = 'notastack';
  h.appendChild(no);
  grid.appendChild(h);
  for (const n of names) { const c = cell(n); cells.push(c); grid.appendChild(c); }
  return h;
}
const first = proposal(['a.jpg', 'b.jpg', 'c.jpg']);
const second = proposal(['d.jpg', 'e.jpg']);

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
                  'delete']);
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
           json: async () => ({ failed: [], dropped: [], total: 5,
                                purged: 0, binned: 0 }) };
};
const stored = {};
const localStorage = {
  getItem: k => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
};
const window = {
  innerWidth: 1400, scrollY: 0,
  scrollBy: () => {}, scrollTo: () => {},
  addEventListener: () => {},
};
const location = { href: '/browse?suggest=1', reload: () => {} };
const confirm = () => true;
const VIEW = { event: null, year: null, tag: null, audience: null, kind: null,
               band: null };
const GRID_GROUPS = [['day', 'By day'], ['none', 'Ungrouped']];
const GROUPING = [];
const CHIPS = [['event', 'Event'], ['audience', 'Access']];
const FIXED = {};
const EXTRA = {};
const ADMIN = true;
const USERS = ['family'];
const GROUPS = ['family'];
const USUAL = 'family';

const settle = () => new Promise(r => setImmediate(r));
const on = c => c.children.find(k => k._classes.has('choose')) || null;
const writes = () => calls.filter(c => c.url.startsWith('/api/decide'));
const inGrid = n => grid.children.some(
  c => c._classes.has('cell') && c.dataset.name === n);
const counted = () => document.byId.selcount.textContent;

(async () => {
  try {
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'GROUPING', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS,
      GROUPING, fn => fn());
  } catch (e) {
    console.log('FAIL the script threw on load: ' + e.message);
    process.exit(1);
  }

  // Nothing arrives selected and nothing is asked of the selection: the work
  // is answering one proposal at a time, and a page that arrived with three
  // photographs ticked would apply the next thing pressed to all of them.
  check('nothing arrives selected', counted() === '0 selected', counted());
  // Every photograph offers to be the one kept — the question is which of
  // these to keep, so each of them has to be answerable.
  check('each photograph offers to be kept', cells.every(on),
        String(cells.filter(on).length));

  // --- keeping one ------------------------------------------------------------
  on(cells[1]).click();
  await settle(); await settle();
  check('keeping one writes once', writes().length === 1,
        String(writes().length));
  const body = JSON.parse(writes()[0].body || '{}');
  check('the others are stacked under the one kept',
        body.stacked_under === 'f/b.jpg', writes()[0].body);
  check('and the one kept is not stacked under itself',
        (body.files || []).map(f => f.name).sort().join(',') === 'a.jpg,c.jpg',
        writes()[0].body);

  // Answered, so it stops being a question: the others fold away, the heading
  // goes, and what is left is the stack that was just made.
  check('the rest leave the page', !inGrid('a.jpg') && !inGrid('c.jpg'));
  check('the one kept stays', inGrid('b.jpg'));
  check('it is shown as a stack',
        !!cells[1].children.find(k => k._classes.has('stack')));
  check('the proposal is answered and gone',
        !grid.children.includes(first));
  check('no control is left asking on it',
        !inGrid('b.jpg') || !on(cells[1]));
  check('and nothing was left selected', counted() === '0 selected', counted());
  // The page is a list of questions. Answering one must not answer, disturb
  // or unwire the next — you go down the page.
  check('the next proposal is untouched', grid.children.includes(second)
        && inGrid('d.jpg') && inGrid('e.jpg'));
  check('and its date is still a date, not a count',
        second.querySelector('.dim').textContent === '2026 08 30 10:00',
        second.querySelector('.dim').textContent);

  // --- saying it is not a stack ----------------------------------------------
  const was = writes().length;
  second.querySelector('.notastack').click();
  await settle(); await settle();
  const said = writes().slice(was);
  check('declining writes once', said.length === 1, String(said.length));
  const refused = JSON.parse((said[0] || {}).body || '{}');
  // Remembered, so the same refusal is not offered again tomorrow.
  check('it is remembered against every file in the proposal',
        refused.no_stack === true
        && (refused.files || []).map(f => f.name).sort().join(',')
           === 'd.jpg,e.jpg', said[0] && said[0].body);
  check('the photographs leave the page',
        !inGrid('d.jpg') && !inGrid('e.jpg'));
  check('and the question with them', !grid.children.includes(second));
  check('nothing is selected after declining either',
        counted() === '0 selected', counted());

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok');
})().catch(e => { console.log('FAIL ' + (e && e.stack)); process.exit(1); });
