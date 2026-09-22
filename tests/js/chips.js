// The same script, read at the filter bar.
//
// Two things that are only true one click at a time, and so are invisible in
// the markup: a date closes *up a level* rather than all the way out, and no
// menu offers a value that means "everything" — the cross already says that,
// and a second way to say it is one more row to read past in the list of the
// ones that narrow.
//
// Run as: node chips.js <browse.js> <date> <expected date after closing>,
// because the ladder is a step at a time and each step is its own page.
const fs = require('fs');
const { El, document } = require('./dom.js');

const js = fs.readFileSync(process.argv[2], 'utf8');
const startDate = process.argv[3];
const afterClose = process.argv[4] === 'gone' ? null : process.argv[4];
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

const grid = mk('grid');
const actions = mk('actions');
const tick = new El('button');
tick.id = 'selall';
tick.className = 'tick';
document.byId.selall = tick;
actions.appendChild(tick);
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
document.querySelectorAll = sel => (sel === '.cell' ? []
                                  : sel === '.stage' ? [stage] : realQsa(sel));
document.querySelector = sel => document.querySelectorAll(sel)[0] || null;

const asked = [];
const fetch = async (url) => {
  asked.push(url);
  return { ok: true, json: async () => [] };
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
const location = { href: '/browse?start', reload: () => {} };
const history = { back: () => {} };
const confirm = () => true;
const VIEW = { event: null, tag: null, date: startDate, audience: null,
               kind: 'image', band: null, source: null, camera: null,
               deleted: null, stacks: null, within: null };
const GRID_GROUPS = [['day', 'By day'], ['none', 'Ungrouped']];
const ONE_FIELD = { event: 'event', subevent: 'event' };
const GROUPING = ['day'];
const CHIPS = [['date', 'Date'], ['kind', 'Type']];
const FIXED = { kind: [['image', 'Photos'], ['video', 'Video'],
                       ['other', 'Other']] };
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

const settle = () => new Promise(r => setImmediate(r));
const chips = document.byId.chips;
const chipFor = label => chips.children.find(
  c => c._classes.has('chip') && String(c.innerHTML).startsWith(label));
const crossIn = chip => chip.children.find(k => k._classes.has('x'));
// The option rows the menu is showing, as the text each one carries.
const optionLabels = () => document.byId.menu.querySelectorAll('.opt')
  .map(o => String(o.innerHTML).replace(/<[^>]*>/g, '').trim());

(async () => {
  try {
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'history',
      'confirm', 'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS',
      'GROUPS', 'USUAL', 'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'NO_EVENT',
      'setTimeout', js,
    )(document, window, fetch, localStorage, location, history, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS, ONE_FIELD,
      GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, NO_EVENT, fn => fn());
  } catch (e) {
    console.log('FAIL the script threw on load: ' + e.message);
    process.exit(1);
  }

  // --- a date is a hierarchy, and closing it goes up one -----------------------
  const date = chipFor('Date');
  check('the date is on the bar', date !== undefined);
  const cross = date && crossIn(date);
  check('and carries a cross', cross !== undefined);
  // It says which, because the cross on every other chip means *gone* and on
  // this one usually does not.
  check('that says where it goes',
        cross && String(date.innerHTML).includes(
          afterClose ? 'title="Up to ' + afterClose + '"' : 'title="Clear"'),
        date && date.innerHTML);

  cross.click();
  // `kind` rides along untouched: closing one filter says nothing about the
  // others, and a cross that cleared the bar would be a different library.
  const want = afterClose
    ? '/browse?date=' + afterClose + '&kind=image&group=day'
    : '/browse?kind=image&group=day';
  check('and lands a level wider', location.href === want, location.href);

  // --- everything else still closes outright ---------------------------------
  location.href = '/browse?start';
  crossIn(chipFor('Type')).click();
  check('a filter that is not a hierarchy still clears',
        location.href === '/browse?date=' + startDate + '&group=day',
        location.href);

  // --- no menu offers "everything" -------------------------------------------
  chipFor('Type').click();
  await settle();
  const labels = optionLabels();
  check('the menu offers the values that narrow',
        labels.includes('Photos') && labels.includes('Video'), labels.join('|'));
  check('and nothing that means everything',
        !labels.some(l => /^(any|all|everything)$/i.test(l)), labels.join('|'));

  // --- an event is a hierarchy too, and one of its rungs has a name ---------
  // *Sicily* is the trip. *Sicily, no sub-event* is the part of it nobody has
  // divided up yet — a different set of files, and the one the sub-event
  // grouping's folder for the event itself opens on. The bar has to be able
  // to tell them apart or it would be disagreeing with the folder that set
  // it, so the chip says which it is and the cross climbs to the other.
  location.href = '/browse?start';
  new Function(
    'document', 'window', 'fetch', 'localStorage', 'location', 'history',
    'confirm', 'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS',
    'GROUPS', 'USUAL', 'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE',
    'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'NO_EVENT', 'setTimeout', js,
  )(document, window, fetch, localStorage, location, history, confirm,
    { ...VIEW, date: null, kind: null,
      event: 'Sicily' + EVENT_SEP + NO_EVENT },
    [['event', 'Event']], FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
    GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP,
    NO_EVENT, fn => fn());

  const ev = chipFor('Event');
  check('the event is on the bar', ev !== undefined,
        chips.children.map(c => String(c.innerHTML)).join('|'));
  check('and says it is the event without its parts',
        String(ev.innerHTML).includes('Sicily (no sub-event)'),
        String(ev.innerHTML));
  check('rather than the sentinel it is spelled with',
        !String(ev.innerHTML).includes(NO_EVENT), String(ev.innerHTML));
  check('with a cross that widens to the whole event',
        String(ev.innerHTML).includes('title="Up to Sicily"'),
        String(ev.innerHTML));

  crossIn(ev).click();
  check('and pressing it lands on the whole event',
        location.href === '/browse?event=Sicily&group=day', location.href);

  // The whole event is the top of the ladder: there is nothing above a trip
  // but the library, which is what clearing means.
  location.href = '/browse?start';
  new Function(
    'document', 'window', 'fetch', 'localStorage', 'location', 'history',
    'confirm', 'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS',
    'GROUPS', 'USUAL', 'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE',
    'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'NO_EVENT', 'setTimeout', js,
  )(document, window, fetch, localStorage, location, history, confirm,
    { ...VIEW, date: null, kind: null,
      event: 'Sicily' + EVENT_SEP + 'Taormina' },
    [['event', 'Event']], FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
    GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP,
    NO_EVENT, fn => fn());

  const part = chipFor('Event');
  check('a part of an event is shown whole',
        String(part.innerHTML).includes('Sicily &gt; Taormina'),
        String(part.innerHTML));
  crossIn(part).click();
  check('and widens to the event it is part of',
        location.href === '/browse?event=Sicily&group=day', location.href);

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok');
})().catch(e => { console.log('FAIL ' + (e && e.stack)); process.exit(1); });
