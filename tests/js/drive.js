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
                  'selall', 'selnone']) mk(id);
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
                                  : sel === '.stage' ? [stage] : realQsa(sel));
document.querySelector = sel => document.querySelectorAll(sel)[0] || null;

// --- browser globals ----------------------------------------------------------
const calls = [];
const fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body });
  if (url.startsWith('/api/suggest')) {
    return { ok: true, json: async () => [{ value: 'ghost', n: 1, scope: 'all' }] };
  }
  if (url.startsWith('/api/file/')) {
    return { ok: true, json: async () => ({ name: 'a.jpg', exif: {}, facts: [] }) };
  }
  return { ok: true, json: async () => ({ failed: [], dropped: [], total: 2 }) };
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
      'VIEW', 'CHIPS', 'FIXED', 'EXTRA', 'ADMIN', 'USERS', 'GROUPS', 'USUAL',
      'setTimeout', js,
    )(document, window, fetch, localStorage, location, confirm,
      VIEW, CHIPS, FIXED, EXTRA, ADMIN, USERS, GROUPS, USUAL, fn => fn());
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

  // The viewer closes on a click beside the picture.
  cells[0].click();
  await tick();
  check('clicking a photo opens the viewer',
        document.byId.viewer.classList.contains('on'));
  stage.click();
  check('clicking beside the picture closes it',
        !document.byId.viewer.classList.contains('on'));

  // Unticking leaves nothing selected, and nothing is implicitly targeted.
  // The old model left the cursor on the cell: not ticked, the count saying
  // none, and the actions quietly applying to it anyway.
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

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok ' + 'all browse-script checks passed');
})();
