const CACHE = 'cat-pwa-v1';
const SHELL = [
  '/',
  '/login',
  '/student',
  '/robot',
  '/projector',
  '/static/style.css',
  '/static/common.js',
  '/static/offline.html'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL).catch(() => {}))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  event.respondWith(
    caches.match(event.request, { ignoreSearch: true }).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        if (!response || response.status !== 200 || response.type !== 'basic') return response;
        const clone = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, clone)).catch(() => {});
        return response;
      });
    }).catch(() => caches.match('/static/offline.html'))
  );
});
