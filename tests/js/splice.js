// Drives the splice page's script against a stub DOM.
//
// Usage: node splice.js <script.js> <state.json> <scenario>
// Prints the requests the page sent, one JSON line each, then OK.
//
// The stub is deliberately loose — every element answers every property the
// script reaches for — because what is under test is what the gestures *send*,
// not how a browser lays out a timeline.

const fs = require('fs');
const [, , scriptPath, statePath, scenario] = process.argv;
const js = fs.readFileSync(scriptPath, 'utf8');
const SPLICE = JSON.parse(fs.readFileSync(statePath, 'utf8'));

function element(id) {
  const listeners = {};
  const el = {
    id, hidden: false, textContent: '', innerHTML: '', href: '',
    style: {}, dataset: {}, children: [], offsetLeft: 0, offsetWidth: 1000,
    clientWidth: 1000, scrollLeft: 0,
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    querySelectorAll() { return []; },
    setAttribute(k, v) { this[k] = v; },
    remove() {},
    getBoundingClientRect() { return { left: 0, width: 1000 }; },
    fire(type, ev) { (listeners[type] || []).forEach(fn => fn(ev || {})); },
  };
  return el;
}
const els = {};
const document = {
  getElementById(id) { return els[id] || (els[id] = element(id)); },
  createElement(tag) { return element(tag); },
  addEventListener(type, fn) { (document.listeners[type] = document.listeners[type] || []).push(fn); },
  listeners: {},
};
const video = document.getElementById('sv');
Object.assign(video, {
  currentTime: 0, duration: SPLICE.duration, paused: true, seeking: false,
  play() { this.paused = false; return Promise.resolve(); },
  pause() { this.paused = true; },
});

const sent = [];
function fetch(url, opts) {
  if (opts && opts.method === 'POST') {
    const body = JSON.parse(opts.body);
    sent.push({ url, body });
    const made = (body.clips || []).map((_, i) => 'b.mp4~new' + i);
    const reply = url.endsWith('/make') ? { made }
      : url.endsWith('/split') ? { first: body.name, second: 'b.mp4~half' }
      : url.endsWith('/merge') ? { name: body.first } : {};
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(reply)) });
  }
  const answer = url.startsWith('/api/keyframes/') ? { keys: SPLICE.keys || null }
    : SPLICE.clips;
  return Promise.resolve({ ok: true, json: () => Promise.resolve(answer) });
}
const window = { addEventListener() {} };
// Every question the page asks is answered yes.
globalThis.confirm = () => true;
const settle = () => new Promise(r => setImmediate(r));

(async () => {
  new Function('document', 'window', 'fetch', 'SPLICE', 'location',
               'requestAnimationFrame', js)(
    document, window, fetch, SPLICE, { hash: '' }, () => {});
  const click = id => els[id].onclick({ stopPropagation() {} });
  const key = k => (document.listeners.keydown || []).forEach(fn =>
    fn({ key: k, target: { tagName: 'BODY' }, preventDefault() {} }));

  // Pick a clip the way a click does: the last-drawn bar covering `t`.
  const pickAt = t => {
    const found = els.bars.children.filter(c =>
      String(c.className).startsWith('bar') && c.onclick);
    const hit = found.find(c => parseFloat(c.style.left) <= t / SPLICE.duration * 100
      && parseFloat(c.style.left) + parseFloat(c.style.width) >= t / SPLICE.duration * 100);
    hit.onclick({ stopPropagation() {} });
  };
  const newClip = (a, b) => {
    key('n');
    video.currentTime = a; key('i');
    video.currentTime = b; key('o');
  };
  if (scenario === 'new-keys') {
    newClip(10, 20); click('bsave');
  } else if (scenario === 'nothing') {
    click('bsave');
  } else if (scenario === 'split') {
    pickAt(5); video.currentTime = 12; click('bsplit'); click('bsave');
  } else if (scenario === 'join') {
    pickAt(5); click('bjoin'); click('bsave');
  } else if (scenario === 'delete') {
    pickAt(5); click('bdel'); click('bsave');
  } else if (scenario === 'swallow') {
    newClip(10, 20); click('bsave');
  } else if (scenario === 'discard') {
    newClip(10, 20); click('bdiscard');
  } else if (scenario === 'still') {
    video.currentTime = 12.3456;
    key('p'); click('bsave');
  } else if (scenario === 'hide') {
    click('bhide');
  }
  await settle(); await settle();
  sent.forEach(s => console.log(JSON.stringify(s)));
  console.log('OK');
  // The page polls while a clip is being cut; that timer would keep node up.
  process.exit(0);
})().catch(e => { console.log('FAIL ' + e.stack); process.exit(1); });
