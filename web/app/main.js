// 앱 진입점 (R1a). 설정 → 시드 불러오기 → 물건 선택 → 관측 저장 → 목록. 외부 통신 없음.
import { Store } from "./db.js";
import { sniffImage, SUPPORTED } from "./sniff.js";
import { sha256Hex } from "./hash.js";
import { uuid4, isUuid } from "./uuid.js";
import { isoWithOffset, fromDatetimeLocal, toDatetimeLocal, localDate } from "./time.js";
import { buildEvent, validateEvent, lineBytes, PHOTO_TAGS, PHOTO_TAG_LABEL, CHANGE_STATUS_LABEL, PHOTO_LIMIT } from "./event.js";
import { validateSeed } from "./seed.js";

export const APP_VERSION = "0.1.0";
const $ = (id) => document.getElementById(id);
const state = { store: null, assets: [], target: null, photos: [], saving: false };

function text(el, value, cls) {
  el.textContent = value;
  if (cls !== undefined) el.className = cls;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node[k] = v;
  }
  node.append(...children);
  return node;
}

// ---- 설정 ----
async function loadSettings() {
  const [studyId, dataMode, route] = await Promise.all([
    state.store.getMeta("study_id"), state.store.getMeta("data_mode"), state.store.getMeta("route_version_id"),
  ]);
  $("study-id").value = studyId ?? "";
  $("data-mode").value = dataMode ?? "synthetic";
  $("route-version").value = route ?? "";
}

async function saveSettings() {
  const studyId = $("study-id").value.trim();
  const route = $("route-version").value.trim();
  if (!studyId) return text($("settings-note"), "study_id 를 입력해야 한다", "bad");
  if (route && !isUuid(route)) return text($("settings-note"), "경로 버전 ID 는 UUID 형식이어야 한다", "bad");
  await state.store.setMeta("study_id", studyId);
  await state.store.setMeta("data_mode", $("data-mode").value);
  await state.store.setMeta("route_version_id", route || null);
  text($("settings-note"), "저장됨", "ok");
  await refreshStatus();
}

// ---- 시드 ----
async function loadSeedObject(seed, source) {
  // 스키마 전체 규칙으로 검증한다. 시드는 통째로 교체하며 저장된 관측은 건드리지 않는다.
  const errs = validateSeed(seed);
  if (errs.length) return text($("seed-note"), "시드 오류: " + errs.slice(0, 5).join("; ") + (errs.length > 5 ? ` 외 ${errs.length - 5}건` : ""), "bad");
  await state.store.replaceAssets(seed, source);
  text($("seed-note"), `${seed.length}개 물건 불러옴 (${source}). 이전 시드의 물건은 목록에서 제거됨`, "ok");
  await renderAssets();
  await renderRecords();
}

async function loadSyntheticSeed() {
  try {
    const res = await fetch("data/assets.seed.synthetic.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadSeedObject(await res.json(), "bundled_synthetic");
  } catch (e) {
    text($("seed-note"), "가상 시드를 읽지 못함: " + e, "bad");
  }
}

async function loadSeedFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    await loadSeedObject(JSON.parse(await file.text()), "file:" + file.name);
  } catch (e) {
    text($("seed-note"), "시드 파일 해석 실패: " + e, "bad");
  }
  input.value = "";
}

async function renderAssets() {
  state.assets = await state.store.listAssets();
  const list = $("asset-list");
  list.replaceChildren(...state.assets.map((a) => el("li", {},
    el("span", { text: a.label }),
    el("span", { class: "badge", text: a.data_mode }),
    el("span", { class: "muted", text: a.location_point ? ` [${a.location_point[0]}, ${a.location_point[1]}]` : a.address ? ` ${a.address}` : "" }),
    el("button", { text: "관측 기록", onclick: () => startObservation(a) }),
  )));
  if (!state.assets.length) list.append(el("li", { class: "muted", text: "물건이 없다. 시드를 불러온다." }));
  await refreshStatus();
}

// ---- 관측 ----
function startObservation(asset) {
  state.target = asset;
  state.photos = [];
  $("target-label").textContent = asset.label;
  $("change-status").value = "";
  $("observed-at").value = toDatetimeLocal(new Date());
  $("date-only").checked = false;
  $("note").value = "";
  $("photos").value = "";
  $("photo-list").replaceChildren();
  text($("observe-note"), "", "");
  $("sec-observe").hidden = false;
  $("sec-observe").scrollIntoView({ behavior: "smooth" });
}

