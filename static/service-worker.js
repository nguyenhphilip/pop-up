// Cache basic files
self.addEventListener("install", event => {
  event.waitUntil(
    caches.open("whosout-v1").then(cache =>
      cache.addAll(["/", "/index.html", "/manifest.json"])
    )
  );
});

self.addEventListener("fetch", event => {
  event.respondWith(
    caches.match(event.request).then(r => r || fetch(event.request))
  );
});

// Handle push notifications
self.addEventListener("push", event => {
  const data = event.data ? event.data.json() : {};
  const title = data.title || "Who’s Out";
  const options = {
    body: data.body || "",
    icon: "/icon-192.png",
    badge: "/icon-192.png"
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(clients.openWindow("/"));
});