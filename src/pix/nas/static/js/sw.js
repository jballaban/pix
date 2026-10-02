
const SHELL = 'pix2-shell-v3';
const KEEP = ['/offline', '/manifest.webmanifest', '/icon-192.png',
              '/icon-512.png', '/icon-maskable-512.png',
              '/apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(KEEP))
                                .then(() => self.skipWaiting()));
});

// Old shells go on activation, so a worker update cannot leave a previous
// version's files answering for this one.
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(names => Promise.all(names.filter(n => n !== SHELL)
                                    .map(n => caches.delete(n))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  // A page is always fetched fresh: its script is inside it, and a cached page
  // is a cached build. Offline, say so plainly instead of showing a library
  // that may no longer be what is there.
  if (req.mode === 'navigate') {
    // `no-store` because fetching is not the same as fetching *fresh*: a
    // plain fetch reads the HTTP cache, which is where a stale page lives.
    // The server says the same thing in a header; this says it from the side
    // that claims to.
    e.respondWith(fetch(req, { cache: 'no-store' })
                  .catch(() => caches.match('/offline')));
    return;
  }
  // Everything else is either one of the shell files or a photograph, and the
  // browser's own cache already handles photographs perfectly well.
  if (KEEP.indexOf(new URL(req.url).pathname) >= 0) {
    e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
  }
});
