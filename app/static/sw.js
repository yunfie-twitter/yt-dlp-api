const CACHE_NAME = 'yt-dlp-web-v1';
const ASSETS = [
  './',
  './index.html',
  './style.css',
  './app.js',
  './locales/ja.json',
  './locales/en.json',
  'https://cdn.jsdelivr.net/npm/beercss@3.13.1/dist/cdn/beer.min.css',
  'https://cdn.jsdelivr.net/npm/beercss@3.13.1/dist/cdn/beer.min.js',
  'https://cdn.jsdelivr.net/npm/material-dynamic-colors@1.1.2/dist/cdn/material-dynamic-colors.min.js',
  'https://unpkg.com/vue@3/dist/vue.global.js',
  'https://unpkg.com/@vueuse/shared@11.3.0/index.iife.min.js',
  'https://unpkg.com/@vueuse/core@11.3.0/index.iife.min.js',
  'https://unpkg.com/axios@1.7.9/dist/axios.min.js',
  'https://unpkg.com/dayjs@1.11.13/dayjs.min.js',
  'https://unpkg.com/dayjs@1.11.13/plugin/duration.js',
  'https://unpkg.com/dayjs@1.11.13/plugin/relativeTime.js',
  'https://unpkg.com/dayjs@1.11.13/locale/ja.js',
  'https://unpkg.com/dayjs@1.11.13/locale/en.js',
  'https://unpkg.com/dompurify@3.2.2/dist/purify.min.js',
  'https://unpkg.com/reconnecting-websocket@4.4.0/dist/reconnecting-websocket-iife.min.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(ASSETS))
  );
});

self.addEventListener('fetch', (event) => {
  // API requests: Network only (or Network first)
  if (event.request.url.includes('/info') || 
      event.request.url.includes('/download') || 
      event.request.url.includes('/search') ||
      event.request.url.includes('/health')) {
    return;
  }

  // Static assets: Cache First, falling back to network
  event.respondWith(
    caches.match(event.request)
      .then((response) => {
        return response || fetch(event.request);
      })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keyList) => {
      return Promise.all(keyList.map((key) => {
        if (key !== CACHE_NAME) {
          return caches.delete(key);
        }
      }));
    })
  );
});
