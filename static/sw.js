const CACHE_NAME = 'vaultdl-v2';
const APP_SHELL = ['/', '/static/index.html', '/static/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
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
  const path = new URL(event.request.url).pathname;

  // Only the app shell itself goes through the service worker. Everything
  // else — API calls, /incoming_files, /media_files — is left completely
  // untouched so it always hits the server directly (video content has no
  // business being duplicated into the cache, and doing so previously also
  // meant code updates never reached the browser).
  if (!APP_SHELL.includes(path)) return;

  // Network-first: always try to get the freshest copy so future updates
  // apply immediately. Only fall back to the cached copy if there's no
  // network at all (e.g. truly offline).
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});

