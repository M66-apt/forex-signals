const CACHE_NAME = "fx-signal-shell-v2"; // bumped so browsers detect this file changed and drop the old (stale) cache
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

// Network-first for EVERYTHING (app shell + data): always try to fetch the
// latest version first, and only fall back to the cached copy if the
// network request fails (i.e. genuinely offline). This is what makes future
// edits to dashboard.html show up immediately instead of getting stuck
// behind a stale cache — the previous cache-first version of this file was
// the cause of "UI ไม่เปลี่ยน" after updates.
self.addEventListener("fetch", (event) => {
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
