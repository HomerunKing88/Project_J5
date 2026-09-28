// 최소 앱 캐시 (ADR-01: 서비스 워커에는 앱 실행 파일만 캐시한다). 사진·관측·시드 파일은 캐시하지 않는다.
// 버전을 올리면 이전 캐시를 지운다. 네트워크 없이도 아래 파일로 앱이 뜬다.
const CACHE_PREFIX = "j5-app-";
const CACHE = CACHE_PREFIX + "v0.2.13"; // 캐시한 앱 파일이 바뀌면 올린다 (J5-036: 조건 찾기의 물건 조건, 필지 패널 관심 단계)
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
// 인증키(리뷰 반영): 페이지의 타일 URL 에는 자리표 %7Bkey%7D 만 있다. 여기서 IndexedDB meta 의 tiles_key 를 읽어 네트워크 요청에만 넣고,
// 캐시 키는 자리표가 든 원래 URL 을 써서 키가 캐시·요소 속성에 남지 않게 한다. 저장 실패는 응답 반환을 막지 않는다.
const TILE_CACHE = "j5-tiles-v1";
const TILE_HOSTS = ["api.vworld.kr"];
const TILE_MAX_ENTRIES = 6000;
const KEY_MARK = "%7Bkey%7D";
let tileKey = undefined; // undefined: 아직 안 읽음, null: 없음

function readTileKey() {
  if (tileKey !== undefined) return Promise.resolve(tileKey);
  return new Promise((resolve) => {
    let req;
    try { req = indexedDB.open("j5"); } catch { tileKey = null; return resolve(null); }
    req.onerror = () => { tileKey = null; resolve(null); };
    req.onsuccess = () => {
      const db = req.result;
      try {
        if (!db.objectStoreNames.contains("meta")) { db.close(); tileKey = null; return resolve(null); }
        const get = db.transaction("meta").objectStore("meta").get("tiles_key");
        get.onsuccess = () => { tileKey = get.result || null; db.close(); resolve(tileKey); };
        get.onerror = () => { tileKey = null; db.close(); resolve(null); };
      } catch { tileKey = null; db.close(); resolve(null); }
    };
  });
}

/** 받은 요청 그대로, 또는 자리표를 키로 바꾼 같은 타일 요청 (허용 호스트 안에서만 쓰인다). */
function tileRequest(e, target) {
  return target === e.request.url ? e.request : new Request(target, { mode: "no-cors", credentials: "omit" });
}

async function tileResponse(e) {
  let cache = null;
  try { cache = await caches.open(TILE_CACHE); } catch { cache = null; }
  if (cache) {
    const hit = await cache.match(e.request).catch(() => null);
    if (hit) return hit;
  }
  let target = e.request.url;
  if (target.includes(KEY_MARK)) {
    const key = await readTileKey();
    if (!key) return new Response("", { status: 401, statusText: "tile key missing" });
    target = target.split(KEY_MARK).join(encodeURIComponent(key));
  }
  const res = await fetch(tileRequest(e, target), { mode: "no-cors" });
  if (cache && res && (res.ok || res.type === "opaque")) {
    try {
      await cache.put(e.request, res.clone());
      const keys = await cache.keys();
      if (keys.length > TILE_MAX_ENTRIES) await Promise.all(keys.slice(0, keys.length - TILE_MAX_ENTRIES + 500).map((k) => cache.delete(k)));
    } catch { /* 저장 실패(용량 등)는 응답을 막지 않는다 */ }
  }
  return res;
}

self.addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.type === "tile-key-changed") tileKey = undefined;                      // 다음 요청 때 IndexedDB 에서 다시 읽는다
  if (d.type === "tile-evict" && typeof d.url === "string") {                 // 페이지가 그리지 못한 타일(오류 응답)은 캐시에서 뺀다
    e.waitUntil(caches.open(TILE_CACHE).then((c) => c.delete(d.url)).catch(() => {}));
  }
});

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
