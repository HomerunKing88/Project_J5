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

function row(label, value, cls = "") {
  // 파일명 등 외부 값은 textContent 로만 넣는다. HTML 문자열 삽입을 쓰지 않는다.
  const tr = document.createElement("tr");
  const th = document.createElement("th");
  th.textContent = label;
  const td = document.createElement("td");
  td.textContent = value;
  if (cls) td.className = cls;
  tr.append(th, td);
  return tr;
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
  const envTable = $("env");
  envTable.replaceChildren(...lines.map(([k, v, c]) => row(k, String(v), c)));
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
  const table = document.createElement("table");
  table.append(
    row("출처", rec.source),
    row("이름 / 보고된 type", `${rec.name} / ${rec.reported_type ?? "-"}`),
    row("크기", `${fmtBytes(rec.bytes)} (${rec.bytes})`),
    row("매직바이트 판정", `${rec.sniffed ?? "unknown"} · ${verdict}`, cls),
    row("sha256", rec.sha256 ?? "(계산 안 함)"),
    row("읽기/해시 ms", `${rec.read_ms ?? "-"} / ${rec.hash_ms ?? "-"}`),
  );
  div.append(table);
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
  const entry = { id: results.saves.length + 1, kind, filename, at: new Date().toISOString(), confirmed_by_user: false };
  results.saves.push(entry);
  renderSaves();
  show();
  return entry;
}

function renderSaves() {
  // 저장 시도마다 확인 체크박스를 따로 둔다. 하나를 확인해도 다른 시도가 확인된 것으로 기록하지 않는다.
  const list = $("saves");
  list.replaceChildren(...results.saves.map((s) => {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = s.confirmed_by_user;
    box.addEventListener("change", () => { s.confirmed_by_user = box.checked; show(); });
    label.append(box, ` #${s.id} ${s.kind} · ${s.filename} · '파일' 앱에서 열어 확인함`);
    return label;
  }));
}

function saveResults() {
  // 저장 시도를 먼저 기록해 이 파일 자체에 자신의 시도(미확인 상태)가 들어가게 한다.
  // 확인 체크 후 다시 저장하면 그 파일에 이전 시도의 확인 상태가 담긴다. 마지막 파일이 최종 증거다.
  results.user_notes = $("notes").value;
  results.finished_at = new Date().toISOString();
  const name = `j5-devtest-${results.finished_at.replace(/[:.]/g, "-")}.json`;
  logSave("results_json", name);
  const blob = new Blob([JSON.stringify(results, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
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
window.addEventListener("online", () => { results.env.online = true; show(); });
window.addEventListener("offline", () => { results.env.online = false; show(); });
probeEnv();
