// 최소 앱 캐시 (ADR-01: 서비스 워커에는 앱 실행 파일만 캐시한다). 사진·관측·시드 파일은 캐시하지 않는다.
// 버전을 올리면 이전 캐시를 지운다. 네트워크 없이도 아래 파일로 앱이 뜬다.
const CACHE_PREFIX = "j5-app-";
const CACHE = CACHE_PREFIX + "v0.2.43"; // 캐시한 앱 파일이 바뀌면 올린다 (J5-084: 대상에 조회 결과 저장)
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
  "./app/vworld.js",
  "./app/zip.js",
];

self.addEventListener("install", (e) => {
  // 새 판의 파일은 브라우저의 HTTP 캐시를 거치지 않고 서버에서 받는다(cache: "reload"). 거치면 이전 판 파일이 새 판 캐시에 섞일 수 있다 (J5-081)
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(APP_FILES.map((u) => new Request(u, { cache: "reload" })))).then(() => self.skipWaiting()));
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

// 배포 주소(ADR-24, Cloudflare Pages)는 ./index.html 을 ./ 로, devtest.html 을 devtest 로 넘긴다. 설치 때 받은 응답이나 네트워크 응답에
// "넘겨받음" 표시가 붙어 있으면 탐색 요청에 그대로 돌려줄 때 브라우저(Safari 포함)가 페이지를 열지 않는다. 표시가 있으면 같은 본문·머리글로 새 응답을 만든다.
function unredirected(res) {
  if (!res.redirected) return res;
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: res.headers });
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  const sameOrigin = url.origin === self.location.origin;
  // 다른 출처는 앱이 만든 타일 요청(주소에 키 자리표가 있음)만 맡는다. 그 밖의 요청(기기 시험 페이지의 VWorld 직접 연결 시험 등, J5-078)은
  // 손대지 않고 브라우저에 맡긴다: no-cors 로 바꿔 보내면 다른 출처 허용 여부를 잴 수 없고, 실제 키가 든 주소가 타일 캐시에 남는다(리뷰 반영 PR #125)
  if ((!sameOrigin && TILE_HOSTS.includes(url.hostname) && e.request.url.includes(KEY_MARK)) || (sameOrigin && url.pathname.includes("/tiles/"))) {
    e.respondWith(tileResponse(e));
    return;
  }
  if (!sameOrigin) return;
  const path = url.pathname.replace(/^.*\//, "./").replace(/^\.\/app\//, "./app/");
  // 앱 파일만 캐시 우선. 그 밖(시드·데이터 등)은 네트워크로 보내고 캐시하지 않는다.
  // 페이지 열기는 주소의 ?뒤(연습용 모드 ?demo=1 등, J5-077)를 빼고 캐시의 앱 첫 화면과 맞춘다. 오프라인에서도 그런 주소로 열린다(리뷰 반영 PR #124)
  const hit = e.request.mode === "navigate" ? caches.match(e.request, { ignoreSearch: true }) : caches.match(e.request);
  e.respondWith(hit.then((h) => h || fetch(e.request)).then(unredirected));
});
