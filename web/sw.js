// 최소 앱 캐시 (ADR-01: 서비스 워커에는 앱 실행 파일만 캐시한다). 사진·관측·시드 파일은 캐시하지 않는다.
// 버전을 올리면 이전 캐시를 지운다. 네트워크 없이도 아래 파일로 앱이 뜬다.
const CACHE = "j5-app-v0.1.0";
const APP_FILES = [
  "./",
  "./index.html",
  "./styles.css",
  "./favicon.svg",
  "./app/main.js",
  "./app/db.js",
  "./app/event.js",
  "./app/hash.js",
  "./app/sha256.js",
  "./app/sniff.js",
  "./app/time.js",
  "./app/uuid.js",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(APP_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  const path = url.pathname.replace(/^.*\//, "./").replace(/^\.\/app\//, "./app/");
  // 앱 파일만 캐시 우선. 그 밖(시드·데이터 등)은 네트워크로 보내고 캐시하지 않는다.
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
