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
  return Promise.resolve({ ok: true, json: () => Promise.resolve(SPLICE.clips) });
}
const window = { addEventListener() {} };
const settle = () => new Promise(r => setImmediate(r));

(async () => {
  new Function('document', 'window', 'fetch', 'SPLICE', 'location',
               'requestAnimationFrame', js)(
    document, window, fetch, SPLICE, { hash: '' }, () => {});
  const click = id => els[id].onclick({ stopPropagation() {} });
  const key = k => (document.listeners.keydown || []).forEach(fn =>
    fn({ key: k, target: { tagName: 'BODY' }, preventDefault() {} }));

  if (scenario === 'split-fresh') {
    video.currentTime = 30;
    click('bsplit');
  } else if (scenario === 'split-inside') {
    video.currentTime = 10;
    key('s');
  } else if (scenario === 'still') {
    video.currentTime = 12.3456;
    key('p');
  } else if (scenario === 'hide') {
    click('bhide');
  }
  await settle(); await settle();
  sent.forEach(s => console.log(JSON.stringify(s)));
  console.log('OK');
})().catch(e => { console.log('FAIL ' + e.stack); process.exit(1); });
