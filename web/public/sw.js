// Service Worker Hotaru. Без внешних библиотек.
//
// Что делает:
//  - /_next/static/*, иконки: cache-first (имена файлов содержат хеш содержимого);
//  - навигации (HTML) по /, /events/*, /stories/*, /articles/*: сеть в приоритете,
//    ответ сохраняется; при обрыве сети или медленной сети (>4 с, если страница уже
//    сохранена) отдаётся сохранённая копия; нет копии и нет сети — страница /offline;
//  - запросы мягкой навигации (RSC), не-GET и чужие домены не перехватываются: без
//    сети Next сам откатывается на полную навигацию, её подхватывает этот worker.
// Мягкая навигация не делает HTML-запрос, поэтому клиент присылает сообщение
// {type: "cache-page", url}, и worker сам скачивает и сохраняет страницу.

const VERSION = "v1";
const STATIC_CACHE = `hotaru-static-${VERSION}`;
const PAGE_CACHE = `hotaru-pages-${VERSION}`;
const OFFLINE_URL = "/offline";
const MAX_PAGES = 40;
const SLOW_NETWORK_MS = 4000;

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const CACHEABLE_PAGE = new RegExp(
  `^/(?:$|events/${UUID}(?:/compare)?$|stories/${UUID}$|articles/${UUID}$)`,
);
const STATIC_ASSET = /^\/(?:_next\/static\/|pwa-icon\/|apple-icon)/;

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(STATIC_CACHE);
      await cache.add(new Request(OFFLINE_URL, { cache: "reload" }));
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set([STATIC_CACHE, PAGE_CACHE]);
      for (const name of await caches.keys()) {
        if (name.startsWith("hotaru-") && !keep.has(name)) {
          await caches.delete(name);
        }
      }
      await self.clients.claim();
    })(),
  );
});

function pageKey(url) {
  return url.origin + url.pathname;
}

async function trimPages(cache) {
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - MAX_PAGES; i++) {
    await cache.delete(keys[i]);
  }
}

async function storePage(cache, key, response) {
  // delete + put переносит запись в конец очереди: вытесняются давно не открытые.
  await cache.delete(key);
  await cache.put(key, response);
  await trimPages(cache);
}

async function offlineResponse() {
  return (await caches.match(OFFLINE_URL)) || Response.error();
}

async function handlePage(event, request, url) {
  const cache = await caches.open(PAGE_CACHE);
  const key = pageKey(url);
  const cached = await cache.match(key, { ignoreVary: true });

  const network = fetch(request).then((response) => {
    if (response.ok && !response.redirected) {
      event.waitUntil(storePage(cache, key, response.clone()));
    }
    return response;
  });
  // Ошибка сети не должна остаться необработанной, если мы уже вернули копию.
  event.waitUntil(network.catch(() => undefined));

  try {
    if (cached) {
      const slow = new Promise((resolve) => setTimeout(() => resolve(cached), SLOW_NETWORK_MS));
      return await Promise.race([network, slow]);
    }
    return await network;
  } catch {
    return cached || (await offlineResponse());
  }
}

async function handleStatic(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) await cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (STATIC_ASSET.test(url.pathname)) {
    event.respondWith(handleStatic(request));
    return;
  }

  if (request.mode === "navigate") {
    if (CACHEABLE_PAGE.test(url.pathname)) {
      event.respondWith(handlePage(event, request, url));
    } else {
      // Остальные навигации (404, /offline, служебные): сеть, а без сети — /offline.
      event.respondWith(fetch(request).catch(() => offlineResponse()));
    }
  }
});

// Самый первый визит: страница и её скрипты/стили загрузились до того, как worker
// взял их под контроль, поэтому в кеше нет _next/static. Клиент присылает список
// уже загруженных ресурсов, чтобы первая сохранённая страница открывалась без сети.
async function cacheAssets(urls) {
  const cache = await caches.open(STATIC_CACHE);
  for (const raw of urls) {
    try {
      const url = new URL(raw, self.location.origin);
      if (url.origin !== self.location.origin || !STATIC_ASSET.test(url.pathname)) continue;
      if (await cache.match(url.href)) continue;
      const response = await fetch(url.href);
      if (response.ok) await cache.put(url.href, response);
    } catch {
      // Ресурс недоступен — пропускаем, он сохранится при следующем запросе.
    }
  }
}

self.addEventListener("message", (event) => {
  const data = event.data;
  if (data && data.type === "cache-assets" && Array.isArray(data.urls)) {
    event.waitUntil(cacheAssets(data.urls.filter((u) => typeof u === "string")));
    return;
  }
  if (!data || data.type !== "cache-page" || typeof data.url !== "string") return;
  let url;
  try {
    url = new URL(data.url, self.location.origin);
  } catch {
    return;
  }
  if (url.origin !== self.location.origin || !CACHEABLE_PAGE.test(url.pathname)) return;

  event.waitUntil(
    (async () => {
      try {
        const response = await fetch(pageKey(url), { credentials: "same-origin" });
        if (response.ok && !response.redirected) {
          const cache = await caches.open(PAGE_CACHE);
          await storePage(cache, pageKey(url), response);
        }
      } catch {
        // Нет сети — сохранять нечего, молча выходим.
      }
    })(),
  );
});
