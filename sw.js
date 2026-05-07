// sw.js — Solar Energy Monitor Service Worker
// Minimal service worker required for PWA installability on Android/Chrome
// Caches the shell so the app loads offline

const CACHE = 'solar-monitor-v1';
const SHELL = [
  './',
  './index.html',
  './manifest.json',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Network first for data files (latest.json, daily archives)
  if (e.request.url.includes('/data/')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }
  // Cache first for shell assets
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
