// The same script, on a grid that is folding the app's own guesses.
//
// A second stage rather than more of drive.js's, because this is a different
// library: one photograph standing for two the viewer cannot see, beside a
// stack somebody actually made and a photograph that is only itself. Telling
// those three apart is the whole of the feature, and drive.js's two cells
// cannot pose the question.
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
  if (+c.dataset.behind + +c.dataset.proposed > 0) {
    const badge = new El('a');
    badge.className = 'stack' + (+c.dataset.proposed ? ' guessed' : '');
    c.appendChild(badge);
  }
  return c;
}

const grid = mk('grid');
const heading = new El('h3');
heading.className = 'group';
for (const cls of ['grppick', 'addgrp']) {
  const b = new El('button');
  b.className = cls;
  heading.appendChild(b);
}
const crumb = new El('span');
crumb.className = 'crumb';
crumb.dataset.level = '0';
for (const cls of ['grpname', 'rmgrp']) {
  const b = new El('button');
  b.className = cls;
  crumb.appendChild(b);
}
heading.appendChild(crumb);
const count = new El('span');
count.className = 'dim';
count.textContent = '3';
heading.appendChild(count);
grid.appendChild(heading);

// A guess, a decision, and a photograph that is neither.
const cells = [cell('lead.jpg', { proposed: '2' }),
               cell('real.jpg', { behind: '1' }),
               cell('plain.jpg')];
cells.forEach(c => grid.appendChild(c));
const lead = cells[0];

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
const actBtn = name => actions.querySelectorAll('[data-act]')
                              .find(b => b.dataset.act === name);

const realQsa = document.querySelectorAll.bind(document);
document.querySelectorAll = sel => (sel === '.cell' ? cells
                                  : sel === '.group' ? [heading]
                                  : sel === '.stage' ? [stage] : realQsa(sel));
document.querySelector = sel => document.querySelectorAll(sel)[0] || null;

// What the two photographs behind the guess come back as.
const BEHIND =
  '<div class="cell" data-folder="f" data-name="one.jpg" data-kind="image"'
  + ' data-audience="" data-event="" data-tags="" data-date="2026-08-30"'
  + ' data-deleted="" data-under="" data-behind="0" data-proposed="0">'
  + '<button class="pick" aria-label="select"></button></div>'
  + '<div class="cell" data-folder="f" data-name="two.jpg" data-kind="image"'
  + ' data-audience="" data-event="" data-tags="" data-date="2026-08-30"'
  + ' data-deleted="" data-under="" data-behind="0" data-proposed="0">'
  + '<button class="pick" aria-label="select"></button></div>';

const calls = [];
const fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body });
  if (url.startsWith('/api/behind/')) {
    return { ok: true, json: async () => ({ cells: BEHIND }) };
  }
  if (url.startsWith('/api/file/')) {
    return { ok: true, json: async () => ({ name: 'lead.jpg', exif: {}, facts: [] }) };
  }
  return { ok: true,
           json: async () => ({ failed: [], dropped: [], total: 5,
                                purged: 0, binned: 0 }) };
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
const location = { href: '/browse', reload: () => {} };
const confirm = () => true;
const VIEW = { event: null, date: null, tag: null, audience: null, kind: null,
               band: null, deleted: null, stacks: null, within: null };
const GRID_GROUPS = [['day', 'By day'], ['stack', 'By stack'],
                     ['none', 'Ungrouped']];
const GROUPING = ['day'];
const CHIPS = [['event', 'Event'], ['stacks', 'Stacks']];
const FIXED = { stacks: [['only', 'Only stacks'],
                         ['guesses', 'Only suggested'],
                         ['firm', 'No suggestions']] };
const EXTRA = {};
const ADMIN = true;
const USERS = ['family'];
const GROUPS = ['family'];
const USUAL = 'family';
const PAGE = '/browse';
// What each derived tier is capped at, longest edge.
const TIERS = [['/thumb/', 400], ['/large/', 1000],
               ['/preview/', 1600]];

