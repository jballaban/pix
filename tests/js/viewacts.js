// The viewer's own actions, driven: whatever is selected in the grid behind
// it, an action pressed in the viewer is about the photograph on show.
const fs = require('fs');
const { El, document, sizeset, section, runPage } = require('./dom.js');

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
function kid(parent, tag, cls) {
  const e = new El(tag);
  e.className = cls;
  parent.appendChild(e);
  return e;
}

// A cell the way markup from before the lanes drew one: every mark a direct
// child. The script has to put them where they belong, not assume the server
// already did.
function cell(name) {
  const c = new El('div');
  c.className = 'cell';
  Object.assign(c.dataset, {
    folder: 'f', name, kind: 'image', audience: 'bob', tags: 'beach',
    people: 'Mom', date: '2026-08-30', under: '', behind: '0', proposed: '0',
  });
  kid(c, 'button', 'pick');
  kid(kid(c, 'span', 'tags'), 'i', '');
  kid(kid(c, 'span', 'folk'), 'i', '');
  kid(kid(c, 'span', 'who'), 'i', '');
  kid(c, 'span', 'badge');
  kid(c, 'span', 'clip-mark');
  return c;
}

const grid = mk('grid');
const cells = [cell('a.jpg'), cell('b.jpg')];
section(null, cells, grid);
const actions = mk('actions');
const barGrp = kid(actions, 'span', 'grp');
barGrp.dataset.side = 'live';
const barDelete = kid(barGrp, 'button', '');
barDelete.dataset.act = 'delete';
// The viewer's row, as `_viewacts` renders it.
const viewActs = mk('viewacts');
const liveGrp = kid(viewActs, 'span', 'vgrp');
liveGrp.dataset.side = 'live';
const viewDelete = kid(liveGrp, 'button', 'danger');
viewDelete.dataset.vact = 'delete';
const goneGrp = kid(viewActs, 'span', 'vgrp');
goneGrp.dataset.side = 'gone';
goneGrp.hidden = true;
const viewRestore = kid(goneGrp, 'button', '');
viewRestore.dataset.vact = 'restore';
sizeset();

// The Display rows, as the server renders them, in a menu of their own.
const menu = mk('dispmenu');
const showOpts = [];
for (const [key, ats] of [['people', ['off', 'top', 'bot']],
                          ['access', ['off', 'top', 'bot']],
                          ['tags', ['off', 'top', 'bot']],
                          ['subevent', ['off', 'top', 'bot']],
                          ['clip', ['off', 'on']]]) {
  for (const at of ats) {
    const b = kid(menu, 'button', 'showopt');
    b.dataset.info = key;
    b.dataset.at = at;
    showOpts.push(b);
  }
}
for (const id of ['menu', 'selcount', 'count', 'note', 'viewer', 'vimg',
                  'vvid', 'vmeta', 'rail', 'railtoggle', 'viewclose',
                  'working', 'workwhat', 'workbar', 'worktally', 'workstop',
                  'bincount', 'chips']) mk(id);
const stage = new El('div');
stage.className = 'stage';
document.byId.viewer.appendChild(stage);
Object.assign(document.byId.vvid, {
  pause() {}, load() {}, play: () => Promise.resolve(),
});
document.documentElement = new El('html');

const realQsa = document.querySelectorAll.bind(document);
document.querySelectorAll = sel => (sel === '.cell' ? cells
                                  : sel === '.stage' ? [stage] : realQsa(sel));
document.querySelector = sel => document.querySelectorAll(sel)[0] || null;

// What was chosen last time: tags at the bottom, people off — and a value
// that is not a place, which must not be believed.
const stored = {};
const localStorage = {
  getItem: k => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
};
const window = {
  innerWidth: 1400, scrollY: 0, devicePixelRatio: 1,
  scrollBy: () => {}, scrollTo: () => {}, addEventListener: () => {},
};
const sent = [];
const fetch = async (url, opts) => {
  if (opts && opts.body) sent.push(url + ' ' + opts.body);
  return { ok: true, json: async () => ({ failed: [], dropped: [], total: 2,
                                          exif: {}, facts: [] }) };
};
const location = { href: '/browse', reload: () => {} };
const history = { back: () => {} };
const VIEW = { event: null, date: null, tag: null, audience: null, kind: null,
               band: null, deleted: null, stacks: null, within: null };
globalThis.ARCHIVED = 'archived';
globalThis.ARCHIVED_LABEL = 'Hidden';

const laneOf = (c, where) => {
  const ov = c.children.find(k => k._classes.has('ov') && k._classes.has(where));
  return ov ? ov.children.find(k => k._classes.has('lane')) : null;
};
const fixOf = (c, where) => {
  const ov = c.children.find(k => k._classes.has('ov') && k._classes.has(where));
  return ov ? ov.children.find(k => k._classes.has('fix')) : null;
};
const holds = (box, cls) => !!box && box.children.some(k => k._classes.has(cls));

try {
  runPage(js, ['document', 'window', 'fetch', 'localStorage', 'location', 'history', 'confirm', 'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL', 'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'NO_EVENT', 'STACK', 'BACK', 'setTimeout'],
      [document, window, fetch, localStorage, location, history, () => true, VIEW, [['event', 'Event']], {}, {}, true, ['family'], ['family'], 'family', [['day', 'By day']], { event: 'event', subevent: 'event' }, ['day'], '/browse', [['/thumb/', 400]], 'new', ' > ', '(none)', '', '', fn => fn()]);
} catch (e) {
  console.log('FAIL the script threw on load: ' + e.message);
  process.exit(1);
}

const settle = () => new Promise(r => setImmediate(r));
(async () => {
  // Something selected in the grid: the first photograph.
  cells[0].querySelector('.pick').click();
  // And the second one opened in the viewer.
  cells[1].click();
  await settle();
  check('the viewer is open', document.byId.viewer._classes.has('on'));

  viewDelete.click();
  for (let i = 0; i < 6; i++) await settle();
  const said = sent.join(' | ');
  check('deleting from the viewer deletes the photograph on show',
        said.includes('b.jpg'), said);
  check('and not what happens to be selected behind it',
        !said.includes('a.jpg'), said);

  // Closed again, the bar acts on the selection as it always did.
  sent.length = 0;
  document.byId.viewclose.click();
  barDelete.click();
  for (let i = 0; i < 6; i++) await settle();
  const after = sent.join(' | ');
  check('the bar still means the selection', after.includes('a.jpg')
        && !after.includes('b.jpg'), after);

  // A binned photograph is offered restore, not delete.
  cells[1].dataset.deleted = '1';
  cells[1].click();
  await settle();
  check('a binned file offers restore', goneGrp.hidden === false
        && liveGrp.hidden === true);

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok');
})();
