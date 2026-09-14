// The same script again, on the landing page.
//
// That page has no selection, no viewer and no actions — it is folders, not
// photographs — so the script finds most of what it wires missing. Whether
// that is *a page without photographs* or *a page whose every handler died on
// load* is not visible in either the markup or the script, and the difference
// is the whole page: one throw takes the chips and the grouping menu with it.
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

const grid = mk('grid', 'grid folders');
// The heading is the grouping control, and on this page that is all it is:
// there is nothing here to select, so it carries no select-all.
const heading = new El('h3');
heading.className = 'group';
const crumb = new El('span');
crumb.className = 'crumb';
crumb.dataset.level = '0';
const grpname = new El('button');
grpname.className = 'grpname';
crumb.appendChild(grpname);
const rm = new El('button');
rm.className = 'rmgrp';
crumb.appendChild(rm);
heading.appendChild(crumb);
const addBtn = new El('button');
addBtn.className = 'addgrp';
heading.appendChild(addBtn);
document.appendChild(heading);

// Folders, not cells: the script's grid machinery must find nothing to do.
for (const name of ['2025', '2026']) {
  const t = new El('a');
  t.className = 'tile';
  t.attrs.href = '/browse?date=' + name;
  grid.appendChild(t);
}

// **Every element the real page has, and no others.** This stage used to be a
// hand-picked list, which is how it came to hold a `#menu` the landing page
// did not render: the script threw reaching for it on load, every handler on
// the page died with it, and the harness went on passing because its own
// stage had one. The ids now come from the served HTML.
const ids = process.argv[3]
  ? JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))
  : ['grid', 'menu', 'chips', 'note', 'sizepick'];
for (const id of ids) if (!document.byId[id]) mk(id);
// No size control here: it is a thumbnail size, and these folders are text.

const calls = [];
const fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body });
  return { ok: true, json: async () => [{ value: 'Sicily', n: 12, scope: 'all' }] };
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
let went = null;
const location = { get href() { return '/'; },
                   set href(v) { went = v; },
                   reload: () => {} };
const confirm = () => true;
const VIEW = { event: null, date: '2026', tag: null, audience: null,
               kind: null, band: null, camera: null, deleted: null,
               stacks: null, within: null };
const GRID_GROUPS = [['day', 'By day'], ['year', 'By year'],
                     ['event', 'By event'], ['none', 'Ungrouped']];
const GROUPING = ['year'];
const CHIPS = [['event', 'Event'], ['date', 'Date'], ['camera', 'Camera']];
const FIXED = {};
const EXTRA = {};
const ADMIN = true;
const USERS = ['family'];
const GROUPS = ['family'];
const USUAL = 'family';
const PAGE = '/';

const settle = () => new Promise(r => setImmediate(r));
// The key handler is bound to the document, so it runs on this page too —
// with no viewer on it to ask about, and no cells to page through.
const keys = {};
const realAdd = document.addEventListener.bind(document);
document.addEventListener = (t, fn) => { (keys[t] ||= []).push(fn); realAdd(t, fn); };
const press = key => (keys.keydown || []).forEach(fn => fn(
  { key, preventDefault() {}, target: { tagName: 'DIV' } }));

(async () => {
  try {
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'GROUPING', 'PAGE', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS,
      GROUPING, PAGE, fn => fn());
  } catch (e) {
    console.log('FAIL the script threw on load: ' + e.message);
    process.exit(1);
  }

  // --- the filters ------------------------------------------------------------
  const chips = document.byId.chips;
  const menu = document.byId.menu;
  const labels = el => el.querySelectorAll('.opt')
    .map(o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
  const on = () => chips.children.filter(c => !c._classes.has('addchip'));
  const plus = () => chips.children.find(c => c._classes.has('addchip'));

  // Only the filters that are doing something. The others are a list of
  // questions the app can ask, which is not a thing to read past on the way
  // to the one or two that are the address of what you are looking at.
  check('only the filters in use are on the bar', on().length === 1,
        chips.children.map(c => c.innerHTML).join('|'));
  check('and it says what it is set to', on()[0].innerHTML.includes('2026'),
        on()[0].innerHTML);
  check('the rest are behind a +', !!plus());

  plus().click();
  await settle();
  check('which offers the ones not in use',
        labels(menu).join(',') === 'Event,Camera', labels(menu).join(','));
  check('and not the one already on the bar',
        !labels(menu).includes('Date'), labels(menu).join(','));

  // Two steps: which question, then what to answer. The value list is the one
  // the chip itself opens, rather than a second version of it here.
  menu.querySelectorAll('.opt')[0].click();
  await settle(); await settle();
  check('picking one opens its values', menu.hidden === false);
  const asked = calls.filter(c => c.url.startsWith('/api/suggest'));
  check('which the server is asked for', asked.length === 1,
        String(asked.length));
  // They have to be the values for *this* view, or the folder you are standing
  // in has nothing to do with the list you are handed.
  check('for this view', asked.length > 0 && asked[0].url.includes('date=2026'),
        asked[0] && asked[0].url);
  const opts = menu.querySelectorAll('.opt');
  check('the values come back', opts.length > 0, String(opts.length));
  opts[0].click();
  check('picking one goes to a view that carries both',
        !!went && went.includes('event=Sicily') && went.includes('date=2026'),
        String(went));
  // On *this* page. Narrowing the library from here and being handed the file
  // grid instead is indistinguishable from the control doing nothing — except
  // that the summary you were reading is gone.
  check('and stays on this page', !!went && went.startsWith('/?'),
        String(went));

  // --- the grouping -----------------------------------------------------------
  went = null;
  grpname.click();
  await settle();
  check('the grouping menu opens', menu.hidden === false);
  const levels = menu.querySelectorAll('.opt');
  check('it offers the ways to cut the library', levels.length > 0,
        String(levels.length));
  levels[0].click();
  check('and choosing one regroups this page',
        !!went && went.includes('group=') && went.startsWith('/?'),
        String(went));

  // Sub-grouping: a level *inside* the one already there, which is how you go
  // from a shelf of years to the events within them without losing the years.
  went = null;
  addBtn.click();
  await settle();
  const inner = menu.querySelectorAll('.opt');
  const byEvent = inner.find(o => o.innerHTML.includes('By event'));
  check('a level can be added inside the one already there', !!byEvent,
        inner.map(o => o.innerHTML).join('|'));
  if (byEvent) byEvent.click();
  check('and it nests rather than replacing',
        !!went && went.includes('group=year%2Cevent') && went.startsWith('/?'),
        String(went));

  went = null;
  rm.click();
  check('the cross drops a level',
        !!went && went.includes('group=none') && went.startsWith('/?'),
        String(went));

  // --- the keyboard -----------------------------------------------------------
  // Escape is the way out of a menu here as everywhere. It reached for the
  // viewer first, and there is none on this page — so every press threw, and
  // an exception in a document handler is invisible until you open the
  // console.
  plus().click();
  await settle();
  check('a menu is open to escape from', menu.hidden === false);
  press('Escape');
  check('escape closes it', menu.hidden === true);
  // And with nothing open: still no viewer to ask about, still no cells to
  // page through.
  press('Escape');
  press('ArrowRight');
  press('ArrowLeft');
  press('i');

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok');
})().catch(e => { console.log('FAIL ' + (e && e.stack)); process.exit(1); });
