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
// Each carries the circle the server draws on it, because a folder can be
// selected and a decision about it is a decision about the files it holds.
const tiles = [];
for (const name of ['2025', '2026']) {
  const t = new El('a');
  t.className = 'tile';
  t.attrs.href = '/browse?date=' + name;
  const pick = new El('button');
  pick.className = 'pick';
  t.appendChild(pick);
  const spread = new El('span');
  spread.className = 'spread audience';
  t.appendChild(spread);
  grid.appendChild(t);
  tiles.push(t);
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

// The folder bar, as the server draws it: a tick, a count, and the four
// controls whose question a *set* can answer. The ids above come from the
// served page, but an element stubbed by id alone has none of its contents.
{
  const bar = document.byId.actions;
  const tick = new El('button');
  tick.id = 'selall';
  tick.className = 'tick';
  document.byId.selall = tick;
  bar.appendChild(tick);
  const count = new El('span');
  count.id = 'selcount';
  count.className = 'count';
  document.byId.selcount = count;
  bar.appendChild(count);
  const g = new El('span');
  g.className = 'grp';
  g.dataset.side = 'live';
  g.hidden = true;
  for (const name of ['event', 'tags', 'access', 'download']) {
    const b = new El('button');
    b.dataset.act = name;
    g.appendChild(b);
  }
  bar.appendChild(g);
}
// No size control here: it is a thumbnail size, and these folders are text.

const calls = [];
// What each folder turns out to hold, by the query that opens it.
const inFolder = {
  '2025': [{ folder: 'f', name: 'a.jpg', tags: '', audience: '', event: '' }],
  '2026': [{ folder: 'f', name: 'b.jpg', tags: '', audience: 'kid', event: '' },
           { folder: 'f', name: 'c.jpg', tags: '', audience: '', event: '' }],
};
const fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body });
  if (url.startsWith('/api/files')) {
    const year = (url.match(/date=(\d+)/) || [])[1];
    return { ok: true, json: async () => inFolder[year] || [] };
  }
  if (url.startsWith('/api/decide')) {
    return { ok: true, json: async () => ({ failed: [], dropped: [], total: 3,
                                            binned: 0 }) };
  }
  return { ok: true, json: async () => [{ value: 'Sicily', n: 12, scope: 'all' }] };
};
const stored = {};
const localStorage = {
  getItem: k => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
};
const window = {
  innerWidth: 1400, scrollY: 0, devicePixelRatio: 1,
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
// Each filter's drawing, as the server hands it over. Stand-ins rather than
// the real paths: what this drives is that a chip is drawn and not spelled
// out, which is true of any `<svg>`.
const MARKS = { event: '<svg id="m-event"></svg>', date: '<svg id="m-date"></svg>',
                camera: '<svg id="m-camera"></svg>' };
const FIXED = {};
const EXTRA = {};
const ADMIN = true;
const USERS = ['family'];
const GROUPS = ['family'];
const USUAL = 'family';
// The audience value meaning *nobody yet*, as the server sends it.
const UNREVIEWED = 'new';
const PAGE = '/';
// What each derived tier is capped at, longest edge.
const TIERS = [['/thumb/', 400], ['/large/', 1000],
               ['/preview/', 1600]];

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
      'GRID_GROUPS', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'MARKS', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS,
      GROUPING, PAGE, TIERS, UNREVIEWED, MARKS, fn => fn());
  } catch (e) {
    console.log('FAIL the script threw on load: ' + e.message);
    process.exit(1);
  }

  // --- the filters ------------------------------------------------------------
  const chips = document.byId.chips;
  const menu = document.byId.menu;
  const labels = el => el.querySelectorAll('.opt')
    .map(o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
  const on = () => chips.children.filter(c => c._classes.has('on'));
  const off = () => chips.children.filter(c => c._classes.has('off'));
  const plus = () => chips.children.find(c => c._classes.has('addchip'));

  // The ones doing something carry their value; the rest are there as their
  // own glyph. Spelled out they were a row of words saying nothing in front
  // of the one or two that are the address of what you are looking at — which
  // was true of the words and is not true of the drawings.
  check('the filter in use is on the bar', on().length === 1,
        chips.children.map(c => c.innerHTML).join('|'));
  check('and it says what it is set to', on()[0].innerHTML.includes('2026'),
        on()[0].innerHTML);
  check('it is drawn rather than named', on()[0].innerHTML.includes('<svg'),
        on()[0].innerHTML);
  check('but it still says which question it is, for a pointer and a reader',
        on()[0].attrs['aria-label'] === 'Date: 2026', on()[0].attrs['aria-label']);
  check('the unused ones are on the bar too, as their glyph alone',
        off().length === 2, off().map(c => c.innerHTML).join('|'));
  check('carrying no value, because they have none',
        off().every(c => !c.innerHTML.includes('class="val"')));
  // Both are rendered every time. Which of them is on screen is the
  // stylesheet's business, because it can change while the page is open by
  // turning the phone over.
  check('and the + is still there for a screen with no room for them',
        !!plus());

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

  // --- a decision about a folder --------------------------------------------
  // One zoom out, the same gesture: a folder is a set of filters, so a
  // decision about it is a decision about every file that link would open.
  const actions = document.byId.actions;
  const act = name => actions.querySelectorAll('[data-act]')
    .find(b => b.dataset.act === name);
  const pickOf = t => t.children.find(k => k._classes.has('pick'));

  check('nothing is selected to begin with',
        document.byId.selcount.textContent === '0 selected',
        document.byId.selcount.textContent);
  check('so the actions are not on screen',
        actions.querySelectorAll('.grp').every(g => g.hidden));

  went = null;
  pickOf(tiles[1]).click();
  check('a folder can be selected',
        document.byId.selcount.textContent === '1 selected',
        document.byId.selcount.textContent);
  check('and it says so on the card', tiles[1]._classes.has('picked'));
  check('now the actions are', actions.querySelectorAll('.grp')
        .some(g => !g.hidden));
  // The circle is inside the link that opens the folder, so it has to say it
  // is not that.
  check('selecting it did not open it', went === null, String(went));

  // The whole point: four controls, and not one of the four that needs a
  // photograph to mean anything.
  const offered = actions.querySelectorAll('[data-act]').map(b => b.dataset.act);
  check('the folder bar offers what a set can answer',
        offered.join(',') === 'event,tags,access,download', offered.join(','));

  act('tags').click();
  await settle(); await settle();
  check('the menu opens on the values already in use', menu.hidden === false);

  // Read when something is actually ticked, not when the menu opens: the
  // folder may hold thousands, and opening a menu is not asking for them.
  const n = calls.length;
  const row = menu.querySelectorAll('.opt')[0];
  check('there is a name to tick', !!row);
  if (row) {
    row.click();
    for (let k = 0; k < 8; k++) await settle();
    const read = calls.slice(n).filter(c => c.url.startsWith('/api/files'));
    check('ticking one reads what the folder holds', read.length === 1,
          String(read.length));
    check('by the query that opens it, minus the grouping',
          !!read[0] && read[0].url.includes('date=2026')
          && !read[0].url.includes('group='), read[0] && read[0].url);

    const wrote = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('and writes to the files, not to the folder', wrote.length === 1,
          String(wrote.length));
    const sent = wrote.length ? JSON.parse(wrote[0].body) : { files: [] };
    check('every file the folder holds', sent.files.length === 2,
          JSON.stringify(sent.files));
    check('named as files', sent.files.every(f => f.folder && f.name),
          JSON.stringify(sent.files));
  }

  // --- the card changes under the menu ---------------------------------------
  // A write that finishes and leaves the card saying what it said before is a
  // write with nothing on screen to show for it. It used to wait for the menu
  // to be dismissed and then reload the page, so ticking a name appeared to
  // do nothing at all until you clicked away.
  {
    const audience = () => (tiles[1].children.find(
      k => k._classes.has('spread') && k._classes.has('audience')
    ) || { innerHTML: '' }).innerHTML || '';

    // What is left to decide reads as a chip like every other fact about
    // the folder, and says the name and nothing else — one of the two files
    // here is shared and the other is not.
    check('what is undecided is a chip like the rest',
          /class="none"[^>]*>undecided<\/i>/.test(audience()), audience());
    check('and no chip carries a percentage', !/%/.test(audience()),
          audience());

    act('access').click();
    await settle(); await settle();
    const before = audience();
    check('the card does not say family yet', !/family/.test(before), before);

    const row = menu.querySelectorAll('.opt')[0];
    check('there is a name to tick', !!row);
    if (row) {
      row.click();
      for (let k = 0; k < 8; k++) await settle();
      check('the menu is still open to tick another', menu.hidden === false);
      check('and the card underneath it has changed',
            /family/.test(audience()), audience());
      check('and the new one is named plainly too',
            />family<\/i>/.test(audience()), audience());
      check('and nothing is undecided any more',
            !/undecided/.test(audience()), audience());
    }
  }

  // --- a chip opens the folder cut down to itself ----------------------------
  // The card could say *half of this event is undecided* and the page could
  // not then take you to that half. Pressed after a write on purpose: the
  // chips are redrawn from the files that just changed, so a handler hung on
  // each one would go in the bin with it — the chips would work until the
  // first edit and then stop, which is the kind of thing nobody reports
  // because it looks like they never worked.
  {
    const spread = tiles[1].children.find(
      k => k._classes.has('spread') && k._classes.has('audience'));
    const chip = spread.children.find(k => k.dataset.col);
    check('a chip carries the filter it stands for', !!chip,
          spread.innerHTML);
    if (chip) {
      check('by the column the filter bar uses',
            chip.dataset.col === 'audience', chip.dataset.col);
      went = null;
      chip.click();
      check('pressing it opens the folder, narrowed to that',
            !!went && went.includes('date=2026')
            && went.includes('audience=family'), String(went));
      check('and not merely the folder', went !== '/browse?date=2026',
            String(went));
    }
  }

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok');
})().catch(e => { console.log('FAIL ' + (e && e.stack)); process.exit(1); });
