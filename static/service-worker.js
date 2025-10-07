// ----- Caching core files -----
self.addEventListener("install", event => {
  event.waitUntil(
    caches.open("whosout-v1").then(cache =>
      cache.addAll([
        "/",
        "/index.html",
        "/manifest.json",
        "/icon-192.png",
        "/icon-512.png"
      ])
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", event => {
  event.respondWith(
    caches.match(event.request).then(r => r || fetch(event.request))
  );
});

// ----- Handle push notifications -----
self.addEventListener("push", event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    // Defensive: some backends send text
    data = { title: "Who’s Out", body: event.data && event.data.text() };
  }

  const title = data.title || "Who’s Out";
  const options = {
    body: data.body || "",
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    // Avoid notification collapsing on Android
    tag: String(Date.now()),
    renotify: false
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(clients.openWindow("/"));
});
