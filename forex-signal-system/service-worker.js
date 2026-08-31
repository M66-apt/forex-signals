const CACHE_NAME = "fx-signal-shell-v1";
const SHELL_FILES = [
  "./dashboard.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Network-first for signals.json / API calls (always want fresh data when online),
// cache-first for the static app shell (so it still opens offline).
self.addEventListener("fetch", (event) => {
  const url = event.request.url;
  const isData = url.includes("signals.json") || url.includes("api.twelvedata.com");

  if (isData) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
