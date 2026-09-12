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
for (const act of ['access', 'tags', 'event', 'date']) {
  const b = new El('button');
  b.dataset.act = act;
  actions.appendChild(b);
}
for (const id of ['menu', 'chips', 'selcount', 'count', 'note', 'viewer',
                  'vimg', 'vvid', 'vmeta', 'rail', 'railtoggle', 'viewclose',
                  'selall', 'selnone',
                  'working', 'workwhat', 'workbar', 'worktally']) mk(id);
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
const fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body });
  if (url.startsWith('/api/suggest')) {
    return { ok: true, json: async () => [{ value: 'ghost', n: 1, scope: 'all' }] };
  }
  if (url.startsWith('/api/file/')) {
    return { ok: true, json: async () => ({ name: 'a.jpg', exif: {}, facts: [] }) };
  }
  return { ok: true,
           json: async () => ({ failed: [], dropped: dropping, total: 2 }) };
};
const localStorage = { getItem: () => null, setItem: () => {} };
const listeners = {};
const window = {
  innerWidth: 1400, scrollY: 0,
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

const tick = () => new Promise(r => setImmediate(r));
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

  // Selecting reveals the action row.
  cells[1].querySelector('.pick').click();
  check('selecting shows the actions', actions.hidden === false);
  check('selection is counted',
        document.byId.selcount.textContent === '1 selected',
        document.byId.selcount.textContent);

  // Opening Access lists both the accounts and whatever is already there.
  const access = actions.children.find(b => b.dataset.act === 'access');
  access.click();
  await tick(); await tick();
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
  await tick(); await tick();
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
  await tick();
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

  // With nothing selected the viewer still gives the actions something to
  // apply to, and paging carries that one along — otherwise S would write to
  // a photograph that had gone off screen.
  document.byId.selnone.click();
  cells[0].click();
  await tick();
  check('opening with nothing selected selects what you opened',
        cells[0].classList.contains('picked'),
        document.byId.selcount.textContent);
  arrow('ArrowRight');
  check('and paging carries that one selection along',
        cells[1].classList.contains('picked')
        && !cells[0].classList.contains('picked'),
        document.byId.selcount.textContent);
  stage.click();

  // Unticking leaves nothing selected, and nothing is implicitly targeted.
  // The old model left the cursor on the cell: not ticked, the count saying
  // none, and the actions quietly applying to it anyway.
  // From a clean selection: looking at a photograph no longer clears one, so
  // what the viewer left ticked would otherwise still be ticked here.
  document.byId.selnone.click();
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
    const acc = actions.children.find(b => b.dataset.act === 'access');
    acc.click();
    await tick(); await tick();
    const rows = menu.querySelectorAll('.opt');
    if (rows.length) rows[0].click();
    await tick(); await tick();
    check('nothing is changed with nothing selected',
          !calls.slice(n).some(c => c.url.startsWith('/api/decide')));
    grid.click();
  }

  // An arrow key both moves and selects.
  document.byId.grid.click();
  arrow('ArrowRight');
  check('arrowing selects what it lands on',
        document.byId.selcount.textContent === '1 selected',
        document.byId.selcount.textContent);

  // The date menu opens on what the files actually say.
  const date = actions.children.find(b => b.dataset.act === 'date');
  date.click();
  await tick();
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
    await tick(); await tick();
    check('clearing the date sends a write',
          calls.slice(n).some(c => c.url.startsWith('/api/decide')));
  }

  // Once something has been changed, the button offers to finish.
  const doneBtn = document.byId.selnone;
  check('the button offers Done after an edit',
        doneBtn.textContent === 'Done', doneBtn.textContent);

  // The heading selects its whole section, and says so with three states.
  document.byId.selnone.click();
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
  await tick();
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
  await tick();
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

  // Arrow keys navigate by where things *are*. Adding a column count to an
  // index went wrong two ways: a heading spans every column and eats a whole
  // row, and the count itself was only right at some window widths — which is
  // why "down" went down and one to the right, but only sometimes.
  {
    const wide = [];
    for (let i = 0; i < 7; i++) wide.push(cell('w' + i + '.jpg', ''));
    const room = mk('room');
    const head = new El('h3');
    head.className = 'group';
    head.dataset.level = '0';
    head._rect = { left: 0, top: 0, width: 480, height: 24 };
    room.appendChild(head);
    // Three per row, 160 wide, 160 tall — the last row deliberately short.
    wide.forEach((c, i) => {
      c._rect = { left: (i % 3) * 160, top: 24 + Math.floor(i / 3) * 160,
                  width: 160, height: 160 };
      room.appendChild(c);
    });
    const all = [...cells, ...wide];
    document.querySelectorAll = sel => (sel === '.cell' ? all
                                      : sel === '.group' ? [heading]
                                      : sel === '.stage' ? [stage] : realQsa(sel));
    // Re-running the script picks up the new grid.
    new Function(
      'document', 'window', 'fetch', 'localStorage', 'location', 'confirm',
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'GRID_GROUPS', 'GROUPING', 'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL,
      GRID_GROUPS, GROUPING, fn => fn());

    wide[0].querySelector('.pick').click();
    arrow('ArrowDown');
    check('down lands directly below, not one to the right',
          wide[3].classList.contains('cur'),
          all.findIndex(c => c.classList.contains('cur')) + '');
    arrow('ArrowUp');
    check('up comes straight back', wide[0].classList.contains('cur'));
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
    const acc = actions.children.find(b => b.dataset.act === 'access');
    acc.click();
    await tick(); await tick();
    const rows = menu.querySelectorAll('.opt');
    const names = rows.map(
      o => (o.innerHTML.match(/<span>([^<]*)<\/span>/) || [])[1]);
    rows[names.indexOf('james')].click();
    await tick(); await tick();
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
    check('the takeover reported progress',
          document.byId.worktally.textContent === '2 of 2 files',
          document.byId.worktally.textContent);
    check('and is down once the write is done',
          !document.byId.working.classList.contains('on'));
  }

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok ' + 'all browse-script checks passed');
})();
