// sw.js — Solar Energy Monitor Service Worker
const CACHE = 'solar-monitor-v4';  // bumped: forces fresh manifest.json fetch
const SHELL = [];  // manifest.json removed — served network-first below

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

  // Always network-first for HTML, data, and manifest
  // (manifest must be fresh so PWA icon changes take effect immediately)
  if (url.pathname.endsWith('.html') ||
      url.pathname.endsWith('/') ||
      url.pathname.includes('/data/') ||
      url.pathname.endsWith('manifest.json')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // Cache-first for CDN libraries and other static assets
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
