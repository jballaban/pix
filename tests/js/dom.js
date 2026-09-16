// A DOM stub wide enough to run the browse page's script and watch what it
// does. Not a browser — enough to catch the failures that keep recurring: a
// runtime error that kills every handler, a menu that cannot be dismissed, a
// request built with the wrong shape.
//
// It honours bubbling and stopPropagation, because without those it reports
// dismissals a browser would never perform.
'use strict';

function camel(s) { return s.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }

class El {
  constructor(tag) {
    this.tagName = (tag || 'div').toUpperCase();
    this.children = [];
    this.parent = null;
    this.dataset = {};
    this.style = {};
    this.attrs = {};
    this.id = '';
    this._classes = new Set();
    this._listeners = {};
    this._text = '';
    this._html = '';
    this.hidden = false;
    this.value = '';
  }
  get classList() {
    const c = this._classes;
    return {
      add: (...n) => n.forEach(x => c.add(x)),
      remove: (...n) => n.forEach(x => c.delete(x)),
      contains: n => c.has(n),
      toggle: (n, on) => (on === undefined ? (c.has(n) ? c.delete(n) : c.add(n))
                                           : (on ? c.add(n) : c.delete(n))),
    };
  }
  set className(v) {
    this._classes = new Set(String(v).split(/\s+/).filter(Boolean));
  }
  get className() { return [...this._classes].join(' '); }
  set innerHTML(v) {
    this._html = v;
    this.children = parseInto(v);
    this.children.forEach(c => { c.parent = this; });
  }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  remove() {
    if (this.parent) {
      this.parent.children = this.parent.children.filter(x => x !== this);
    }
  }
  // The real API, because the page uses it to put fetched cells where they
  // belong. A stub that forced a different call would be a stub the page was
  // written around.
  get parentNode() { return this.parent; }
  insertAdjacentElement(where, other) {
    if (!this.parent) return other;
    const kin = this.parent.children;
    const at = kin.indexOf(this);
    kin.splice(where === 'afterend' ? at + 1 : at, 0, other);
    other.parent = this.parent;
    return other;
  }
  replaceWith(other) {
    if (!this.parent) return;
    const i = this.parent.children.indexOf(this);
    if (i >= 0) { this.parent.children[i] = other; other.parent = this.parent; }
  }
  addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); }
  // Tests set `_rect` to lay elements out; without one an element simply
  // has no position, which is what an undisplayed node reports anyway.
  getBoundingClientRect() {
    const r = this._rect || { left: 0, top: 0, width: 0, height: 0 };
    return { ...r, right: r.left + r.width, bottom: r.top + r.height };
  }
  contains(other) {
    let p = other;
    while (p) { if (p === this) return true; p = p.parent; }
    return false;
  }
  // The real API, because the grid asks whether a click landed on a link
  // before it treats it as a click on the cell. Without it that question was
  // a TypeError, which is not an answer a browser would ever give.
  matches(sel) { return matchesSel(this, sel); }
  closest(sel) {
    let e = this;
    while (e) { if (matchesSel(e, sel)) return e; e = e.parent; }
    return null;
  }
  get nextElementSibling() {
    if (!this.parent) return null;
    const kin = this.parent.children;
    return kin[kin.indexOf(this) + 1] || null;
  }
  scrollIntoView() {}
  focus() {}
  // A form posts by leaving the page, which is the whole point of using one:
  // the browser owns the transfer. The stub records it instead.
  submit() { (document.submitted ||= []).push(this); }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  get clientWidth() { return 900; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = e => {
      for (const c of e.children) {
        if (matchesSel(c, sel)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  click(ev) {
    let stopped = false;
    const e = Object.assign({}, ev || {}, {
      target: this,
      stopPropagation() { stopped = true; },
      preventDefault() {},
    });
    if (this.onclick) this.onclick(e);
    (this._listeners.click || []).forEach(fn => fn(e));
    let p = this.parent;
    while (p && !stopped) {
      // Both kinds, because a browser bubbles to both. The chips put their
      // handler on the button and their cross *inside* it, so a shim that
      // bubbled only to `addEventListener` handlers reported a cross that
      // did nothing — when the thing being tested was what it does.
      if (p.onclick) p.onclick(e);
      if (!stopped) (p._listeners.click || []).forEach(fn => fn(e));
      p = p.parent;
    }
    if (!stopped) (document._listeners.click || []).forEach(fn => fn(e));
  }
}

// One element against one simple selector — id, class, `[data-x]`, `[data-x="v"]`
// or a tag name. Shared by `querySelectorAll`, `matches` and `closest` rather
// than written out in each: three copies of this rule would be three places for
// it to disagree about what a selector means.
//
// `[data-act]` and `[data-act="stack"]` are different questions, and answering
// both with the first was how a lookup for one button kept finding another.
function matchesSel(el, sel) {
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  if (sel.startsWith('.')) return el._classes.has(sel.slice(1));
  if (sel.startsWith('[')) {
    const spec = sel.slice(1, -1).split('=');
    const have = el.dataset[camel(spec[0].replace(/^data-/, ''))];
    if (have === undefined) return false;
    return spec.length === 1
      || have === spec.slice(1).join('=').replace(/^["']|["']$/g, '');
  }
  return el.tagName === sel.toUpperCase();
}

// Elements carrying an id have to become findable, the way a real innerHTML
// assignment makes them — otherwise getElementById returns null and the script
// fails for a reason the browser would never have produced.
function parseInto(html) {
  const out = [];
  let m;
  const idRe = /<([a-z]+)([^>]*? id="([^"]+)"[^>]*)>/g;
  while ((m = idRe.exec(html))) {
    const e = new El(m[1]);
    e.id = m[3];
    // Keep the attributes, so a test can see what the page prefilled.
    const attrRe = /(\w+)="([^"]*)"/g;
    let a;
    while ((a = attrRe.exec(m[2]))) e.attrs[a[1]] = a[2];
    if (e.attrs.value !== undefined) e.value = e.attrs.value;
    document.byId[m[3]] = e;
    out.push(e);
  }
  // Whole elements with their attributes — enough for the cells the page
  // fetches and inserts, which is the only markup it ever parses rather than
  // builds. Deliberately not a parser: it reads what this page writes.
  const tagRe = /<(div|span|a)\s([^>]*class="(?:cell|stack|top-mark)[^"]*"[^>]*)>/g;
  while ((m = tagRe.exec(html))) {
    const e = new El(m[1]);
    const attrRe = /([\w-]+)="([^"]*)"/g;
    let a;
    while ((a = attrRe.exec(m[2]))) {
      if (a[1] === 'class') e.className = a[2];
      else if (a[1].startsWith('data-')) e.dataset[camel(a[1].slice(5))] = a[2];
      else e.attrs[a[1]] = a[2];
    }
    // The controls a cell carries, so the page can wire what it inserts.
    const inner = html.slice(tagRe.lastIndex);
    const stop = inner.indexOf('</div>');
    for (const kid of (stop < 0 ? inner : inner.slice(0, stop))
         .matchAll(/<(button|span|a)\s+class="([^"]*)"/g)) {
      const c = new El(kid[1]);
      c.className = kid[2];
      e.appendChild(c);
    }
    out.push(e);
  }
  const spanRe = /<span class="([^"]*)"[^>]*>([^<]*)<\/span>/g;
  while ((m = spanRe.exec(html))) {
    const e = new El('span');
    e.className = m[1];
    e._text = m[2];
    out.push(e);
  }
  return out;
}

const document = new El('document');
// A real page always has one, and a form has to be in the document before it
// can be posted.
document.body = document;
document._listeners = {};
document.byId = {};
document.getElementById = id => document.byId[id] || null;
document.createElement = t => new El(t);
document.addEventListener = (t, fn) => (document._listeners[t] ||= []).push(fn);

module.exports = { El, document };
