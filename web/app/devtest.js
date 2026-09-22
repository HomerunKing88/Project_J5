// J5-004 기기 시험. 이 페이지는 아무것도 서버로 보내지 않는다. 결과는 화면과 저장 파일에만 남는다.
import { sniffImage, SUPPORTED } from "./sniff.js";
import { sha256Hex, subtleAvailable } from "./hash.js";

const PHOTO_LIMIT = 20_000_000; // 데이터 사전 §3.3 초기값
const $ = (id) => document.getElementById(id);
const results = { page: "j5-devtest", app: "0.1.0", started_at: new Date().toISOString(), env: {}, photos: [], saves: [], user_notes: "" };

function show() {
  $("results").textContent = JSON.stringify(results, null, 2);
}

function fmtBytes(n) {
  return n >= 1e6 ? (n / 1e6).toFixed(2) + " MB" : n >= 1e3 ? (n / 1e3).toFixed(1) + " KB" : n + " B";
}

async function probeEnv() {
  const env = {
    user_agent: navigator.userAgent,
    online: navigator.onLine,
    secure_context: window.isSecureContext,
    crypto_subtle: subtleAvailable(),
    indexeddb: typeof indexedDB !== "undefined",
    indexeddb_roundtrip: null,
    storage_estimate: null,
    storage_persisted: null,
    protocol: location.protocol,
  };
  try {
    if (navigator.storage?.estimate) {
      const e = await navigator.storage.estimate();
      env.storage_estimate = { usage: e.usage ?? null, quota: e.quota ?? null };
    }
    if (navigator.storage?.persisted) env.storage_persisted = await navigator.storage.persisted();
  } catch (err) { env.storage_error = String(err); }
  try {
    env.indexeddb_roundtrip = await idbRoundtrip();
  } catch (err) { env.indexeddb_roundtrip = "error: " + String(err); }
  results.env = env;
  const lines = [
    ["보안 컨텍스트", env.secure_context, env.secure_context ? "ok" : "bad"],
    ["crypto.subtle(sha256)", env.crypto_subtle, env.crypto_subtle ? "ok" : "bad"],
    ["IndexedDB 왕복", env.indexeddb_roundtrip, env.indexeddb_roundtrip === "ok" ? "ok" : "bad"],
    ["저장 용량(사용/한도)", env.storage_estimate ? `${fmtBytes(env.storage_estimate.usage ?? 0)} / ${env.storage_estimate.quota ? fmtBytes(env.storage_estimate.quota) : "?"}` : "미제공", ""],
    ["persisted", env.storage_persisted, ""],
    ["온라인", env.online, ""],
  ];
  $("env").innerHTML = lines.map(([k, v, c]) => `<tr><th>${k}</th><td class="${c}">${String(v)}</td></tr>`).join("");
  if (!env.secure_context) {
    $("env-note").textContent = "http로 열려 crypto.subtle을 쓸 수 없다. 해시는 계산하지 않는다. 실제 앱은 HTTPS 배포를 전제한다(ADR-01).";
  }
  show();
}

function idbRoundtrip() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") return resolve("unavailable");
    const req = indexedDB.open("j5-devtest", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("t");
    req.onerror = () => reject(req.error);
    req.onsuccess = () => {
      const db = req.result;
      const tx = db.transaction("t", "readwrite");
      const st = tx.objectStore("t");
      const payload = { at: Date.now(), bytes: new Uint8Array([1, 2, 3]) };
      st.put(payload, "k");
      const get = st.get("k");
      tx.oncomplete = () => {
        const ok = get.result && get.result.bytes && get.result.bytes.length === 3;
        db.close();
        indexedDB.deleteDatabase("j5-devtest");
        resolve(ok ? "ok" : "mismatch");
      };
      tx.onerror = () => reject(tx.error);
    };
  });
}