const settle = () => new Promise(r => setImmediate(r));
const keys = {};
function arrow(key) {
  (keys.keydown || []).forEach(fn => fn(
    { key, preventDefault() {}, target: { tagName: 'DIV' } }));
}
const writes = () => calls.filter(c => c.url.startsWith('/api/decide'));
const inGrid = n => grid.children.some(
  c => c._classes.has('cell') && c.dataset.name === n);
const at = n => grid.children.filter(c => c._classes.has('cell'))
                            .findIndex(c => c.dataset.name === n);
function deselect() {
  if (document.byId.selcount.textContent !== '0 selected') tick.click();
}

(async () => {
  const realAdd = document.addEventListener.bind(document);
  document.addEventListener = (t, fn) => { (keys[t] ||= []).push(fn); realAdd(t, fn); };
  try {
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'GROUPING', 'PAGE', 'TIERS', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS,
      GROUPING, PAGE, TIERS, fn => fn());
  } catch (e) {
    console.log('FAIL the script threw on load: ' + e.message);
    process.exit(1);
  }

  // --- what can be refused ----------------------------------------------------
  const no = actBtn('nostack');
  check('refusing is not offered with nothing selected', no.hidden === true);

  cells[2].querySelector('.pick').click();
  check('nor for a photograph the app said nothing about', no.hidden === true);

  deselect();
  cells[1].querySelector('.pick').click();
  // Un-deciding a decision is what Unstack is for, and offering both here
  // would make two buttons for the same gesture with different consequences.
  check('nor for a stack somebody made', no.hidden === true);
  check('but that one can be taken apart', actBtn('unstack').hidden === false);

  deselect();
  lead.querySelector('.pick').click();
  check('offered for a guess', no.hidden === false);
  // A guess is chosen between exactly like a decision: the badge means there
  // are more of these, and Stack is how you say which one to keep — on its
  // own, because the others are not on the page to be ticked.
  check('and a guess can be accepted as a stack',
        actBtn('stack').hidden === false);
  // Nothing to take apart yet. Offering it would be un-deciding something
  // nobody decided, beside a button that refuses the same guess properly.
  check('but not taken apart', actBtn('unstack').hidden === true);

  // --- refusing it ------------------------------------------------------------
  no.click();
  await settle(); await settle(); await settle();

  const asked = calls.findIndex(c => c.url.startsWith('/api/behind/'));
  const wrote = calls.findIndex(c => c.url.startsWith('/api/decide'));
  check('what it was hiding is asked for', asked >= 0);
  // Afterwards they are nothing's members, and the page would have no way
  // left to find out what it had been holding back.
  check('and asked for before the refusal is written',
        asked >= 0 && wrote >= 0 && asked < wrote, `${asked} then ${wrote}`);
  check('the refusal is written once', writes().length === 1,
        String(writes().length));
  const body = JSON.parse((writes()[0] || {}).body || '{}');
  check('it says this is not a stack', body.no_stack === true,
        writes()[0] && writes()[0].body);
  check('and names the photograph that spoke for it',
        (body.files || []).length === 1 && body.files[0].name === 'lead.jpg',
        writes()[0] && writes()[0].body);

  // The library is not smaller than it was — those photographs were always
  // there, and a grid that kept them off screen until the next reload would
  // be quietly holding part of it back.
  check('what it hid is on the page', inGrid('one.jpg') && inGrid('two.jpg'));
  check('beside the one that was speaking for them',
        at('one.jpg') === at('lead.jpg') + 1
        && at('two.jpg') === at('lead.jpg') + 2,
        `${at('lead.jpg')} ${at('one.jpg')} ${at('two.jpg')}`);
  check('and it no longer claims to be a stack',
        !lead.children.find(k => k._classes.has('stack'))
        && lead.dataset.proposed === '0');
  check('so it cannot be refused twice', no.hidden === true);

  // Put where they belong rather than at the end: the order the grid reads in
  // is the order the viewer pages through, and a photograph on screen here and
  // last in that order opens the wrong picture.
  deselect();
  lead.click();
  await settle();
  arrow('ArrowRight');
  await settle();
  check('paging on goes to the one beside it',
        document.byId.vmeta.textContent.startsWith('one.jpg'),
        document.byId.vmeta.textContent);

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok');
})().catch(e => { console.log('FAIL ' + (e && e.stack)); process.exit(1); });
