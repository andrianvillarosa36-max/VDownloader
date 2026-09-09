self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {}); // no-op, just needs to exist for installability
const CACHE_NAME = 'vault-downloader-v1';
const ASSETS = ['/', '/static/index.html', '/static/manifest.json'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)));
});

self.addEventListener('fetch', (e) => {
  if (e.request.url.includes('/files/') || e.request.url.includes('/ws/')) {
    return fetch(e.request);
  }
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});

