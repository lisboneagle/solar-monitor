// sw.js — Solar Energy Monitor Service Worker
const CACHE = 'solar-monitor-v2';
const SHELL = ['./manifest.json'];

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
  const url = new URL(e.request.url);

  // Always network-first for HTML and data — never serve stale versions
  if (url.pathname.endsWith('.html') ||
      url.pathname.endsWith('/') ||
      url.pathname.includes('/data/')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // Cache-first for static assets (manifest, CDN libraries)
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