async function onPick(input, source) {
  const files = Array.from(input.files || []);
  for (const file of files) {
    const t0 = performance.now();
    const head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
    const format = sniffImage(head);
    const rec = {
      source, name: file.name, reported_type: file.type || null, bytes: file.size,
      last_modified: file.lastModified ? new Date(file.lastModified).toISOString() : null,
      sniffed: format, supported: SUPPORTED.has(format), over_limit: file.size > PHOTO_LIMIT,
      sha256: null, hash_ms: null, read_ms: null,
    };
    if (rec.supported && !rec.over_limit) {
      const t1 = performance.now();
      const buf = await file.arrayBuffer();
      rec.read_ms = Math.round(performance.now() - t1);
      const t2 = performance.now();
      rec.sha256 = await sha256Hex(buf);
      rec.hash_ms = rec.sha256 ? Math.round(performance.now() - t2) : null;
      rec.blob_url = URL.createObjectURL(new Blob([buf], { type: file.type || "application/octet-stream" }));
    }
    rec.total_ms = Math.round(performance.now() - t0);
    results.photos.push(rec);
    renderPhoto(rec);
  }
  show();
  input.value = "";
}

function renderPhoto(rec) {
  const cls = rec.supported && !rec.over_limit ? "ok" : "bad";
  let verdict;
  if (rec.sniffed === "heic") verdict = "미지원 HEIC. 변환 안내 후 중단 (성공으로 표시하지 않음)";
  else if (!rec.supported) verdict = "미지원·판정 불가 형식";
  else if (rec.over_limit) verdict = `20MB 초과 (${fmtBytes(rec.bytes)}). 작은 묶음으로 나눠야 함`;
  else verdict = "지원 형식";
  const div = document.createElement("div");
  div.innerHTML = `<table>
    <tr><th>출처</th><td>${rec.source}</td></tr>
    <tr><th>이름 / 보고된 type</th><td>${rec.name} / ${rec.reported_type ?? "-"}</td></tr>
    <tr><th>크기</th><td>${fmtBytes(rec.bytes)} (${rec.bytes})</td></tr>
    <tr><th>매직바이트 판정</th><td class="${cls}">${rec.sniffed ?? "unknown"} · ${verdict}</td></tr>
    <tr><th>sha256</th><td>${rec.sha256 ?? "(계산 안 함)"}</td></tr>
    <tr><th>읽기/해시 ms</th><td>${rec.read_ms ?? "-"} / ${rec.hash_ms ?? "-"}</td></tr>
  </table>`;
  if (rec.blob_url) {
    const a = document.createElement("a");
    a.href = rec.blob_url;
    a.download = (rec.sha256 ?? "photo") + "." + rec.sniffed;
    a.textContent = "이 사진을 그대로 저장 (이진 저장 시험)";
    a.addEventListener("click", () => logSave("photo_binary", a.download));
    div.append(a);
  }
  $("photos").prepend(div);
}

function logSave(kind, filename) {
  results.saves.push({ kind, filename, at: new Date().toISOString(), confirmed_by_user: false });
  show();
}

function saveResults() {
  results.user_notes = $("notes").value;
  results.finished_at = new Date().toISOString();
  const name = `j5-devtest-${results.finished_at.replace(/[:.]/g, "-")}.json`;
  const blob = new Blob([JSON.stringify(results, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  logSave("results_json", name);
}

async function copyResults() {
  try {
    await navigator.clipboard.writeText(JSON.stringify(results, null, 2));
    $("copy-note").textContent = "복사됨";
  } catch (err) {
    $("copy-note").textContent = "복사 실패: " + err;
  }
}

$("pick-library").addEventListener("change", (e) => onPick(e.target, "library"));
$("pick-camera").addEventListener("change", (e) => onPick(e.target, "camera"));
$("save").addEventListener("click", saveResults);
$("copy").addEventListener("click", copyResults);
$("confirm-save").addEventListener("change", (e) => {
  results.saves.forEach((s) => { s.confirmed_by_user = e.target.checked; });
  show();
});
window.addEventListener("online", () => { results.env.online = true; show(); });
window.addEventListener("offline", () => { results.env.online = false; show(); });
probeEnv();
