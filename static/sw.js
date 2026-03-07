// NoteHelper Service Worker - minimal, no offline caching
// This exists solely to satisfy PWA installability requirements

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', (event) => {
  // Pass everything through to the network - no caching
  return;
});
