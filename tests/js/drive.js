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
  ['event', 'tags', 'date', 'access', 'stack', 'top', 'unstack', 'delete']);
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
    return { ok: true, json: async () => [{ value: 'ghost', n: 1, scope: 'all' }] };
  }
  if (url.startsWith('/api/file/')) {
    return { ok: true, json: async () => ({ name: 'a.jpg', exif: {}, facts: [] }) };
  }
  return { ok: true,
           json: async () => ({ failed: [], dropped: dropping, total: 2,
                                purged: 1, binned }) };
};
const localStorage = { getItem: () => null, setItem: () => {} };
const listeners = {};
// Enough of a viewport to check scroll anchoring: `scrolled` is how far down
// the page is, and the test grid's rectangles are computed from it.
let scrolled = 0;
const window = {
  innerWidth: 1400, scrollY: 0,
  scrollBy: (_x, y) => { scrolled += y; window.scrollY = scrolled; },
  addEventListener: (t, fn) => ((listeners[t] ||= []).push(fn)),
};
const location = { href: '/browse' };
const confirm = () => true;
const VIEW = { event: null, year: null, tag: null, audience: null, kind: null,
               band: null };
const GRID_GROUPS = [['day', 'By day'], ['event', 'By event'],
                     ['none', 'Ungrouped']];
const GROUPING = ['day'];
const CHIPS = [['event', 'Event'], ['audience', 'Access']];
const FIXED = {};
const EXTRA = { audience: [['new', 'New']] };
const ADMIN = true;
const USERS = ['family', 'james'];
const GROUPS = ['family'];
const USUAL = 'family';

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
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL', 'GRID_GROUPS', 'GROUPING',
      'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, GRID_GROUPS, GROUPING, fn => fn());
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

    cells[0].click();          // this one shows
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
    check('it takes two writes, one each way', sent.length === 2,
          String(sent.length));
    if (sent.length === 2) {
      const first = JSON.parse(sent[0].body);
      const second = JSON.parse(sent[1].body);
      check('the old top starts deferring to the new one',
            first.stacked_under === 'f/b.jpg'
            && first.files.length === 1 && first.files[0].name === 'a.jpg',
            sent[0].body);
      check('and the new one stops deferring to anything',
            second.stacked_under === null
            && second.files.length === 1 && second.files[0].name === 'b.jpg',
            sent[1].body);
      check('both halves are one gesture in the log',
            first.batch === second.batch, first.batch + ' vs ' + second.batch);
    }
    cells[0].dataset.behind = '0';
    cells[1].dataset.under = '';
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
    check('and does not offer to remove the level it is adding to',
          !labels.includes('Remove this grouping'), labels.join(','));
    rows[labels.indexOf('By event')].click();
    check('choosing one keeps the outer level and adds inside it',
          /group=day%2Cevent|group=day,event/.test(location.href),
          location.href);
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
      'GRID_GROUPS', 'GROUPING', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, GROUPING, fn => fn());

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
      'GRID_GROUPS', 'GROUPING', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, GROUPING, fn => fn());

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
      'GRID_GROUPS', 'GROUPING', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, GROUPING, fn => fn());

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
      'GRID_GROUPS', 'GROUPING', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, GROUPING, fn => fn());

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

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok ' + 'all browse-script checks passed');
})();