async function addPhotos(input) {
  const files = Array.from(input.files || []);
  for (const file of files) {
    const head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
    const ext = sniffImage(head);
    const item = { name: file.name, bytes: file.size, ext, tags: new Set(), error: null, blob: null, sha256: null };
    if (ext === "heic") item.error = "HEIC 는 미지원. 사진 앱에서 JPEG 로 변환해 다시 선택 (이 사진은 저장하지 않음)";
    else if (!SUPPORTED.has(ext)) item.error = "JPEG/PNG/WebP 가 아님";
    else if (file.size > PHOTO_LIMIT) item.error = `20MB 초과 (${file.size} 바이트)`;
    if (!item.error) {
      const buf = await file.arrayBuffer();
      item.sha256 = await sha256Hex(buf);
      item.blob = new Blob([buf], { type: file.type || "application/octet-stream" });
    }
    state.photos.push(item);
  }
  renderPhotos();
  input.value = "";
}

function renderPhotos() {
  $("photo-list").replaceChildren(...state.photos.map((p, idx) => {
    const box = el("div", { class: "photo-item" });
    box.append(el("div", { text: `${p.name} · ${p.bytes} 바이트 · ${p.ext ?? "unknown"}` }));
    if (p.error) box.append(el("div", { class: "bad", text: p.error }));
    else {
      box.append(el("div", { class: "muted", text: "sha256 " + p.sha256.slice(0, 16) + "…" }));
      const tags = el("div", { class: "tags" });
      for (const t of PHOTO_TAGS) {
        const box2 = el("input", { type: "checkbox", checked: p.tags.has(t) });
        box2.addEventListener("change", () => { box2.checked ? p.tags.add(t) : p.tags.delete(t); });
        tags.append(el("label", {}, box2, ` ${PHOTO_TAG_LABEL[t]}`));
      }
      box.append(tags);
    }
    box.append(el("button", { text: "제거", onclick: () => { state.photos.splice(idx, 1); renderPhotos(); } }));
    return box;
  }));
}

async function saveObservation() {
  // 빠른 두 번 탭으로 같은 관측이 두 번 저장되지 않게 첫 비동기 작업 전에 잠근다.
  if (state.saving) return;
  state.saving = true;
  $("save-observation").disabled = true;
  try {
    await doSaveObservation();
  } finally {
    state.saving = false;
    $("save-observation").disabled = false;
  }
}

async function doSaveObservation() {
  const note = $("note").value;
  const status = $("change-status").value;
  const dateOnly = $("date-only").checked;
  const problems = [];
  if (!status) problems.push("관측 상태를 선택");
  const observedAt = dateOnly ? ($("observed-at").value || "").slice(0, 10) : fromDatetimeLocal($("observed-at").value);
  if (!observedAt) problems.push("관측 시각이 올바르지 않음");
  const bad = state.photos.filter((p) => p.error);
  if (bad.length) problems.push("오류가 있는 사진을 제거해야 함");
  const good = state.photos.filter((p) => !p.error);
  const uniq = new Map(good.map((p) => [p.sha256, p]));
  if (uniq.size !== good.length) problems.push("같은 사진이 두 번 선택됨");
  if (problems.length) return text($("observe-note"), problems.join(". "), "bad");

  const [routeVersionId, studyId, dataMode] = await Promise.all([
    state.store.getMeta("route_version_id"), state.store.getMeta("study_id"), state.store.getMeta("data_mode"),
  ]);
  if (!studyId) return text($("observe-note"), "설정에서 study_id 를 먼저 저장해야 한다", "bad");
  if (state.target.data_mode !== (dataMode ?? "synthetic")) {
    return text($("observe-note"), `물건의 자료 모드(${state.target.data_mode})와 설정(${dataMode ?? "synthetic"})이 다르다. 설정을 맞춘 뒤 저장`, "bad");
  }
  const ev = buildEvent({
    eventId: uuid4(), assetId: state.target.asset_id, observedAt, precision: dateOnly ? "date" : "datetime",
    deviceCreatedAt: isoWithOffset(), routeVersionId: routeVersionId || null, changeStatus: status, note: note.trim() ? note : null,
    attachments: [...uniq.values()].map((p) => ({ sha256: p.sha256, ext: p.ext, bytes: p.bytes, tags: [...p.tags] })),
  });
  const errs = validateEvent(ev);
  if (errs.length) return text($("observe-note"), "저장 불가: " + errs.join(", "), "bad");

  const line = lineBytes(ev);
  // 저장 시점의 study_id·data_mode 를 기록에 고정한다. 나중에 설정을 바꿔도 이 기록의 맥락은 바뀌지 않는다 (J5-007 은 이 값으로 묶는다).
  const record = {
    event_id: ev.event_id, asset_id: ev.asset_id, event: ev, line, event_hash: await sha256Hex(line),
    study_id: studyId, data_mode: dataMode ?? "synthetic", asset_label: state.target.label,
    saved_at: new Date().toISOString(), status: "saved", exported_in: [],
  };
  const photos = [...uniq.values()].map((p) => ({ sha256: p.sha256, ext: p.ext, bytes: p.bytes, blob: p.blob, created_at: record.saved_at }));
  try {
    await state.store.saveObservation(record, photos);
  } catch (e) {
    // 용량 부족 등. '저장됨' 으로 표시하지 않는다.
    return text($("observe-note"), `저장 실패: ${e?.name || ""} ${e?.message || e}. 기록은 남지 않았다`, "bad");
  }
  text($("observe-note"), "저장됨 (이 기기). PC 반영 여부 미확인", "ok");
  $("sec-observe").hidden = true;
  state.target = null;
  await renderRecords();
}

