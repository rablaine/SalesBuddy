// Sales Buddy Service Worker
// Satisfies PWA installability and serves an offline page when the server is down.

var CACHE_NAME = 'salesbuddy-offline-v2';
var OFFLINE_URL = '/static/offline.html';

self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.add(new Request(OFFLINE_URL, { cache: 'reload' }));
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  event.waitUntil(
    // Clean up old caches
    caches.keys().then(function(names) {
      return Promise.all(
        names.filter(function(name) { return name !== CACHE_NAME; })
             .map(function(name) { return caches.delete(name); })
      );
    }).then(function() {
      return clients.claim();
    })
  );
});

self.addEventListener('fetch', function(event) {
  // Only intercept navigation requests (page loads, not API calls or assets)
  if (event.request.mode !== 'navigate') return;

  event.respondWith(
    fetch(event.request).catch(function() {
      return caches.match(OFFLINE_URL);
    })
  );
});
