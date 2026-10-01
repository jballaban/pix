// The Display menu, driven: which facts a thumbnail shows, and in which lane.
//
// Its own stage because it is about where things *are* inside a cell, which no
// other stage looks at — they all press the circle and read the request.
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
    folder: 'f', name, kind: 'video', audience: 'bob', tags: 'beach',
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
const cells = [cell('a.mp4'), cell('b.mp4')];
section(null, cells, grid);
mk('actions');
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
const stored = { 'pix2.info': JSON.stringify(
  { tags: 'bot', people: 'off', subevent: 'sideways' }) };
const localStorage = {
  getItem: k => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
};
const window = {
  innerWidth: 1400, scrollY: 0, devicePixelRatio: 1,
  scrollBy: () => {}, scrollTo: () => {}, addEventListener: () => {},
};
const fetch = async () => ({ ok: true, json: async () => ({}) });
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
  new Function(
    'document', 'window', 'fetch', 'localStorage', 'location', 'history',
    'confirm', 'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS',
    'USUAL', 'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS',
    'UNREVIEWED', 'EVENT_SEP', 'NO_EVENT', 'STACK', 'BACK', 'setTimeout', js,
  )(document, window, fetch, localStorage, location, history, () => true,
    VIEW, [['event', 'Event']], {}, {}, true, ['family'], ['family'], 'family',
    [['day', 'By day']], { event: 'event', subevent: 'event' }, ['day'],
    '/browse', [['/thumb/', 400]], 'new', ' > ', '(none)', '', '', fn => fn());
} catch (e) {
  console.log('FAIL the script threw on load: ' + e.message);
  process.exit(1);
}

const c = cells[0];
check('a cell gets its two lanes', laneOf(c, 'top') && laneOf(c, 'bot'));
check('the circle holds the top-right end', holds(fixOf(c, 'top'), 'pick'));
check('with the clip mark beside it', holds(fixOf(c, 'top'), 'clip-mark'));
check('and the length holds the bottom-right', holds(fixOf(c, 'bot'), 'badge'));
check('tags go where they were last put', holds(laneOf(c, 'bot'), 'tags'));
check('and not in the lane they default to', !holds(laneOf(c, 'top'), 'tags'));
check('access stays in its default lane', holds(laneOf(c, 'bot'), 'who'));
check('nothing is left loose in the cell',
      !c.children.some(k => ['pick', 'tags', 'folk', 'who', 'badge',
                             'clip-mark'].some(n => k._classes.has(n))));

const root = document.documentElement;
check('what is off is said on <html>, for the stylesheet',
      root.getAttribute('data-info-people') === 'off',
      root.getAttribute('data-info-people'));
check('a stored value that is not a place is not believed',
      root.getAttribute('data-info-subevent') === 'bot',
      root.getAttribute('data-info-subevent'));

const opt = (key, at) => showOpts.find(b => b.dataset.info === key
                                         && b.dataset.at === at);
check('the menu shows what is chosen',
      opt('tags', 'bot').getAttribute('aria-pressed') === 'true'
      && opt('tags', 'top').getAttribute('aria-pressed') === 'false');

opt('tags', 'top').click();
check('choosing Top moves the tags up', holds(laneOf(c, 'top'), 'tags')
      && !holds(laneOf(c, 'bot'), 'tags'));
check('on every cell', holds(laneOf(cells[1], 'top'), 'tags'));
check('and is remembered', JSON.parse(stored['pix2.info']).tags === 'top',
      stored['pix2.info']);
check('the menu follows', opt('tags', 'top').getAttribute('aria-pressed') === 'true');

opt('access', 'top').click();
const top = laneOf(c, 'top').children;
check('two facts in one lane keep the menu\'s order — access before tags',
      top.findIndex(k => k._classes.has('who'))
      < top.findIndex(k => k._classes.has('tags')),
      top.map(k => k.className).join(' '));

opt('clip', 'off').click();
check('a fixed fact is turned off, not moved',
      root.getAttribute('data-info-clip') === 'off'
      && holds(fixOf(c, 'top'), 'clip-mark'));

if (failures.length) {
  failures.forEach(f => console.log('FAIL ' + f));
  process.exit(1);
}
console.log('ok');
