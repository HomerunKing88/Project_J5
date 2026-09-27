// 최소 앱 캐시 (ADR-01: 서비스 워커에는 앱 실행 파일만 캐시한다). 사진·관측·시드 파일은 캐시하지 않는다.
// 버전을 올리면 이전 캐시를 지운다. 네트워크 없이도 아래 파일로 앱이 뜬다.
const CACHE_PREFIX = "j5-app-";
const CACHE = CACHE_PREFIX + "v0.2.4"; // 캐시한 앱 파일이 바뀌면 올린다 (J5-024: 배경 타일)
const APP_FILES = [
  "./",
  "./index.html",
  "./styles.css",
  "./favicon.svg",
  "./app/main.js",
  "./app/basemap.js",
  "./app/db.js",
  "./app/event.js",
  "./app/export.js",
  "./app/extmap.js",
  "./app/hash.js",
  "./app/limits.js",
  "./app/map.js",
  "./app/migrate.js",
  "./app/parcels.js",
  "./app/sha256.js",
  "./app/seed.js",
  "./app/sniff.js",
  "./app/tiles.js",
  "./app/time.js",
  "./app/ui.js",
  "./app/unzip.js",
  "./app/uuid.js",
  "./app/view.js",
  "./app/zip.js",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(APP_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  // 같은 origin 의 다른 앱 캐시는 건드리지 않는다. 이 앱의 이전 버전 캐시만 지운다.
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith(CACHE_PREFIX) && k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

// 배경 지도 타일 (J5-024, ADR-18): 허용 호스트(app/tiles.js 의 TILE_HOSTS 와 같음) 또는 같은 출처의 /tiles/ 경로만 캐시 우선으로 저장한다.
// 앱 캐시와 다른 이름이라 앱 버전을 올려도 지워지지 않는다. 항목 수 상한을 넘으면 오래된 순서 보장 없이 일부를 지운다.
const TILE_CACHE = "j5-tiles-v1";
const TILE_HOSTS = ["api.vworld.kr"];
const TILE_MAX_ENTRIES = 6000;

async function tileResponse(e) {
  const cache = await caches.open(TILE_CACHE);
  const hit = await cache.match(e.request);
  if (hit) return hit;
  const res = await fetch(e.request, { mode: "no-cors" });
  if (res && (res.ok || res.type === "opaque")) {
    await cache.put(e.request, res.clone());
    const keys = await cache.keys();
    if (keys.length > TILE_MAX_ENTRIES) await Promise.all(keys.slice(0, keys.length - TILE_MAX_ENTRIES + 500).map((k) => cache.delete(k)));
  }
  return res;
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  const sameOrigin = url.origin === self.location.origin;
  if ((!sameOrigin && TILE_HOSTS.includes(url.hostname)) || (sameOrigin && url.pathname.includes("/tiles/"))) {
    e.respondWith(tileResponse(e));
    return;
  }
  if (!sameOrigin) return;
  const path = url.pathname.replace(/^.*\//, "./").replace(/^\.\/app\//, "./app/");
  // 앱 파일만 캐시 우선. 그 밖(시드·데이터 등)은 네트워크로 보내고 캐시하지 않는다.
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
