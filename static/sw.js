// NoteHelper Service Worker - minimal, no offline caching
// This exists solely to satisfy PWA installability requirements

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});
