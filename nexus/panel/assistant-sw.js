/* Static app shell only. Never cache API, SSE, credentials, audio or personal data. */
'use strict';
const CACHE = 'nexus-assistant-shell-v1';
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
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/world/')) return;
  if (request.mode === 'navigate') {
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
