/* Static app shell only. Never cache API, SSE, credentials, audio or personal data. */
'use strict';
const CACHE = 'nexus-assistant-shell-v2';
const SHELL = [
  '/dashboard', '/dashboard/dashboard.css', '/dashboard/integrations.css',
  '/dashboard/workbench.css', '/dashboard/repository.css', '/dashboard/modules.css',
  '/dashboard/desktop.css', '/dashboard/shell.css', '/dashboard/settings.css',
  '/dashboard/assistant.css', '/dashboard/gui.js', '/dashboard/dashboard.js',
  '/dashboard/integrations.js', '/dashboard/workbench.js', '/dashboard/studio.js',
  '/dashboard/repository.js', '/dashboard/operations.js', '/dashboard/management.js', '/dashboard/world-settings.js',
  '/dashboard/control-shortcuts.js', '/dashboard/desktop.js', '/dashboard/shell.js',
  '/dashboard/i18n.js', '/dashboard/nav.js', '/dashboard/palette.js', '/dashboard/mission.js',
  '/dashboard/rolehub.js', '/dashboard/routerstudio.js', '/dashboard/computer.js',
  '/dashboard/approvals.js', '/dashboard/workflows.js', '/dashboard/security.js',
  '/dashboard/memory.js', '/dashboard/cost.js', '/dashboard/channels.js',
  '/dashboard/assistant.js', '/dashboard/worlddesk.js', '/dashboard/settings.js',
  '/dashboard/presence.js', '/dashboard/presence.css',
  '/dashboard/presence-192.png', '/dashboard/presence-512.png',
  '/assistant-icon.svg', '/manifest.webmanifest'
];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('nexus-assistant-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const request = event.request, url = new URL(request.url);
  // Credential URLs must never become cache keys. Authenticated results stay online-only.
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.search || url.pathname.startsWith('/api/') || url.pathname.startsWith('/world/')) return;
  if (request.mode === 'navigate' && ['/dashboard','/gui','/panel','/hud','/console','/'].includes(url.pathname)) {
    event.respondWith(fetch(request).catch(() => caches.match('/dashboard')));
    return;
  }
  if (!SHELL.includes(url.pathname)) return;
  event.respondWith(caches.match(request).then(cached => cached || fetch(request).then(response => {
    if (response.ok && response.type === 'basic') {
      const copy = response.clone(); caches.open(CACHE).then(cache => cache.put(request, copy));
    }
    return response;
  })));
});
// Push is a generic nudge, never an approval transport. Ignore payload URLs,
// action buttons and text, and never fetch an API from a push handler.
self.addEventListener('push', event => {
  event.waitUntil(self.registration.showNotification('Nexus', {
    body: 'Open Nexus to review an update. / আপডেট দেখতে Nexus খুলুন।',
    icon: '/dashboard/presence-192.png', badge: '/dashboard/presence-192.png',
    tag: 'nexus-update', renotify: false,
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = new URL('/dashboard#presence', self.location.origin).href;
  event.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(async clients => {
    const client = clients.find(c => {
      const url = new URL(c.url); return url.origin === self.location.origin && url.pathname === '/dashboard';
    });
    if (client) { await client.navigate(target); return client.focus(); }
    return self.clients.openWindow(target);
  }));
});
// No background-sync queue or automatic subscription refresh. Lost sends and
// expired credentials always need a fresh authenticated operator interaction.
