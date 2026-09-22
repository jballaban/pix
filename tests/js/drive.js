// Run the browse page's script the way a curator drives it, and assert what it
// does. Not a browser test — a smoke test for the failures that keep recurring:
// a runtime error that silently kills every handler, a menu that cannot be
// dismissed, and a request built with the wrong shape.
//
// Usage: node drive.js <path to the extracted browse script>
'use strict';
const fs = require('fs');
const { El, document } = require('./dom.js');

const js = fs.readFileSync(process.argv[2], 'utf8');
const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail ? ': ' + detail : ''));
}

// --- the page the server renders ---------------------------------------------
function mk(id, cls) {
  const e = new El('div');
  e.id = id;
  if (cls) e.className = cls;
  document.byId[id] = e;
  document.appendChild(e);
  return e;
}

function cell(name, audience) {
  const c = new El('div');
  c.className = 'cell';
  Object.assign(c.dataset, {
    folder: 'f', name, kind: 'image', audience: audience || '', tags: '',
    date: '2026-01-01',
  });
  const pick = new El('button');
  pick.className = 'pick';
  c.appendChild(pick);
  return c;
}

const grid = mk('grid');
// A heading and its two cells, the way the server lays a section out.
const heading = new El('h3');
heading.className = 'group';
const pick = new El('button');
pick.className = 'grppick';
heading.appendChild(pick);
// One heading per section, reading as a path: each crumb is a level.
const crumb = new El('span');
crumb.className = 'crumb';
crumb.dataset.level = '0';
for (const cls of ['grpname', 'rmgrp']) {
  const b = new El('button');
  b.className = cls;
  crumb.appendChild(b);
}
heading.appendChild(crumb);
const addBtn = new El('button');
addBtn.className = 'addgrp';
heading.appendChild(addBtn);
grid.appendChild(heading);
const cells = [cell('a.jpg', 'ghost'), cell('b.jpg', '')];
cells.forEach(c => grid.appendChild(c));

const actions = mk('actions');
// A tri-state tick, a count, and two sets of actions — one per side of the
// deletion line, each shown only when the selection holds files it applies to.
// One button carrying the current size, as the page renders it.
const sizePick = mk('sizepick');

const badgeOn = c => c.children.find(k => k._classes.has('stack')) || null;
// Choosing a top is a control on the photograph now, not the photograph
// itself: clicking one opens it, here as everywhere else.
const chooseOn = c => c.children.find(k => k._classes.has('choose')) || null;
// Still asking, i.e. the choose controls are on the page.
const choosing_still = () => grid.children.some(c => !!chooseOn(c));
const tick = new El('button');
tick.id = 'selall';
tick.className = 'tick';
document.byId.selall = tick;
actions.appendChild(tick);
function actGroup(side, names) {
  const g = new El('span');
  g.className = 'grp';
  g.dataset.side = side;
  g.hidden = true;
  for (const act of names) {
    const b = new El('button');
    b.dataset.act = act;
    g.appendChild(b);
  }
  actions.appendChild(g);
  return g;
}
const liveActs = actGroup('live',
  ['event', 'tags', 'date', 'access', 'stack', 'top', 'unstack',
   'nostack', 'download', 'delete']);
const goneActs = actGroup('gone', ['restore', 'purge']);
const chooseActs = actGroup('choose', []);
const cancelBtn = new El('button');
cancelBtn.id = 'choosecancel';
document.byId.choosecancel = cancelBtn;
chooseActs.appendChild(cancelBtn);
// Buttons are nested in their group now, so they are found by walking rather
// than by looking at the row's own children.
const actBtn = name => actions.querySelectorAll('[data-act]')
                              .find(b => b.dataset.act === name);
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

const realAdd = document.addEventListener.bind(document);
document.addEventListener = (t, fn) => {
  (keys[t] ||= []).push(fn);
  realAdd(t, fn);
};
const realQsa = document.querySelectorAll.bind(document);
document.querySelectorAll = sel => (sel === '.cell' ? cells
                                  : sel === '.group' ? [heading]
                                  : sel === '.stage' ? [stage] : realQsa(sel));
document.querySelector = sel => document.querySelectorAll(sel)[0] || null;

// --- browser globals ----------------------------------------------------------
const calls = [];
// Which files the server says no longer match the filters. A test sets this
// to make a write push things out of the view, which is when the grid has to
// tidy up after itself.
let dropping = [];
// What a stack says is behind it, as the markup the grid is made of.
let behindCells = '';
// What the server says is waiting in the bin after a write.
let binned = 0;
// Pressed while a write is running, to check it stops between chunks.
let stopAfter = null;
const fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body });
  if (stopAfter !== null && url.startsWith('/api/decide')
      && calls.filter(c => c.url.startsWith('/api/decide')).length >= stopAfter) {
    document.byId.workstop.click();
  }
  if (url.startsWith('/api/suggest')) {
    if (/column=event/.test(url)) {
      // Whole names, as the server stores them: an event and, where there is
      // one, its sub-event in the same string.
      return { ok: true, json: async () => [
        { value: 'Sicily', n: 4, scope: 'all' },
        { value: 'Sicily > Taormina', n: 2, scope: 'all' },
        { value: 'Sicily > Catania', n: 1, scope: 'all' },
        { value: 'Cornwall', n: 3, scope: 'all' },
      ] };
    }
    return { ok: true, json: async () => [{ value: 'ghost', n: 1, scope: 'all' }] };
  }
  if (url.startsWith('/api/behind/')) {
    return { ok: true, json: async () => ({ cells: behindCells }) };
  }
  if (url.startsWith('/api/file/')) {
    return { ok: true, json: async () => ({ name: 'a.jpg', exif: {}, facts: [] }) };
  }
  return { ok: true,
           json: async () => ({ failed: [], dropped: dropping, total: 2,
                                purged: 1, binned }) };
};
// A real one, so a preference that is supposed to survive a reload can be
// checked to survive a reload rather than checked to have been written.
const stored = {};
const localStorage = {
  getItem: k => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
};
const listeners = {};
// Enough of a viewport to check scroll anchoring: `scrolled` is how far down
// the page is, and the test grid's rectangles are computed from it.
let scrolled = 0;
const window = {
  innerWidth: 1400, scrollY: 0, devicePixelRatio: 1,
  scrollBy: (_x, y) => { scrolled += y; window.scrollY = scrolled; },
  scrollTo: (_x, y) => { scrolled = y; window.scrollY = y; },
  addEventListener: (t, fn) => ((listeners[t] ||= []).push(fn)),
};
let reloaded = 0;
// Where the page was sent, which for a download is the whole gesture: the
// browser takes the transfer and the page stays where it is.
let went = null;
const location = { get href() { return went === null ? '/browse' : went; },
                   set href(v) { went = v; },
                   reload: () => { reloaded += 1; } };
const confirm = () => true;
const VIEW = { event: null, year: null, tag: null, audience: null, kind: null,
               band: null };
const GRID_GROUPS = [['day', 'By day'], ['event', 'By event'],
                     ['subevent', 'By event and sub-event'],
                     ['none', 'Ungrouped']];
const ONE_FIELD = { event: 'event', subevent: 'event' };
const GROUPING = ['day'];
const CHIPS = [['event', 'Event'], ['audience', 'Access']];
const FIXED = {};
const EXTRA = { audience: [['new', 'New']] };
const ADMIN = true;
const USERS = ['family', 'james'];
const GROUPS = ['family'];
const USUAL = 'family';
// The audience value meaning *nobody yet*, as the server sends it.
const UNREVIEWED = 'new';
// What separates an event from a sub-event in one name.
const EVENT_SEP = ' > ';
const PAGE = '/browse';
// What each derived tier is capped at, longest edge.
const TIERS = [['/thumb/', 400], ['/large/', 1000],
               ['/preview/', 1600]];

const settle = () => new Promise(r => setImmediate(r));
// The control selects everything when nothing is ticked and clears otherwise,
// so pressing it blindly to "reset" would do the opposite half the time.
function deselect() {
  if (document.byId.selcount.textContent !== '0 selected') tick.click();
}
const keys = {};
function arrow(key, opts) {
  (keys.keydown || []).forEach(fn => fn(Object.assign(
    { key, preventDefault() {}, target: { tagName: 'DIV' } }, opts || {})));
}

