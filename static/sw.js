'use strict';
// Cache only the generic offline message. No API responses, credentials, or work records.
const OFFLINE_CACHE = 'workdesk-offline-v1';
self.addEventListener('install', event => {
  event.waitUntil(caches.open(OFFLINE_CACHE).then(cache => cache.add('/static/offline.html')).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => { event.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', event => {
  if (event.request.mode === 'navigate' && new URL(event.request.url).origin === self.location.origin) {
    event.respondWith(fetch(event.request).catch(() => caches.match('/static/offline.html')));
  }
});
self.addEventListener('push', event => {
  let message = {};
  try { message = event.data?.json() || {}; } catch (_) {}
  event.waitUntil(self.registration.showNotification('GitHub Workdesk', {
    body: message.body || 'New GitHub activity. Open your inbox.',
    icon: '/static/icon-192.png', badge: '/static/icon-192.png',
    tag: message.tag || 'workdesk', data: {url: '/?view=notifications'}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const client of windows) {
      if (new URL(client.url).origin === self.location.origin) {
        client.postMessage({type: 'OPEN_NOTIFICATIONS'});
        return client.focus();
      }
    }
    return self.clients.openWindow('/?view=notifications');
  })());
});
