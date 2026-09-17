// The home-screen offer, driven on one device at a time.
//
// Every branch here is a guess about somebody's phone, and none of them shows
// up in the markup: whether the bar appears at all, whether it offers a button
// or a sentence, and — the one that matters most — whether saying no is the end
// of it. A prompt that comes back is worse than no prompt, and nothing about
// the served HTML would ever reveal that it does.
//
// Run as: node install.js <install.js source> <case>
const fs = require('fs');
const { El, document } = require('./dom.js');

const js = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail ? ' — ' + detail : ''));
}

// A body to append to, and a way to read what landed in it.
document.body = document;
const strip = () => document.children.find(c => c._classes.has('install'));
const wordsIn = el => (el ? el.children.map(k => k.textContent).join(' | ') : '');
const button = (el, cls) =>
  (el ? el.children.find(k => k._classes.has(cls)) : undefined);

const UA = {
  ios: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605',
  ipad: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605',
  android: 'Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537',
  desktop: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537',
};

const cases = {
  android: { ua: UA.android, prompt: true },
  'android-no-api': { ua: UA.android, prompt: false },
  ios: { ua: UA.ios, prompt: false },
  ipad: { ua: UA.ipad, prompt: false, touch: 5 },
  desktop: { ua: UA.desktop, prompt: false },
  installed: { ua: UA.android, prompt: false, standalone: true },
  'said-no': { ua: UA.android, prompt: true, stored: 'no' },
};
const it = cases[scenario];
if (!it) { console.log('FAIL unknown case ' + scenario); process.exit(1); }

const stored = {};
if (it.stored) stored['pix2.install'] = it.stored;
const localStorage = {
  getItem: k => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
};

const navigator = {
  userAgent: it.ua,
  maxTouchPoints: it.touch || 0,
  standalone: it.standalone === true ? true : undefined,
};

const listeners = {};
const window = {
  addEventListener: (t, fn) => ((listeners[t] ||= []).push(fn)),
  matchMedia: q => ({ matches: !!it.standalone && q.indexOf('standalone') >= 0 }),
};

let prompted = 0;
const promptEvent = {
  preventDefault: () => {},
  prompt: () => { prompted += 1; },
};

try {
  new Function('document', 'window', 'navigator', 'localStorage', js)(
    document, window, navigator, localStorage);
} catch (e) {
  console.log('FAIL the snippet threw: ' + e.message);
  process.exit(1);
}

// Chrome hands the page its prompt a moment after load, not before it.
if (it.prompt) (listeners.beforeinstallprompt || []).forEach(fn => fn(promptEvent));

if (scenario === 'desktop') {
  check('nothing is offered on a desktop', strip() === undefined,
        wordsIn(strip()));
} else if (scenario === 'installed') {
  // The offer would be absurd inside the thing it offers.
  check('nothing is offered inside the installed app', strip() === undefined);
} else if (scenario === 'said-no') {
  check('no means no, on every later page', strip() === undefined);
} else if (scenario === 'ios') {
  const bar = strip();
  check('an iPhone is offered the home screen', bar !== undefined);
  // Add to Home Screen lives in the Share menu and nowhere else, and nobody
  // finds it by guessing.
  check('and told where it lives',
        /Share/.test(wordsIn(bar)) && /Home Screen/.test(wordsIn(bar)),
        wordsIn(bar));
  check('with no button, because iOS gives the page no way to install',
        button(bar, 'go') === undefined);
  check('and a way to end it', button(bar, 'no') !== undefined);
} else if (scenario === 'ipad') {
  // iPadOS has reported itself as a Mac for years; touch points are what tell
  // a tablet from a desktop that happens to have a trackpad.
  check('an iPad is a phone for this purpose', strip() !== undefined);
} else if (scenario === 'android') {
  const bar = strip();
  check('Android is offered the home screen', bar !== undefined);
  const go = button(bar, 'go');
  check('with a real button, since Chrome hands the page its prompt',
        go !== undefined, wordsIn(bar));
  go.click();
  check('which actually prompts', prompted === 1, String(prompted));
  // Whatever they answer at the system level, they have answered.
  check('and does not ask again afterwards', strip() === undefined);
  check('remembered', stored['pix2.install'] === 'no');
} else if (scenario === 'android-no-api') {
  const bar = strip();
  check('a browser that sends no prompt still gets the offer',
        bar !== undefined);
  check('as a sentence about the menu',
        /menu/.test(wordsIn(bar)), wordsIn(bar));
  button(bar, 'no').click();
  check('saying no removes it', strip() === undefined);
  check('and remembers, so it is gone for good',
        stored['pix2.install'] === 'no');
}

if (failures.length) {
  failures.forEach(f => console.log('FAIL ' + f));
  process.exit(1);
}
console.log('ok');