// ---- 목록·상태 ----
async function renderRecords() {
  const events = await state.store.listEvents();
  const labels = new Map(state.assets.map((a) => [a.asset_id, a.label]));
  $("record-list").replaceChildren(...events.map((r) => el("li", {},
    el("div", {}, el("strong", { text: labels.get(r.asset_id) ?? r.asset_label ?? r.asset_id }),
      el("span", { class: "badge", text: r.status === "saved" ? "저장됨" : "내보냄" }),
      el("span", { class: "badge", text: `${r.data_mode} · ${r.study_id}` }),
      labels.has(r.asset_id) ? el("span") : el("span", { class: "warn", text: " (현재 시드에 없는 물건)" })),
    el("div", { class: "muted", text: `${CHANGE_STATUS_LABEL[r.event.payload.change_status]} · ${r.event.observed_at} · 사진 ${r.event.attachment_refs.length}장` }),
    r.event.payload.note ? el("div", { text: r.event.payload.note }) : el("span"),
    el("div", { class: "muted", text: `event ${r.event_id.slice(0, 8)}… · 해시 ${(r.event_hash || "").slice(0, 12)}…` }),
  )));
  text($("record-count"), `(${events.length}건)`);
  await refreshStatus();
}

async function refreshStatus() {
  const c = await state.store.counts();
  const studyId = await state.store.getMeta("study_id");
  const mode = (await state.store.getMeta("data_mode")) ?? "synthetic";
  text($("status-line"), `앱 ${APP_VERSION} · ${mode} · study ${studyId ?? "(미설정)"} · 물건 ${c.assets} · 관측 ${c.events} · 사진 ${c.photos} · ${navigator.onLine ? "온라인" : "오프라인"}`);
}

function registerSw() {
  // 최소 앱 캐시. 보안 컨텍스트가 아니면 등록되지 않으며 앱은 그대로 동작한다.
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register("./sw.js").catch(() => {});
}

async function main() {
  try {
    state.store = await Store.open();
  } catch (e) {
    const f = $("fatal");
    f.hidden = false;
    f.textContent = "IndexedDB 를 열 수 없다: " + e + ". 이 브라우저에서는 기록을 저장할 수 없다.";
    return;
  }
  await loadSettings();
  await renderAssets();
  await renderRecords();
  $("save-settings").addEventListener("click", saveSettings);
  $("load-synthetic").addEventListener("click", loadSyntheticSeed);
  $("seed-file").addEventListener("change", (e) => loadSeedFile(e.target));
  $("photos").addEventListener("change", (e) => addPhotos(e.target));
  $("save-observation").addEventListener("click", saveObservation);
  $("cancel-observation").addEventListener("click", () => { $("sec-observe").hidden = true; state.target = null; });
  $("date-only").addEventListener("change", (e) => { $("observed-at").type = e.target.checked ? "date" : "datetime-local"; $("observed-at").value = e.target.checked ? localDate() : toDatetimeLocal(); });
  window.addEventListener("online", refreshStatus);
  window.addEventListener("offline", refreshStatus);
  registerSw();
}

main();
