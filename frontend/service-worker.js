// Service worker mínimo, solo para que el navegador considere la app
// "instalable" (Chrome/Android exige uno con un handler de fetch para
// mostrar el prompt de instalación) y para que funcione un poquito offline
// (si se corta la conexión, sirve la última versión que se guardó en caché
// en vez de mostrar la pantalla de error del navegador).
//
// A propósito NO cachea nada de forma agresiva: el juego cambia seguido
// (nuevos sonidos, imágenes, código) y una caché "cache-first" clásica
// terminaría mostrando versiones viejas después de cada deploy. Por eso
// la estrategia es "network-first": siempre intenta ir a la red primero
// y solo cae a la caché si no hay conexión.

const CACHE_NAME = 'estratega-shell-v1';
const SHELL_URLS = ['/', '/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((nombres) =>
      Promise.all(nombres.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const { request } = event;

  // Nunca intervenir websockets ni llamadas que no sean GET.
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.protocol === 'ws:' || url.protocol === 'wss:') return;

  event.respondWith(
    fetch(request)
      .then((respuesta) => {
        // Guarda una copia de lo que sí cargó bien, para el próximo corte.
        const copia = respuesta.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copia)).catch(() => {});
        return respuesta;
      })
      .catch(() =>
        caches.match(request).then((cacheado) => cacheado || caches.match('/'))
      )
  );
});
