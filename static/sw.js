const CACHE_NAME = 'vaultdl-v2';
const SHELL_ASSETS = [
  '/',
  '/static/index.html',
  '/static/manifest.json'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = event.request.url;

  if (url.includes('/download') || url.includes('/system')) {
    return; // always live — never cache these
  }

  const isShellAsset = SHELL_ASSETS.some((path) => url.endsWith(path));

  if (isShellAsset) {
    // Network-first for the app shell itself: a fresh deploy shows up on
    // the very next load instead of being stuck behind a stale cache
    // until CACHE_NAME happens to change. Falls back to cache if offline.
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // Cache-first for everything else (media files, icons) — these don't
  // change once created, so serving from cache saves bandwidth.
  event.respondWith(
    caches.match(event.request).then((response) => response || fetch(event.request))
  );
});
