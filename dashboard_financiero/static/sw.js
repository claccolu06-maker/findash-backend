const CACHE_NAME = 'findash-v2';
const STATIC_ASSETS = [
    '/',
    '/static/css/style.css',
    '/static/js/main.js',
    '/static/manifest.json'
];

// Instalación: cachear assets estáticos
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(STATIC_ASSETS);
        })
    );
    self.skipWaiting();
});

// Activación: limpiar caches antiguas
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
            );
        })
    );
    return self.clients.claim();
});

// Fetch: cache-first para estáticos, network-first para rutas Flask
self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Solo interceptar peticiones del mismo origen
    if (url.origin !== location.origin) return;

    // Cache-first para assets estáticos
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(event.request).then((cached) => {
                return cached || fetch(event.request).then((response) => {
                    return caches.open(CACHE_NAME).then((cache) => {
                        cache.put(event.request, response.clone());
                        return response;
                    });
                });
            })
        );
        return;
    }

    // Network-first para rutas de Flask con fallback offline
    event.respondWith(
        fetch(event.request).catch(() => {
            return new Response(
                `<!DOCTYPE html>
                <html lang="es">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Sin conexión - FinDash</title>
                    <style>
                        body { font-family: -apple-system, sans-serif; background: #0a0a0b; color: #fff;
                               display: flex; align-items: center; justify-content: center;
                               min-height: 100vh; margin: 0; text-align: center; padding: 2rem; }
                        h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
                        p { color: #71717a; font-size: 0.95rem; }
                        .dot { width: 12px; height: 12px; background: #00E599; border-radius: 50%;
                               display: inline-block; margin-bottom: 1.5rem; }
                    </style>
                </head>
                <body>
                    <div>
                        <div class="dot"></div>
                        <h1>FinDash sin conexión</h1>
                        <p>Comprueba tu conexión a internet e inténtalo de nuevo.</p>
                    </div>
                </body>
                </html>`,
                { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
            );
        })
    );
});