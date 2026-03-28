// AGRI-SENTINEL Service Worker — PWA Offline Support v3
const CACHE_NAME = 'agri-sentinel-v3';
const STATIC_ASSETS = [
  '/home',
  '/login',
  '/dashboard',
  '/scan',
  '/history',
  '/market-help',
  '/static/robot_hero.png',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

// Install — cache core shell
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(STATIC_ASSETS).catch(err => {
        console.warn('SW: some assets failed to cache', err);
      });
    })
  );
  self.skipWaiting();
});

// Activate — clean old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch — network-first with cache fallback for pages, cache-first for static assets
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET and API/auth requests
  if (request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;
  if (url.pathname.includes('/docs')) return;

  // Cache-first for static assets (icons, images, fonts)
  if (url.pathname.startsWith('/static/') || 
      url.pathname.match(/\.(png|jpg|jpeg|svg|gif|woff2?|ttf|eot)$/)) {
    event.respondWith(
      caches.match(request).then(cached => {
        if (cached) return cached;
        return fetch(request).then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // Network-first for HTML pages
  event.respondWith(
    fetch(request)
      .then(response => {
        // Cache successful responses
        if (response.ok && response.headers.get('content-type')?.includes('text/html')) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, clone));
        }
        return response;
      })
      .catch(() => {
        // Offline fallback
        return caches.match(request).then(cached => {
          if (cached) return cached;
          // Return offline page for navigation requests
          if (request.mode === 'navigate') {
            return caches.match('/dashboard').then(dashCached => {
              return dashCached || new Response(`
                <!DOCTYPE html>
                <html>
                <head>
                  <meta charset="UTF-8">
                  <meta name="viewport" content="width=device-width, initial-scale=1.0">
                  <title>Offline — AGRI-SENTINEL</title>
                  <style>
                    body { font-family: 'Outfit', system-ui, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: #ecfdf5; color: #064e3b; text-align: center; padding: 20px; }
                    .offline { max-width: 320px; }
                    h1 { font-size: 48px; margin-bottom: 16px; }
                    h2 { font-size: 20px; font-weight: 700; margin-bottom: 8px; }
                    p { color: #64748b; margin-bottom: 24px; }
                    button { background: #059669; color: white; border: none; padding: 14px 28px; border-radius: 12px; font-size: 16px; font-weight: 700; cursor: pointer; }
                  </style>
                </head>
                <body>
                  <div class="offline">
                    <h1>📡</h1>
                    <h2>You're Offline</h2>
                    <p>Please check your internet connection and try again.</p>
                    <button onclick="location.reload()">↻ Retry</button>
                  </div>
                </body>
                </html>
              `, {
                status: 200,
                headers: { 'Content-Type': 'text/html' }
              });
            });
          }
          return new Response('Offline', { status: 503 });
        });
      })
  );
});
