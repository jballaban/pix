// What the offline page says, on each of the four things that can be wrong.
//
// None of it is in the markup: the page is served identically every time and
// works out which case it is at run time, by asking `/healthz`. The case that
// matters is the one this page got wrong for a whole afternoon — a phone on
// wifi, a DNS answer at fault, and a page confidently reporting "not on the
// network".
//
// Run as: node offline.js <offline.js source> <case>
const fs = require('fs');
const { El, document } = require('./dom.js');

const js = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail ? ' — ' + detail : ''));
}

for (const id of ['offhead', 'offsay', 'offgo']) {
  const e = new El(id === 'offgo' ? 'button' : 'p');
  e.id = id;
  document.byId[id] = e;
  document.appendChild(e);
}
document.byId.offgo.hidden = true;

const cases = {
  // It answered and it is well: whatever failed has stopped failing.
  back: { reply: { ok: true, index: true } },
  // It answered, and said the app and the projection disagree.
  stale: {
    reply: {
      ok: true, index: false,
      says: 'the index is shape v11, and this build reads v10 — it was '
            + 'written by a newer pix, so this one needs updating',
    },
  },
  // No answer, and the device knows it has no network.
  nonetwork: { reject: true, online: false },
  // No answer, but the device is on *a* network — just not this one.
  elsewhere: { reject: true, online: true },
};
const it = cases[scenario];
if (!it) { console.log('FAIL unknown case ' + scenario); process.exit(1); }

const asked = [];
const fetch = (url, opts) => {
  asked.push({ url, opts });
  if (it.reject) return Promise.reject(new TypeError('Failed to fetch'));
  return Promise.resolve({ json: () => Promise.resolve(it.reply) });
};

const navigator = { onLine: it.online !== false };
let reloaded = 0;
const location = { reload: () => { reloaded += 1; } };
const listeners = {};
const window = {
  addEventListener: (t, fn) => ((listeners[t] ||= []).push(fn)),
};

const settle = () => new Promise(r => setImmediate(r));
const head = () => document.byId.offhead.textContent;
const said = () => document.byId.offsay.textContent;

(async () => {
  try {
    new Function('document', 'window', 'navigator', 'fetch', 'location', js)(
      document, window, navigator, fetch, location);
  } catch (e) {
    console.log('FAIL the snippet threw: ' + e.message);
    process.exit(1);
  }
  await settle();
  await settle();

  // However it turns out, it asks rather than assumes — and never from a cache
  // that was filled at home.
  check('it asks the server', asked.length === 1 && asked[0].url === '/healthz',
        JSON.stringify(asked));
  check('and refuses a cached answer',
        asked[0] && asked[0].opts && asked[0].opts.cache === 'no-store');
  check('and always offers a way to retry',
        document.byId.offgo.hidden === false);

  if (scenario === 'back') {
    check('says it is reachable again', /back/i.test(head()), head());
  } else if (scenario === 'stale') {
    // The whole point of asking: a reachable server with something specific to
    // say, which no amount of guessing from a failed fetch would produce.
    check('says the app needs updating', /updating/i.test(head()), head());
    check('and passes on what the server said',
          /shape v11/.test(said()), said());
  } else if (scenario === 'nonetwork') {
    check('says there is no network', /no network/i.test(head()), head());
    check('and not that the library is at fault',
          !/library cannot be reached/i.test(said()), said());
  } else if (scenario === 'elsewhere') {
    // A phone on wifi, away from home. The failure this page used to describe
    // as "not on the network", which was both wrong and unhelpful.
    check('does not claim the device is offline',
          !/no network/i.test(head()), head());
    check('says it is on a network, but not this one',
          /on a network/i.test(said()), said());
    // Two ways back, which is more use than any description of the failure.
    check('and names both ways back',
          /home/i.test(said()) && /VPN/i.test(said()), said());
  }

  // A phone that rejoins a network should not have to be told to try again.
  check('it listens for coming back', (listeners.online || []).length === 1);

  if (failures.length) {
    failures.forEach(f => console.log('FAIL ' + f));
    process.exit(1);
  }
  console.log('ok');
})().catch(e => { console.log('FAIL ' + (e && e.stack)); process.exit(1); });