(async () => {
  try {
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL', 'GRID_GROUPS', 'ONE_FIELD', 'GROUPING',
      'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, fn => fn());
  } catch (e) {
    console.log('FAIL the script threw on load: ' + e.message);
    process.exit(1);
  }

  // The row is always there — it carries the count and the tick — but an
  // action is on screen only when the selection holds files it applies to.
  check('the row is on screen with nothing selected', actions.hidden === false);
  check('but neither set of actions is',
        liveActs.hidden === true && goneActs.hidden === true);

  cells[1].querySelector('.pick').click();
  check('selecting a living file offers the living actions',
        liveActs.hidden === false, 'live group still hidden');
  check('and not the ones for deleted files', goneActs.hidden === true);
  check('selection is counted',
        document.byId.selcount.textContent === '1 selected',
        document.byId.selcount.textContent);

  // Opening Access lists both the accounts and whatever is already there.
  const access = actBtn('access');
  access.click();
  await settle(); await settle();
  const menu = document.byId.menu;
  check('the access menu opens', menu.hidden === false);
  const opts = menu.querySelectorAll('.opt');
  const labels = opts.map(o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
  check('accounts are offered', labels.includes('james'), labels.join(','));
  check('a stale grant is still listed', labels.includes('ghost'),
        labels.join(','));

  // Ticking one issues an add for the selection.
  const before = calls.length;
  opts[labels.indexOf('james')].click();
  await settle(); await settle();
  const sent = calls.slice(before).filter(c => c.url.startsWith('/api/decide'));
  check('ticking sends one write', sent.length === 1, String(sent.length));
  if (sent.length) {
    const body = JSON.parse(sent[0].body);
    check('it adds rather than replaces',
          Array.isArray(body.add_audience) && body.add_audience[0] === 'james',
          sent[0].body);
    check('it names the selected file',
          body.files.length === 1 && body.files[0].name === 'b.jpg',
          sent[0].body);
  }

  // Roles come before individuals; a name that is no longer an account is
  // still listed, so a stale grant stays removable.
  const bands = menu.querySelectorAll('.band').map(b => b.textContent);
  check('groups are listed first', bands.indexOf('Groups') >= 0,
        bands.join('|'));
  check('a dead grant is grouped apart',
        bands.includes('No longer an account'), bands.join('|'));

  // Clicking outside dismisses.
  grid.click();
  check('a click outside dismisses the menu', menu.hidden === true);

  // The viewer closes on a click beside the picture — and looking at one
  // photograph does not cost a selection. A mis-aimed click on a thumbnail
  // used to throw away hundreds of gestures with nothing that could undo it.
  check('a selection is standing before the viewer opens',
        document.byId.selcount.textContent === '1 selected',
        document.byId.selcount.textContent);
  cells[0].click();
  await settle();
  check('clicking a photo opens the viewer',
        document.byId.viewer.classList.contains('on'));
  check('and the selection it was opened over survives',
        cells[1].classList.contains('picked'),
        document.byId.selcount.textContent);
  // Paged right and back again: the cursor visits a file that is not the
  // selected one, which is where paging would otherwise reselect underneath.
  arrow('ArrowRight'); arrow('ArrowLeft');
  check('paging past it leaves the selection alone too',
        cells[1].classList.contains('picked')
        && !cells[0].classList.contains('picked'),
        document.byId.selcount.textContent);
  stage.click();
  check('clicking beside the picture closes it',
        !document.byId.viewer.classList.contains('on'));
  check('and it is still selected on the way out',
        document.byId.selcount.textContent === '1 selected',
        document.byId.selcount.textContent);

  // Looking changes nothing, with or without a selection standing. The viewer
  // used to tick what you opened when nothing was selected and leave things
  // alone otherwise — two behaviours for one gesture, and it showed: the first
  // photograph you opened got ticked and the next one did not.
  deselect();
  cells[0].click();
  await settle();
  check('opening with nothing selected still selects nothing',
        document.byId.selcount.textContent === '0 selected',
        document.byId.selcount.textContent);
  arrow('ArrowRight');
  check('and paging selects nothing either',
        document.byId.selcount.textContent === '0 selected'
        && !cells[0].classList.contains('picked')
        && !cells[1].classList.contains('picked'),
        document.byId.selcount.textContent);
  stage.click();

  // Unticking leaves nothing selected, and nothing is implicitly targeted.
  // The old model left the cursor on the cell: not ticked, the count saying
  // none, and the actions quietly applying to it anyway.
  // From a clean selection: looking at a photograph no longer clears one, so
  // what the viewer left ticked would otherwise still be ticked here.
  deselect();
  cells[0].querySelector('.pick').click();
  check('ticking one selects it',
        document.byId.selcount.textContent === '1 selected',
        document.byId.selcount.textContent);
  cells[0].querySelector('.pick').click();
  check('unticking empties the selection',
        document.byId.selcount.textContent === '0 selected',
        document.byId.selcount.textContent);
  {
    const n = calls.length;
    const acc = actBtn('access');
    acc.click();
    await settle(); await settle();
    const rows = menu.querySelectorAll('.opt');
    if (rows.length) rows[0].click();
    await settle(); await settle();
    check('nothing is changed with nothing selected',
          !calls.slice(n).some(c => c.url.startsWith('/api/decide')));
    grid.click();
  }

  // Arrows do nothing in the grid. The mouse is the interface there, and a
  // key that moved a cursor which was also a selection is what kept inventing
  // selections nobody had made.
  document.byId.grid.click();
  deselect();
  const at = cells.findIndex(c => c.classList.contains('cur'));
  arrow('ArrowRight'); arrow('ArrowDown'); arrow('ArrowUp');
  check('arrows do not move the cursor in the grid',
        cells.findIndex(c => c.classList.contains('cur')) === at,
        'moved from ' + at);
  check('and do not select in the grid',
        document.byId.selcount.textContent === '0 selected',
        document.byId.selcount.textContent);
  // What follows still needs something to act on.
  cells[0].querySelector('.pick').click();

  // The date menu opens on what the files actually say.
  const date = actBtn('date');
  date.click();
  await settle();
  const dy = document.byId.dy;
  check('the date menu prefills the year', dy && dy.attrs.value === '2026',
        dy ? String(dy.attrs.value) : 'no year box');
  check('the date menu says what is there now',
        menu.innerHTML.includes('2026-01-01'), menu.innerHTML.slice(0, 120));

  // The clear button acts on the selection rather than throwing.
  const clr = document.byId.dclr;
  if (clr) {
    const n = calls.length;
    clr.click();
    await settle(); await settle();
    check('clearing the date sends a write',
          calls.slice(n).some(c => c.url.startsWith('/api/decide')));
  }


  // Delete writes `deleted` like any other decision. There is no value to
  // pick, so it asks instead of opening a menu — and a soft delete is undone
  // from History, which is why a confirm is enough ceremony for it.
  deselect();
  cells[0].querySelector('.pick').click();
  {
    const n = calls.length;
    const del = actBtn('delete');
    check('there is a delete action', !!del);
    del.click();
    await settle(); await settle();
    check('deleting opens no menu', menu.hidden === true);
    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('deleting sends one write', sent.length === 1, String(sent.length));
    if (sent.length) {
      const body = JSON.parse(sent[0].body);
      check('and what it sends is the flag', body.deleted === true,
            sent[0].body);
      check('naming the selected file',
            body.files.length === 1 && body.files[0].name === 'a.jpg',
            sent[0].body);
    }
  }

  // Reaching for an event sends what the selection spans, so the server can
  // propose the events already covering those days.
  {
    deselect();
    // The delete above marked `a.jpg` deleted, and the span is taken from the
    // living half of the selection.
    cells.forEach(c => { c.dataset.deleted = ''; c.classList.remove('gone'); });
    cells[0].dataset.date = '2026-07-26-10:00:00';
    cells[1].dataset.date = 'no date';
    cells[0].querySelector('.pick').click();
    cells[1].querySelector('.pick').click();
    const n = calls.length;
    actBtn('event').click();
    await settle(); await settle();
    const asked = calls.slice(n).find(c => c.url.startsWith('/api/suggest'));
    check('the event menu asks about the selection dates', !!asked,
          'no suggest call');
    if (asked) {
      check('sending the span it covers',
            asked.url.includes('near_from=2026-07-26-10%3A00%3A00')
            && asked.url.includes('near_to=2026-07-26-10%3A00%3A00'),
            asked.url);
      // An undated file left in would stretch the range across the library.
      check('and leaving the undated one out of it',
            !asked.url.includes('no+date') && !asked.url.includes('no%20date'),
            asked.url);
    }
    grid.click();
    deselect();
    cells[0].dataset.date = '2026-01-01';
    cells[1].dataset.date = '2026-01-01';
  }

  // Tags are not occasions, so reaching for one asks about no dates at all.
  {
    deselect();
    cells[0].querySelector('.pick').click();
    const n = calls.length;
    actBtn('tags').click();
    await settle(); await settle();
    const asked = calls.slice(n).find(c => c.url.startsWith('/api/suggest'));
    check('a tag menu asks for no span', asked && !asked.url.includes('near_'),
          asked ? asked.url : 'no suggest call');
    grid.click();
    deselect();
  }

  // A mixed selection offers both sets, and each acts only on the files it
  // means. This is the whole reason the sides follow the selection rather than
  // the filter: under `Including deleted` a day holds both kinds.
  //
  // `deleted` is read off the cell each time, so marking one is enough — no
  // second copy of the script, which would leave two sets of handlers writing
  // to the same row and neither of them right. The state is set explicitly
  // because the delete above left `a.jpg` marked: that write paints the cell
  // immediately, which is the behaviour, not a leak.
  {
    deselect();
    cells[0].dataset.deleted = '';
    cells[0].classList.remove('gone');
    cells[1].dataset.deleted = '1';

    cells[1].querySelector('.pick').click();
    check('a deleted file offers restore and purge', goneActs.hidden === false);
    check('and not the actions for living files', liveActs.hidden === true);

    cells[0].querySelector('.pick').click();
    check('a mixed selection offers both',
          liveActs.hidden === false && goneActs.hidden === false);

    // Purge first: it is the side that would otherwise be contaminated by the
    // delete below, which marks its file deleted the moment it is pressed.
    const m = calls.length;
    actBtn('purge').click();
    await settle(); await settle();
    const purges = calls.slice(m).filter(c => c.url.startsWith('/api/purge'));
    check('purging goes to its own endpoint', purges.length === 1,
          String(purges.length));
    if (purges.length) {
      const body = JSON.parse(purges[0].body);
      check('and names only the deleted file',
            body.files.length === 1 && body.files[0].name === 'b.jpg',
            purges[0].body);
    }

    // Delete is the other side, and takes only the living file.
    const n = calls.length;
    actBtn('delete').click();
    await settle(); await settle();
    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('deleting a mixed selection sends one write', sent.length === 1,
          String(sent.length));
    if (sent.length) {
      const body = JSON.parse(sent[0].body);
      check('naming only the living file',
            body.files.length === 1 && body.files[0].name === 'a.jpg',
            sent[0].body);
    }
    check('and the file it deleted now shows as deleted',
          cells[0].classList.contains('gone'));

    cells[0].dataset.deleted = '';
    cells[0].classList.remove('gone');
    cells[1].dataset.deleted = '';
    deselect();
  }

  // The bin count is rendered with the page, so a write has to say what it is
  // now. A number that only refreshes on reload is worse than none, because it
  // looks current.
  {
    deselect();
    cells.forEach(c => { c.dataset.deleted = ''; c.classList.remove('gone'); });
    const bin = document.byId.bincount;
    bin.hidden = true;
    cells[0].querySelector('.pick').click();
    binned = 3;
    actBtn('delete').click();
    await settle(); await settle();
    check('deleting updates the standing bin count',
          bin.textContent === '3 deleted', bin.textContent);
    check('and shows it once there is something in it', bin.hidden === false);

    // Emptying it puts the count away rather than leaving a nag at zero.
    deselect();
    cells[0].querySelector('.pick').click();
    binned = 0;
    actBtn('purge').click();
    await settle(); await settle();
    check('and hides it again when the bin empties', bin.hidden === true,
          bin.textContent);
    binned = 0;
    cells.forEach(c => { c.dataset.deleted = ''; c.classList.remove('gone'); });
    deselect();
  }

  // A stack is one file speaking for several: the first ticked keeps its
  // place, the rest record that they defer to it.
  {
    deselect();
    cells.forEach(c => { c.dataset.deleted = ''; c.dataset.under = '';
                         c.dataset.behind = '0'; c.classList.remove('gone'); });
    check('nothing to stack with one file selected', actBtn('stack').hidden !== false
          || document.byId.selcount.textContent === '0 selected');

    cells[1].querySelector('.pick').click();
    check('one file alone cannot be stacked', actBtn('stack').hidden === true);
    check('and is not in a stack to be taken out of',
          actBtn('unstack').hidden === true);

    cells[0].querySelector('.pick').click();
    check('two can', actBtn('stack').hidden === false);

    // Stacking asks which one to show rather than taking the first ticked.
    const n = calls.length;
    actBtn('stack').click();
    await settle();
    check('it writes nothing yet', calls.slice(n).filter(
      c => c.url.startsWith('/api/decide')).length === 0);
    check('the grid narrows to the files being stacked',
          cells[0].hidden === false && cells[1].hidden === false
          && heading.hidden === true);
    check('and the only controls are the ones for choosing',
          chooseActs.hidden === false && liveActs.hidden === true);

    check('every candidate offers itself', !!chooseOn(cells[0]));
    // The thing that caught somebody out: clicking a photograph to see it
    // properly answered the question instead, and the grid came back.
    {
      const before = calls.length;
      cells[1].click();
      await settle();
      check('and clicking one opens it rather than choosing it',
            document.byId.viewer.classList.contains('on')
            && !!choosing_still(),
            'a click chose instead of opening');
      check('without writing anything',
            calls.slice(before).filter(
              c => c.url.startsWith('/api/decide')).length === 0);
      stage.click();
    }
    const pickTop = chooseOn(cells[0]);
    if (pickTop) pickTop.click();        // this one shows
    for (let i = 0; i < 6; i++) await settle();
    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('choosing one sends the write', sent.length === 1, String(sent.length));
    if (sent.length) {
      const body = JSON.parse(sent[0].body);
      check('the others defer to the one chosen',
            body.stacked_under === 'f/a.jpg', sent[0].body);
      check('and it is not asked to defer to itself',
            body.files.length === 1 && body.files[0].name === 'b.jpg',
            sent[0].body);
    }
    check('the grid comes back', heading.hidden === false
          && cells[1].hidden === false);
    check('and the one left standing says how many it speaks for',
          cells[0].dataset.behind === '1'
          && !!cells[0].querySelector('.stack'),
          cells[0].dataset.behind);
    // The selection used to survive, still holding the file that had just
    // become a top — so the next things ticked were stacked *with it*, and its
    // own members ended up behind a file that was itself behind something.
    check('and the selection is spent',
          document.byId.selcount.textContent === '0 selected',
          document.byId.selcount.textContent);
    deselect();
    cells.forEach(c => { c.dataset.under = ''; c.dataset.behind = '0'; });
  }

  // Changing your mind puts the grid back and writes nothing.
  {
    deselect();
    cells[0].querySelector('.pick').click();
    cells[1].querySelector('.pick').click();
    const n = calls.length;
    actBtn('stack').click();
    await settle();
    document.byId.choosecancel.click();
    await settle();
    check('cancelling writes nothing', calls.slice(n).filter(
      c => c.url.startsWith('/api/decide')).length === 0);
    check('and puts every file back',
          cells.every(c => c.hidden === false) && heading.hidden === false);
    check('with the ordinary controls again',
          liveActs.hidden === false && chooseActs.hidden === true);
    deselect();
  }

  // Taking one out of a stack is offered only for files that are in one.
  {
    deselect();
    cells[0].dataset.under = 'f/b.jpg';
    cells[0].querySelector('.pick').click();
    check('a stacked file can be taken out', actBtn('unstack').hidden === false);
    check('and can be made the one that shows',
          actBtn('top').hidden === false);

    const n = calls.length;
    actBtn('unstack').click();
    await settle(); await settle();
    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('unstacking sends one write', sent.length === 1, String(sent.length));
    if (sent.length) {
      const body = JSON.parse(sent[0].body);
      check('clearing what it deferred to', body.stacked_under === null,
            sent[0].body);
    }
    cells[0].dataset.under = '';
    deselect();
  }

  // Making a new top must touch that stack and nothing else.
  {
    deselect();
    cells.forEach(c => { c.dataset.under = ''; c.dataset.behind = '0'; });
    // a.jpg is the top of a stack; b.jpg is an ordinary file beside it.
    cells[0].dataset.behind = '2';
    cells[1].querySelector('.pick').click();   // select the unrelated file
    deselect();
    cells[0].querySelector('.pick').click();   // select the stack top
    check('the file that already shows is not offered a promotion',
          actBtn('top').hidden === true);
    // But it is in a stack, whatever the old test thought: it is the one the
    // depth badge is drawn on.
    check('and it can be taken apart', actBtn('unstack').hidden === false);
    const n = calls.length;
    actBtn('top').click();
    await settle(); await settle();
    const named = calls.slice(n)
      .filter(c => c.url.startsWith('/api/decide'))
      .flatMap(c => JSON.parse(c.body).files.map(f => f.name));
    check('and reaching it anyway touches nothing', named.length === 0,
          named.join(','));
    cells[0].dataset.behind = '0';
    deselect();
  }

  // Inside an opened stack, promoting one swaps which file speaks: the chosen
  // one stops deferring and the old top starts.
  {
    deselect();
    cells[0].dataset.behind = '1';       // a.jpg currently speaks
    cells[1].dataset.under = 'f/a.jpg';  // b.jpg is behind it
    cells[1].querySelector('.pick').click();
    const n = calls.length;
    actBtn('top').click();
    for (let i = 0; i < 8; i++) await settle();
    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    // One write. Taking the new top out of what it was behind is the
    // server's half: a file everything defers to cannot be left deferring to
    // one of them, whoever asked for it.
    check('promoting takes one write', sent.length === 1, String(sent.length));
    if (sent.length === 1) {
      const body = JSON.parse(sent[0].body);
      check('the old top starts deferring to the new one',
            body.stacked_under === 'f/b.jpg'
            && body.files.length === 1 && body.files[0].name === 'a.jpg',
            sent[0].body);
    }
    cells[0].dataset.behind = '0';
    cells[1].dataset.under = '';
    deselect();
  }

  // Merging two stacks offers every photograph in both of them as the one to
  // show, not just the two that happen to be speaking.
  {
    deselect();
    cells.forEach(c => { c.dataset.under = ''; c.dataset.behind = '0'; });
    cells[0].dataset.behind = '1';    // a.jpg speaks for one more
    const depth = new El('a');
    depth.className = 'stack';
    depth._text = '2';
    cells[0].appendChild(depth);
    behindCells = '<div class="cell" data-folder="f" data-name="hidden.jpg"'
                + ' data-kind="image" data-audience="" data-event=""'
                + ' data-tags="" data-date="2026-01-01" data-deleted=""'
                + ' data-under="f/a.jpg" data-behind="0">'
                + '<button class="pick"></button></div>';

    cells[0].querySelector('.pick').click();
    cells[1].querySelector('.pick').click();
    // Somewhere down the grid, which is where stacking is done from.
    scrolled = 900; window.scrollY = 900;
    actBtn('stack').click();
    for (let i = 0; i < 8; i++) await settle();
    // The stub has no layout, so the collapse is applied by hand: hiding the
    // rest of the grid leaves a page a few rows tall, and a browser will not
    // hold a scroll position past the bottom of a document.
    scrolled = 0; window.scrollY = 0;

    const opened = grid.children.find(c => c.dataset.name === 'hidden.jpg');
    check('the stack being merged is opened up', !!opened,
          grid.children.map(c => c.dataset.name).join(','));
    check('and what came out of it can be chosen',
          !!opened && opened.hidden === false);
    // The count means *there are more of these, somewhere else*. They are
    // right here.
    check('the depth badge goes while its files are on screen',
          badgeOn(cells[0]) === null || badgeOn(cells[0]).hidden === true,
          'still claiming a closed stack');
    // In its place, what it was really saying. Merging stacks puts several of
    // these among photographs that look alike — which is why they were
    // stacked — and the one to keep is usually one of them.
    const wasTop = c => c.children.find(k => k._classes.has('top-mark'));
    check('and says instead which one was speaking', !!wasTop(cells[0]),
          'nothing marks the file the stack was showing');
    check('only the ones that were', !wasTop(cells[1]) && !wasTop(opened),
          'a file that was never a top is marked as one');
    // Any of them can be the one that shows, so none of them is ringed as
    // though it were already a different kind of candidate.
    check('nothing is selected while a top is being chosen',
          cells.every(c => !c.classList.contains('picked'))
          && (!opened || !opened.classList.contains('picked')),
          'a file was left looking chosen');
    check('and the tick and count go with the selection',
          document.byId.selall.hidden === true
          && document.byId.selcount.hidden === true);

    // Choose the file that was hidden inside a stack.
    const n = calls.length;
    check('what came out of the stack offers itself too',
          !!opened && !!chooseOn(opened));
    if (opened) chooseOn(opened).click();
    for (let i = 0; i < 8; i++) await settle();
    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('choosing it sends one write', sent.length === 1, String(sent.length));
    // No reload: the grid loses the files that went behind it and keeps the
    // one that came out, so the page never has to be fetched again and never
    // jumps back to the top.
    check('the page is not thrown away to do it', reloaded === 0,
          String(reloaded));
    check('and the one chosen stays, now speaking for the rest',
          !!grid.children.find(c => c.dataset.name === 'hidden.jpg'),
          'the promoted file was given back with the borrowed ones');
    // Hiding the rest of the grid collapses the page, and a browser will not
    // hold a scroll position past the bottom of a document.
    check('and you are still where you were when you asked',
          window.scrollY === 900, String(window.scrollY));
    if (sent.length) {
      const body = JSON.parse(sent[0].body);
      check('everything else defers to it',
            body.stacked_under === 'f/hidden.jpg', sent[0].body);
      check('including both of the ones that were speaking',
            body.files.length === 2, sent[0].body);
    }
    // It belongs in the grid now, but the blocks after this one count the
    // section it landed in.
    check('and the mark goes when the question is answered',
          !wasTop(cells[0]), 'a stale Top pill outlived the choosing');
    const stayed = grid.children.find(c => c.dataset.name === 'hidden.jpg');
    if (stayed) stayed.remove();
    behindCells = '';
    cells.forEach(c => { c.dataset.under = ''; c.dataset.behind = '0'; });
    deselect();
  }

  // Keeping the file that already speaks: what is already behind it stays put
  // rather than being written the value it already has.
  {
    deselect();
    reloaded = 0;
    cells.forEach(c => { c.dataset.under = ''; c.dataset.behind = '0'; });
    cells[0].dataset.behind = '1';
    behindCells = '<div class="cell" data-folder="f" data-name="already.jpg"'
                + ' data-kind="image" data-audience="" data-event=""'
                + ' data-tags="" data-date="2026-01-01" data-deleted=""'
                + ' data-under="f/a.jpg" data-behind="0">'
                + '<button class="pick"></button></div>';
    cells[0].querySelector('.pick').click();
    cells[1].querySelector('.pick').click();
    actBtn('stack').click();
    for (let i = 0; i < 8; i++) await settle();

    const n = calls.length;
    const keepTop = chooseOn(cells[0]);
    check('the file already speaking is a candidate too', !!keepTop);
    if (keepTop) keepTop.click();     // keep a.jpg as the one that shows
    for (let i = 0; i < 8; i++) await settle();
    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    if (sent.length) {
      const named = JSON.parse(sent[0].body).files.map(f => f.name);
      check('only the file that was not already behind it is written',
            named.length === 1 && named[0] === 'b.jpg', named.join(','));
    } else {
      check('keeping the existing top still writes something', false);
    }
    behindCells = '';
    cells[0].dataset.behind = '0';
    deselect();
  }

  // Changing your mind about a merge leaves nothing borrowed behind.
  {
    deselect();
    scrolled = 640; window.scrollY = 640;
    cells[0].dataset.behind = '1';
    behindCells = '<div class="cell" data-folder="f" data-name="borrowed.jpg"'
                + ' data-kind="image" data-audience="" data-event=""'
                + ' data-tags="" data-date="2026-01-01" data-deleted=""'
                + ' data-under="f/a.jpg" data-behind="0">'
                + '<button class="pick"></button></div>';
    cells[0].querySelector('.pick').click();
    cells[1].querySelector('.pick').click();
    actBtn('stack').click();
    for (let i = 0; i < 8; i++) await settle();
    scrolled = 0; window.scrollY = 0;      // the same collapse, by hand
    check('it was borrowed',
          !!grid.children.find(c => c.dataset.name === 'borrowed.jpg'));
    document.byId.choosecancel.click();
    await settle();
    check('and given back',
          !grid.children.find(c => c.dataset.name === 'borrowed.jpg'),
          'a file from inside a stack was left in the grid');
    check('at the place you were standing', window.scrollY === 640,
          String(window.scrollY));
    check('with the badge saying what it says again',
          badgeOn(cells[0]) === null || badgeOn(cells[0]).hidden === false,
          'the stack closed without its count');
    // Cancelling asked for none of it to have happened, which includes the
    // selection you arrived with.
    check('and the selection you had is back',
          document.byId.selcount.textContent === '2 selected',
          document.byId.selcount.textContent);
    check('with the tick and count back too',
          document.byId.selall.hidden === false
          && document.byId.selcount.hidden === false);
    behindCells = '';
    cells[0].dataset.behind = '0';
    deselect();
  }

  // The heading selects its whole section, and says so with three states.
  deselect();
  heading.querySelector('.grppick').click();
  check('a heading selects its section',
        document.byId.selcount.textContent === '2 selected',
        document.byId.selcount.textContent);
  check('and shows that all of it is selected', heading.dataset.state === 'all',
        String(heading.dataset.state));
  cells[0].querySelector('.pick').click();
  check('and says "some" when part of it is',
        heading.dataset.state === 'some', String(heading.dataset.state));

  // A crumb's name opens the grouping menu for that level.
  crumb.querySelector('.grpname').click();
  await settle();
  check('a crumb offers groupings', !menu.hidden);
  const groupRows = menu.querySelectorAll('.opt')
    .map(o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
  check('to change that level', groupRows.includes('By event'),
        groupRows.join(','));
  grid.click();

  // The cross on a crumb drops that level outright — no menu to go and find.
  crumb.querySelector('.rmgrp').click();
  check('the cross removes the grouping',
        /group=none/.test(location.href), location.href);
  location.href = '/browse';

  // `+` on a heading adds a level *inside* it rather than replacing it.
  addBtn.click();
  await settle();
  {
    const rows = menu.querySelectorAll('.opt');
    const labels = rows.map(
      o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
    check('the plus offers a grouping to nest',
          labels.includes('By event'), labels.join(','));
    // It hung off the day heading, and a day inside a day is nothing: the
    // level being added to counted as free before, so this row was there and
    // choosing it did not move the page at all.
    check('and not the level it is hanging off',
          !labels.includes('By day'), labels.join(','));
    check('and does not offer to remove the level it is adding to',
          !labels.includes('Remove this grouping'), labels.join(','));
    rows[labels.indexOf('By event')].click();
    check('choosing one keeps the outer level and adds inside it',
          /group=day%2Cevent|group=day,event/.test(location.href),
          location.href);
    location.href = '/browse';
  }

  // Event and sub-event read one column at two widths. Either inside the
  // other divides by a question the outer level has already answered, so
  // picking one takes the other off the menu — while a crumb sitting on one
  // still offers the swap to the other, which is a change of width, not a
  // second level of it.
  {
    // The same heading, re-bound by a page that is grouping by event.
    document.querySelectorAll = sel => (sel === '.cell' ? cells
                                      : sel === '.group' ? [heading]
                                      : sel === '.stage' ? [stage] : realQsa(sel));
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED',
      'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, ONE_FIELD, ['event'], PAGE, TIERS, UNREVIEWED, EVENT_SEP,
      fn => fn());

    const named = () => menu.querySelectorAll('.opt').map(
      o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);

    addBtn.click();
    await settle();
    let labels = named();
    check('grouping by event does not offer sub-event inside it',
          !labels.includes('By event and sub-event'), labels.join(','));
    check('nor a second helping of event',
          !labels.includes('By event'), labels.join(','));
    check('while the groupings that ask something else are still there',
          labels.includes('By day'), labels.join(','));
    grid.click();

    crumb.querySelector('.grpname').click();
    await settle();
    labels = named();
    check('but the crumb itself offers the swap to sub-event',
          labels.includes('By event and sub-event'), labels.join(','));
    grid.click();
    location.href = '/browse';
  }


  // A write that pushes files out of the view has to leave the sections
  // honest behind it: the count says what is there now, a heading whose last
  // file has gone goes with it, and nothing is ticked that nobody ticked.
  {
    const room = mk('sections');
    const mkHead = n => {
      const h = new El('h3');
      h.className = 'group';
      const gp = new El('button');
      gp.className = 'grppick';
      h.appendChild(gp);
      const d = new El('span');
      d.className = 'dim';
      d.textContent = String(n);
      h.appendChild(d);
      return h;
    };
    const h1 = mkHead(2), h2 = mkHead(1);
    const s1 = [cell('s1a.jpg', ''), cell('s1b.jpg', '')];
    const s2 = [cell('s2a.jpg', '')];
    room.appendChild(h1); s1.forEach(c => room.appendChild(c));
    room.appendChild(h2); s2.forEach(c => room.appendChild(c));
    const all = [...s1, ...s2];
    document.querySelectorAll = sel => (sel === '.cell' ? all
                                      : sel === '.group' ? [h1, h2]
                                      : sel === '.stage' ? [stage] : realQsa(sel));
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, fn => fn());

    // One file out of the first section, and the whole of the second.
    s1[0].querySelector('.pick').click();
    s2[0].querySelector('.pick').click();
    dropping = [{ folder: 'f', name: 's1a.jpg' },
                { folder: 'f', name: 's2a.jpg' }];
    const acc = actBtn('access');
    acc.click();
    await settle(); await settle();
    const rows = menu.querySelectorAll('.opt');
    const names = rows.map(
      o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
    rows[names.indexOf('james')].click();
    await settle(); await settle();
    dropping = [];

    check('a section that loses a file recounts',
          h1.querySelector('.dim').textContent === '1',
          h1.querySelector('.dim').textContent);
    check('a section that loses its last file goes too',
          !room.children.includes(h2));
    check('one that kept a file keeps its heading',
          room.children.includes(h1));
    // The cursor lands on a survivor so the keyboard has somewhere to resume,
    // but landing is not choosing: nobody asked for that move.
    check('nothing is left selected once the selection has gone',
          document.byId.selcount.textContent === '0 selected',
          document.byId.selcount.textContent);
    check('and the survivor is not ticked on the way past',
          !s1[1].classList.contains('picked'));
    // Nothing is selected any more, so a menu that acts on a selection has
    // nothing to act on; it used to sit open over a grid it could not touch.
    check('the menu closes when what it acted on has gone',
          menu.hidden === true);
    check('and both sets of actions go away with it',
          liveActs.hidden === true && goneActs.hidden === true);
    check('the takeover reported progress',
          document.byId.worktally.textContent === '2 of 2 files',
          document.byId.worktally.textContent);
    check('and is down once the write is done',
          !document.byId.working.classList.contains('on'));
  }

  // After a file leaves the grid, clicking a thumbnail must still open *that*
  // thumbnail. The handlers used to hold the index their cell had at load, and
  // `drop` rebuilds `cells` around the gap — so deleting one file made every
  // thumbnail below it open the picture one along.
  {
    const room = mk('shifted');
    // Four, and the click lands on the third. With three, dropping the first
    // makes the stale index point past the end and `setCur` clamps it back to
    // the right cell — the bug hides behind the clamp.
    const shelf = [cell('s0.jpg', ''), cell('s1.jpg', ''),
                   cell('s2.jpg', ''), cell('s3.jpg', '')];
    shelf.forEach(c => room.appendChild(c));
    document.querySelectorAll = sel => (sel === '.cell' ? shelf
                                      : sel === '.group' ? []
                                      : sel === '.stage' ? [stage] : realQsa(sel));
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, fn => fn());

    // Push the first one out of the view, the way an edit does.
    shelf[0].querySelector('.pick').click();
    dropping = [{ folder: 'f', name: 's0.jpg' }];
    actBtn('delete').click();
    await settle(); await settle();
    dropping = [];
    check('the edited file left the grid', !room.children.includes(shelf[0]));

    // Now click one in the middle. It must open itself, not its neighbour.
    stage.click();
    shelf[2].click();
    await settle();
    check('a thumbnail still opens itself after one leaves',
          shelf[2].classList.contains('cur'),
          shelf[3].classList.contains('cur') ? 'opened the one below it'
                                            : 'opened neither');
    stage.click();
  }

  // Working through the files at the top of the screen takes them from above
  // you, and everything left slides up — which reads as the page scrolling
  // down on its own, and loses the place you had got to.
  //
  // The stub does no layout, so it is given one: a column of 100px cells whose
  // position follows where they actually sit, and a window that really moves
  // when the page scrolls it.
  {
    const room = mk('scrollroom');
    const col = [];
    for (let i = 0; i < 6; i++) col.push(cell('c' + i + '.jpg', ''));
    col.forEach(c => {
      room.appendChild(c);
      Object.defineProperty(c, '_rect', {
        get() {
          const i = room.children.indexOf(c);
          return { left: 0, top: i * 100 - scrolled, width: 100, height: 100 };
        },
      });
    });
    document.querySelectorAll = sel => (sel === '.cell' ? col
                                      : sel === '.group' ? []
                                      : sel === '.stage' ? [stage] : realQsa(sel));
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, fn => fn());

    // Scrolled a couple of rows down: c2 straddles the top of the screen.
    scrolled = 250;
    const before = col[2].getBoundingClientRect().top;
    check('the anchor starts where the test says', before === -50,
          String(before));

    // Process the two above it, the way you work down a grid.
    col[0].querySelector('.pick').click();
    col[1].querySelector('.pick').click();
    dropping = [{ folder: 'f', name: 'c0.jpg' }, { folder: 'f', name: 'c1.jpg' }];
    actBtn('delete').click();
    await settle(); await settle();
    dropping = [];

    check('both left the grid',
          !room.children.includes(col[0]) && !room.children.includes(col[1]));
    check('and what was on screen is still where it was',
          col[2].getBoundingClientRect().top === before,
          'moved to ' + col[2].getBoundingClientRect().top);
    check('by scrolling back up, not by luck', scrolled === 50,
          String(scrolled));
  }

  // A big write can be stopped part way. Tagging four hundred files by mistake
  // and having to watch it finish is the thing this exists for — stop it, then
  // put back what actually landed from History.
  {
    const room = mk('stoproom');
    const many = [];
    for (let i = 0; i < 250; i++) many.push(cell('m' + i + '.jpg', ''));
    many.forEach(c => room.appendChild(c));
    document.querySelectorAll = sel => (sel === '.cell' ? many
                                      : sel === '.group' ? []
                                      : sel === '.stage' ? [stage] : realQsa(sel));
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, fn => fn());

    document.byId.selall.click();          // all 250
    const n = calls.length;
    stopAfter = 1;                          // stop once the first chunk is away
    actBtn('tags').click();
    await settle(); await settle();
    const menu2 = document.byId.menu;
    const rows = menu2.querySelectorAll('.opt');
    const names = rows.map(
      o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
    rows[names.indexOf('ghost')].click();
    for (let i = 0; i < 12; i++) await settle();
    stopAfter = null;

    const sent = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('it stops part way rather than running to the end',
          sent.length > 0 && sent.length < 3, String(sent.length));
    check('the files it had already written keep the tag',
          many[0].dataset.tags.includes('ghost'), many[0].dataset.tags);
    check('and the ones it never reached are left alone',
          !many[249].dataset.tags.includes('ghost'), many[249].dataset.tags);
    check('the takeover is down', !document.byId.working.classList.contains('on'));
    check('and it says where it got to',
          /stopped/.test(document.byId.note.textContent),
          document.byId.note.textContent);
  }

  // Thumbnail size is a preference about looking, so it says what it will do
  // rather than what is true, and it outlives the page.
  {
    check('it starts at the ordinary size', grid.dataset.size === 'small',
          grid.dataset.size);
    check('and carries the letter for it', sizePick.textContent === 'S',
          sizePick.textContent);

    sizePick.click();
    check('and the grid goes up a size', grid.dataset.size === 'medium',
          grid.dataset.size);
    check('with the letter following it', sizePick.textContent === 'M',
          sizePick.textContent);
    check('with the choice remembered', stored['pix2.thumb'] === 'medium',
          String(stored['pix2.thumb']));

    // Which tier a thumbnail is read from is not the size on the label. The
    // grid shows squares and the tiers are capped on the long edge, so the
    // square taken out of a 16:9 frame in the 1000px tier is 563 across — and
    // the same inch of glass wants twice the pixels on a retina screen. Both
    // of those are arithmetic the page can do and a fixed tier per size
    // cannot, which is why a video thumbnail went soft a size before a
    // photograph did.
    const img = new El('img');
    img.attrs.src = '/thumb/f/a.jpg';
    img.setAttribute = (k, v) => { img.attrs[k] = v; };
    img.getAttribute = k => img.attrs[k];
    // A cell the *current* script instance knows about: blocks above this one
    // re-ran the page against a different grid, and `cells` moved with them.
    const shown = document.querySelectorAll('.cell')[0];
    shown.appendChild(img);
    shown._rect = { left: 0, top: 0, width: 400, height: 400 };
    // Every redraw goes through the one control there is, so each of these
    // also advances the size — which the stub has no layout to care about:
    // the cell is whatever `_rect` says.
    const source = (ar, dpr) => {
      shown.dataset.ar = String(ar);
      window.devicePixelRatio = dpr;
      sizePick.click();
      return img.attrs.src.split('/')[1];
    };

    sizePick.click();                       // medium -> large
    check('and is labelled for it', sizePick.textContent === 'L',
          sizePick.textContent);
    // 400 across at one device pixel each. A 4:3 photograph in the 400px tier
    // is 300 on its short edge, which is not enough.
    const a = source(0.75, 1);
    check('a photograph too big for the thumbnail tier reads the next one up',
          a === 'large', a);
    // The same cell on a retina screen is 800 device pixels, and the square
    // out of the 1000px tier is 750.
    const b = source(0.75, 2);
    check('and on a retina screen the one above that', b === 'preview', b);
    // 16:9 gives 563 out of the same tier, so it runs out one step sooner.
    const d = source(0.5625, 1.5);
    check('a video frame runs out a step earlier than a photograph does',
          d === 'preview', d);

    // Covering the cell to the pixel is not enough: shown at 1:1 a video
    // frame looks nothing like the same cell filled from a photograph with
    // half again as many pixels to give away.
    shown._rect = { left: 0, top: 0, width: 380, height: 380 };
    const tight = source(1, 1);
    check('a source that only just covers the cell is not good enough',
          tight === 'large', tight);
    shown._rect = { left: 0, top: 0, width: 400, height: 400 };

    // One button, so it cycles: there is nowhere else to go from the end.
    window.devicePixelRatio = 1;
    shown.dataset.ar = '1';
    shown._rect = { left: 0, top: 0, width: 150, height: 150 };
    while (grid.dataset.size !== 'large') sizePick.click();
    sizePick.click();
    check('the next one round is back to the smallest',
          grid.dataset.size === 'small', grid.dataset.size);
    check('and a small square cell is what the thumbnail tier is for',
          img.attrs.src === '/thumb/f/a.jpg', img.attrs.src);
    img.remove();
    delete shown._rect;

    // A fresh page finds the preference where it was left.
    stored['pix2.thumb'] = 'large';
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, fn => fn());
    check('a new page opens at the size you left it',
          grid.dataset.size === 'large', grid.dataset.size);

    // What these were called for an afternoon.
    stored['pix2.thumb'] = 'huge';
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, fn => fn());
    check('and one saved under the old names still opens there',
          grid.dataset.size === 'large', grid.dataset.size);
  }

  // --- paging while a stack is open -------------------------------------------
  // The rest of the grid is hidden while a top is being chosen, not removed —
  // it comes back when you are done. So it is still in `cells`, and paging the
  // viewer walked straight through the stack and on into files that were not
  // on screen.
  {
    const room = new El('div');
    room.id = 'grid';
    document.byId.grid = room;
    const three = [cell('t1.jpg', ''), cell('t2.jpg', ''), cell('t3.jpg', '')];
    three.forEach(c => room.appendChild(c));
    document.querySelectorAll = sel => (sel === '.cell' ? three
                                      : sel === '.group' ? []
                                      : sel === '.stage' ? [stage] : realQsa(sel));
    // Only this run's handlers: the ones from the stages above close over
    // their own grids and would answer the same key press.
    keys.keydown = [];
    behindCells = '';
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, fn => fn());

    three[0].querySelector('.pick').click();
    three[1].querySelector('.pick').click();
    actBtn('stack').click();
    for (let i = 0; i < 8; i++) await settle();
    check('the file outside the stack is off screen', three[2].hidden === true);

    three[1].click();
    await settle();
    const showing = () => document.byId.vmeta.textContent.split(' ')[0];
    check('opening one of them shows it', showing() === 't2.jpg', showing());
    arrow('ArrowRight');
    await settle();
    check('paging on cannot reach what is off screen', showing() === 't2.jpg',
          showing());
    arrow('ArrowLeft');
    await settle();
    check('and paging back stays inside the stack', showing() === 't1.jpg',
          showing());
    arrow('ArrowLeft');
    await settle();
    check('as does paging back off the front', showing() === 't1.jpg',
          showing());
  }

  // --- taking a copy away -----------------------------------------------------
  {
    const room = new El('div');
    room.id = 'grid';
    document.byId.grid = room;
    const two = [cell('a.jpg', ''), cell('b.mp4', '')];
    two.forEach(c => room.appendChild(c));
    document.querySelectorAll = sel => (sel === '.cell' ? two
                                      : sel === '.group' ? []
                                      : sel === '.stage' ? [stage] : realQsa(sel));
    keys.keydown = [];
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'ONE_FIELD', 'GROUPING', 'PAGE', 'TIERS', 'UNREVIEWED', 'EVENT_SEP', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, ONE_FIELD, GROUPING, PAGE, TIERS, UNREVIEWED, EVENT_SEP, fn => fn());

    const get = actBtn('download');
    check('nothing selected offers no download', get.hidden === true);

    two[0].querySelector('.pick').click();
    check('one file does', get.hidden === false);

    // Nothing in this library has a playable copy of its own, so there is no
    // question to ask: pressing it downloads.
    went = null;
    get.click();
    await settle();
    check('and pressing it downloads that file',
          !!went && went.startsWith('/download/f/a.jpg'), String(went));
    check('without asking which of two identical things you meant',
          menu.hidden === true);

    // A clip a browser will not play has both, and only then is it a choice.
    two[0].dataset.copy = '1';
    went = null;
    get.click();
    await settle();
    check('a file with a copy of its own asks which', menu.hidden === false);
    const rows = menu.querySelectorAll('.opt');
    const labels = rows.map(
      o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
    check('offering the copy and the original',
          labels.join(',') === 'Playable copies,Originals', labels.join(','));
    if (rows.length > 1) {
      rows[1].click();
      await settle();
      check('and the original is asked for by name',
            !!went && went.includes('original=1'), String(went));
    }
    delete two[0].dataset.copy;

    // More than one cannot be a link: a browser will not start two hundred
    // downloads, and a folder of files is what you wanted anyway.
    document.submitted = [];
    went = null;
    two[1].querySelector('.pick').click();
    get.click();
    await settle();
    check('two files go as one posted form', document.submitted.length === 1,
          String(document.submitted.length));
    const form = document.submitted[0];
    if (form) {
      check('posted, because five hundred names do not fit in an address',
            form.method === 'post', String(form.method));
      check('to the zip', form.action === '/download.zip', String(form.action));
      const field = form.children.find(k => k.name === 'files');
      check('carrying what was selected',
            !!field && JSON.parse(field.value).length === 2,
            field && field.value);
    }
    check('and the page does not go anywhere itself', went === null,
          String(went));
  }

  // --- a half-ticked box completes, it does not clear ------------------------
  // One file carries `ghost` and the other does not, so with both selected
  // that box is *some*. Ticking it used to take the grant off the one that
  // had it — on a folder ninety per cent shared, one click for none of it —
  // which is the opposite of what ticking a box says, and the opposite of
  // what `event` did in the same menu under the same kind of box.
  //
  // Its own two files and its own state: by this point in the run the grid
  // has been stacked, written to and paged through, and a check that assumed
  // the opening position would be testing the blocks above it.
  {
    deselect();
    const two = document.querySelectorAll('.cell').slice(0, 2);
    two[0].dataset.audience = 'ghost';
    two[1].dataset.audience = '';
    two.forEach(c => c.children.find(k => k._classes.has('pick')).click());
    check('two files are selected',
          document.byId.selcount.textContent === '2 selected',
          document.byId.selcount.textContent);

    access.click();
    await settle(); await settle();
    const rows = document.byId.menu.querySelectorAll('.opt');
    const names = rows.map(
      o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
    const half = rows[names.indexOf('ghost')];
    check('one of them has it and the other does not',
          !!half && half.dataset.state === 'some',
          half && half.dataset.state);

    if (half) {
      const n = calls.length;
      half.click();
      await settle(); await settle();
      const out = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
      check('ticking it writes once', out.length === 1, String(out.length));
      if (out.length) {
        const body = JSON.parse(out[0].body);
        check('and it adds, rather than taking it off the one that had it',
              Array.isArray(body.add_audience)
              && body.add_audience[0] === 'ghost', out[0].body);
        check('there is no remove in it', !body.remove_audience, out[0].body);
      }
      check('so the box is full now',
            half.dataset.state === 'all', half.dataset.state);

      // And a full box is where the destructive direction lives: the one
      // state in which ticking reads as *undo this*.
      const m = calls.length;
      half.click();
      await settle(); await settle();
      const back = calls.slice(m).filter(c => c.url.startsWith('/api/decide'));
      check('ticking a full box takes it away', back.length === 1,
            String(back.length));
      if (back.length) {
        const body = JSON.parse(back[0].body);
        check('by removing it', Array.isArray(body.remove_audience)
              && body.remove_audience[0] === 'ghost', back[0].body);
      }
    }
    document.byId.grid.click();   // anywhere outside dismisses the menu
    deselect();
  }

  // --- a file that already says it is not written to --------------------------
  // Sharing a folder where all but two files are already shared is two
  // writes, not eight hundred and sixty-six. The progress counted every one
  // of them, which is how this was noticed: it was doing the work as well as
  // counting it.
  {
    deselect();
    const two = document.querySelectorAll('.cell').slice(0, 2);
    two[0].dataset.audience = 'ghost';
    two[1].dataset.audience = '';
    two.forEach(c => c.children.find(k => k._classes.has('pick')).click());

    const n = calls.length;
    access.click();
    await settle(); await settle();
    const rows = document.byId.menu.querySelectorAll('.opt');
    const names = rows.map(
      o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
    const half = rows[names.indexOf('ghost')];
    if (half) {
      half.click();
      await settle(); await settle();
      const out = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
      check('the write goes out', out.length === 1, String(out.length));
      if (out.length) {
        const sent = JSON.parse(out[0].body);
        check('naming only the file that did not already have it',
              sent.files.length === 1, JSON.stringify(sent.files));
      }

      // And with both carrying it, there is nothing left to do at all.
      const m = calls.length;
      half.click();   // takes it off both
      await settle(); await settle();
      half.click();   // puts it back on both
      await settle(); await settle();
      const again = calls.slice(m).filter(c => c.url.startsWith('/api/decide'));
      const last = again.length ? JSON.parse(again[again.length - 1].body) : null;
      check('a full selection is written once, not twice',
            !last || last.files.length === 2,
            last && JSON.stringify(last.files));

      const k = calls.length;
      half.click();   // off
      await settle(); await settle();
      half.click();   // on again — nothing has changed since
      await settle(); await settle();
      check('and asking for what is already true writes something once',
            calls.slice(k).filter(
              c => c.url.startsWith('/api/decide')).length === 2,
            String(calls.slice(k).filter(
              c => c.url.startsWith('/api/decide')).length));
    }
    document.byId.grid.click();
    deselect();
  }

  // --- an event and the parts of it are one list ----------------------------
  // A sub-event is a part of an event and reads as one: indented under it,
  // visible without opening anything. It used to be a second panel you
  // reached by choosing the event, which hid the very thing it was there to
  // offer and redrew the whole menu to get to it.
  {
    deselect();
    const one = document.querySelectorAll('.cell')[0];
    one.dataset.event = '';
    one.children.find(k => k._classes.has('pick')).click();

    actBtn('event').click();
    await settle(); await settle();
    const menu = document.byId.menu;
    const opts = () => menu.querySelectorAll('.opt');
    const rows = () => opts().map(
      o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
    const row = name => opts().find(
      o => new RegExp('<span>' + name + '</span>').test(o.innerHTML));

    check('every event is listed once', rows().filter(r => r === 'Sicily')
          .length === 1, rows().join(','));
    check('and its parts are listed under it, named short',
          rows().includes('Taormina') && rows().includes('Catania'),
          rows().join(','));
    check('not as combinations',
          !rows().some(r => /Sicily >/.test(r || '')), rows().join(','));
    check('a part is marked as a part of the row above it',
          row('Taormina')._classes.has('sub'), 'not indented under its event');
    check('and the event itself is not', !row('Sicily')._classes.has('sub'));
    // Ticking the event a file already has writes it again rather than
    // clearing it, so taking one off has to be a row of its own.
    check('there is a way to have no event at all',
          rows().includes('No event'), rows().join(','));

    // Choosing the part writes the whole name in one go: both halves are
    // named on the row, so there is nothing left to ask.
    const n = calls.length;
    row('Taormina').click();
    for (let k = 0; k < 8; k++) await settle();
    const wrote = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('choosing a part writes once', wrote.length === 1,
          String(wrote.length));
    if (wrote.length) {
      check('with the whole name, both halves at once',
            JSON.parse(wrote[0].body).event === 'Sicily > Taormina',
            wrote[0].body);
    }
    check('and that closes it', menu.hidden === true);
    check('the cell says the whole name',
          one.dataset.event === 'Sicily > Taormina', one.dataset.event);
    deselect();
  }

  // Ticking at the width the row is asking about: a file saying
  // *Sicily > Taormina* does say *Sicily* when the question is which event it
  // is in. Comparing whole names either way opened the menu on *no* with the
  // answer on the screen behind it.
  {
    deselect();
    const one = document.querySelectorAll('.cell')[0];
    one.dataset.event = 'Sicily > Taormina';
    one.children.find(k => k._classes.has('pick')).click();

    actBtn('event').click();
    await settle(); await settle();
    const menu = document.byId.menu;
    const row = name => menu.querySelectorAll('.opt').find(
      o => new RegExp('<span>' + name + '</span>').test(o.innerHTML));

    check('the event is ticked by a file that named a part of it',
          row('Sicily').dataset.state === 'all',
          String(row('Sicily').dataset.state));
    check('and the part it named is ticked too',
          row('Taormina').dataset.state === 'all',
          String(row('Taormina').dataset.state));
    check('but not a part it did not name',
          row('Catania').dataset.state === 'none',
          String(row('Catania').dataset.state));
    // Taking the part off is pressing the event's own name, which is the
    // name with no part on the end — so a row saying it again would be the
    // same answer twice, in less plain words.
    check('and nothing says it a second time',
          !row('No sub-event'), 'a row repeating what the event name says');
    document.byId.grid.click();
    one.dataset.event = '';
    deselect();
  }

  // Naming the first part of an event: files already in one, being divided
  // up. The box for it sits under that event rather than at the top of the
  // panel, because the one at the top names events and a single box cannot be
  // asked two questions at once.
  {
    deselect();
    const one = document.querySelectorAll('.cell')[0];
    one.dataset.event = 'Cornwall';
    one.children.find(k => k._classes.has('pick')).click();

    actBtn('event').click();
    await settle(); await settle();
    const menu = document.byId.menu;
    const row = name => menu.querySelectorAll('.opt').find(
      o => new RegExp('<span>' + name + '</span>').test(o.innerHTML));

    const boxes = menu.querySelectorAll('.subnew');
    check('the event these files are in offers to divide it up',
          boxes.length === 1, String(boxes.length));
    const ask = boxes.length ? boxes[0].children[0] : null;
    const box = boxes.length ? boxes[0].children[1] : null;
    const save = boxes.length ? boxes[0].children[2] : null;
    // Done once and picked from ever after, so it is a word until it is
    // wanted rather than a box and a button standing open under every event.
    check('as a word rather than a box',
          !!ask && /add a sub-event/i.test(ask.textContent)
          && box.hidden === true && save.hidden === true,
          ask ? String(ask.textContent) : 'nothing there');
    check('and it is the last thing under that event, after its parts',
          boxes.length === 1
          && menu.querySelectorAll('.opt, .subnew').indexOf(boxes[0])
             === menu.querySelectorAll('.opt, .subnew').length - 1,
          'the box is not at the end of the list');
    check('the box at the top still names events',
          /name an event/i.test(document.getElementById('menuq').placeholder),
          document.getElementById('menuq').placeholder);

    const n = calls.length;
    if (ask) {
      ask.click();
      check('pressing it opens a box named after that event',
            ask.hidden === true && box.hidden === false
            && save.hidden === false
            && /sub-event of Cornwall/i.test(box.placeholder),
            String(box.placeholder));
      box.value = 'Beach day';
      save.click();
    }
    for (let k = 0; k < 8; k++) await settle();
    const made = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('typing one writes it', made.length === 1, String(made.length));
    if (made.length) {
      const body = JSON.parse(made[0].body);
      // Half a name: the other half is whatever each file already has, which
      // is a different answer per file and is worked out where the files are.
      check('as a part of whatever event each file is in',
            body.event_leaf === 'Beach day' && body.event_head === undefined,
            made[0].body);
    }
    check('the cell says the whole name',
          one.dataset.event === 'Cornwall > Beach day', one.dataset.event);
    one.dataset.event = '';
    deselect();
  }

  // A selection is not one thing, and the box has to be able to say so at
  // both widths — two files in different parts of the same event agree about
  // the event and disagree about the part.
  {
    deselect();
    const two = document.querySelectorAll('.cell').slice(0, 2);
    two[0].dataset.event = 'Sicily > Taormina';
    two[1].dataset.event = 'Sicily > Catania';
    two.forEach(c => c.children.find(k => k._classes.has('pick')).click());

    actBtn('event').click();
    await settle(); await settle();
    const menu = document.byId.menu;
    const row = name => menu.querySelectorAll('.opt').find(
      o => new RegExp('<span>' + name + '</span>').test(o.innerHTML));

    check('every one of them is in the event, so the event is full',
          row('Sicily').dataset.state === 'all',
          String(row('Sicily').dataset.state));
    check('but only one of them is in this part of it',
          row('Taormina').dataset.state === 'some',
          String(row('Taormina').dataset.state));
    check('and only the other is in that one',
          row('Catania').dataset.state === 'some',
          String(row('Catania').dataset.state));
    // A part of an event nothing here is in says nothing, at either width.
    check('and an event none of them is in is empty',
          row('Cornwall').dataset.state === 'none',
          String(row('Cornwall').dataset.state));
    document.byId.grid.click();
    two.forEach(c => { c.dataset.event = ''; });
    deselect();
  }

  // One in, one out — of the event as well as of the part.
  {
    deselect();
    const two = document.querySelectorAll('.cell').slice(0, 2);
    two[0].dataset.event = 'Sicily > Taormina';
    two[1].dataset.event = 'Cornwall';
    two.forEach(c => c.children.find(k => k._classes.has('pick')).click());

    actBtn('event').click();
    await settle(); await settle();
    const menu = document.byId.menu;
    const row = name => menu.querySelectorAll('.opt').find(
      o => new RegExp('<span>' + name + '</span>').test(o.innerHTML));

    check('an event half of them are in says half',
          row('Sicily').dataset.state === 'some',
          String(row('Sicily').dataset.state));
    check('and so does the other event',
          row('Cornwall').dataset.state === 'some',
          String(row('Cornwall').dataset.state));
    check('and the part only one of them is in',
          row('Taormina').dataset.state === 'some',
          String(row('Taormina').dataset.state));
    check('while a part neither is in stays empty',
          row('Catania').dataset.state === 'none',
          String(row('Catania').dataset.state));
    document.byId.grid.click();
    two.forEach(c => { c.dataset.event = ''; });
    deselect();
  }

  // A file in an event but in no part of it: the event is full, every part
  // of it is empty. Nothing about being in *Sicily* says *Sicily > Taormina*.
  {
    deselect();
    const one = document.querySelectorAll('.cell')[0];
    one.dataset.event = 'Sicily';
    one.children.find(k => k._classes.has('pick')).click();

    actBtn('event').click();
    await settle(); await settle();
    const menu = document.byId.menu;
    const row = name => menu.querySelectorAll('.opt').find(
      o => new RegExp('<span>' + name + '</span>').test(o.innerHTML));

    check('the event it is in is full', row('Sicily').dataset.state === 'all',
          String(row('Sicily').dataset.state));
    check('and no part of that event claims it',
          row('Taormina').dataset.state === 'none'
          && row('Catania').dataset.state === 'none',
          row('Taormina').dataset.state + ',' + row('Catania').dataset.state);

    // They are all in it already, so this row can only mean *and no part of
    // it* — the tri-state rule the rest of the menu follows: a full box is
    // the one place a press reads as *undo this*.
    one.dataset.event = 'Sicily > Taormina';
    document.byId.grid.click();
    actBtn('event').click();
    await settle(); await settle();
    const n = calls.length;
    row('Sicily').click();
    for (let k = 0; k < 8; k++) await settle();
    const wrote = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    if (wrote.length) {
      check('pressing the event they are all in takes the part off',
            JSON.parse(wrote[0].body).event === 'Sicily', wrote[0].body);
    } else {
      check('pressing the event they are all in takes the part off', false,
            'nothing written');
    }
    check('and the file keeps the event', one.dataset.event === 'Sicily',
          one.dataset.event);
    document.byId.grid.click();
    one.dataset.event = '';
    deselect();
  }

  // Choosing an event is half a gesture — the parts of it are in the list
  // directly under it — so the panel stays up and redraws around the answer.
  {
    deselect();
    const one = document.querySelectorAll('.cell')[0];
    one.dataset.event = '';
    one.children.find(k => k._classes.has('pick')).click();

    actBtn('event').click();
    await settle(); await settle();
    const menu = document.byId.menu;
    const row = name => menu.querySelectorAll('.opt').find(
      o => new RegExp('<span>' + name + '</span>').test(o.innerHTML));

    const n = calls.length;
    row('Sicily').click();
    for (let k = 0; k < 8; k++) await settle();
    const wrote = calls.slice(n).filter(c => c.url.startsWith('/api/decide'));
    check('choosing an event writes the half it is', wrote.length === 1
          && JSON.parse(wrote[0].body).event_head === 'Sicily',
          wrote.length ? wrote[0].body : 'nothing written');
    check('and the panel stays up', menu.hidden === false);
    check('now carrying a box for a part of that event',
          menu.querySelectorAll('.subnew').length === 1,
          String(menu.querySelectorAll('.subnew').length));
    check('and showing the event as the answer',
          row('Sicily').dataset.state === 'all',
          String(row('Sicily').dataset.state));
    document.byId.grid.click();
    one.dataset.event = '';
    deselect();
  }

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok ' + 'all browse-script checks passed');
})();
