// 앱 진입점 (R1a). 설정 → 시드 불러오기 → 물건 선택 → 관측 저장 → 목록. 외부 통신 없음.
import { Store } from "./db.js";
import { sniffImage, SUPPORTED } from "./sniff.js";
import { sha256Hex } from "./hash.js";
import { uuid4, isUuid } from "./uuid.js";
import { isoWithOffset, fromDatetimeLocal, toDatetimeLocal, localDate } from "./time.js";
import { buildEvent, validateEvent, lineBytes, PHOTO_TAGS, PHOTO_TAG_LABEL, CHANGE_STATUS_LABEL, PHOTO_LIMIT, VIEWPOINT_MAX } from "./event.js";
import { validateSeed } from "./seed.js";
import { validateParcels, parcelAssets, BASIS_LABEL, parcelTitle, fmtArea } from "./parcels.js";
import { selectRecords, planBatches, buildPackage, hasRemainingBatches, studyIdError } from "./export.js";
// 지도 모듈(map.js)은 선택 기능이라 정적 import 하지 않는다. 로드 실패가 앱 전체(목록·기록·내보내기)를 막지 않도록 initMap 안에서 동적으로 불러온다.

export const APP_VERSION = "0.1.0";
const $ = (id) => document.getElementById(id);
const state = { store: null, assets: [], target: null, photos: [], prevPhotos: [], saving: false, export: null, map: null, parcels: null, parcelsCount: 0, parcelsRec: null, seedLoadedAt: null };

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
  const idErr = studyIdError(studyId);
  if (idErr) return text($("settings-note"), idErr, "bad");
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
  state.seedLoadedAt = (await state.store.getMeta("seed_loaded_at")) ?? null;
  if (state.parcelsRec) parcelsNote(state.parcelsRec);
  const list = $("asset-list");
  list.replaceChildren(...state.assets.map((a) => el("li", {},
    el("span", { text: a.label }),
    el("span", { class: "badge", text: a.data_mode }),
    el("span", { class: "muted", text: a.location_point ? ` [${a.location_point[0]}, ${a.location_point[1]}]` : a.address ? ` ${a.address}` : "" }),
    el("button", { text: "관측 기록", onclick: () => startObservation(a) }),
  )));
  if (!state.assets.length) list.append(el("li", { class: "muted", text: "물건이 없다. 시드를 불러온다." }));
  // 목록을 먼저 채운 뒤 지도를 갱신한다. 지도 실패는 목록에 영향을 주지 않는다.
  mapCall((m) => mapNote(m.setAssets(state.assets)));
  await refreshStatus();
}

// ---- 필지 (J5-013B-1, ADR-13) ----
// 필지 번들은 지도의 보조 층이다. 없거나 실패해도 목록·기록·내보내기는 그대로 동작한다.
async function loadParcelsObject(doc, source) {
  const errs = validateParcels(doc);
  if (errs.length) return text($("parcels-note"), "필지 파일 오류: " + errs.slice(0, 5).join("; ") + (errs.length > 5 ? ` 외 ${errs.length - 5}건` : ""), "bad");
  // 파생본의 필지 파일은 정본의 study_id 를 담는다. 설정과 다르면 다른 정본의 연결이므로 넣지 않는다.
  const studyId = await state.store.getMeta("study_id");
  if (doc.study_id && studyId && doc.study_id !== studyId) {
    return text($("parcels-note"), `필지 파일의 study_id(${doc.study_id})가 설정(${studyId})과 다르다. 같은 정본의 파생본을 넣는다`, "bad");
  }
  await state.store.replaceParcels(doc, source);
  applyParcels(await state.store.getParcels());
}

/** 번들을 넣은 뒤 시드가 바뀌었으면 번들의 정본 연결(asset_ids)은 확인되지 않은 것으로 본다. */
function parcelLinksValid() {
  const rec = state.parcelsRec;
  return !!rec && (rec.seed_loaded_at ?? null) === (state.seedLoadedAt ?? null);
}

function applyParcels(rec) {
  state.parcelsRec = rec ?? null;
  state.parcels = rec?.bundle ?? null;
  state.parcelsCount = state.parcels?.features.length ?? 0;
  closeParcelPanel();
  mapCall((m) => m.setParcels(state.parcels));
  parcelsNote(rec);
  mapCall((m) => mapNote(m.setAssets(state.assets)));
}

function parcelsNote(rec) {
  if (!rec) return text($("parcels-note"), "필지 없음. 지도에는 위치점만 보인다.", "muted");
  const b = rec.bundle, s = b.source;
  const crs = s.crs?.epsg ? `EPSG:${s.crs.epsg}` : (s.crs?.name ?? "?");
  const hasLinks = b.features.some((f) => (f.properties.asset_ids ?? []).length);
  const linkNote = hasLinks ? (parcelLinksValid() ? " · 정본 연결 포함" : " · 정본 연결 포함 (시드가 바뀐 뒤라 표시하지 않음: 같은 파생본의 시드와 함께 다시 불러온다)") : "";
  text($("parcels-note"), `필지 ${b.features.length}개 (${b.data_mode}) · ${s.name} · 도형 기준일 ${s.geometry_version} · ${crs} · 이용허락 ${s.license ?? "미확인"}` +
    (b.source_dataset_version != null ? ` · 정본 v${b.source_dataset_version}` : "") + ` · ${rec.source}` +
    (b.warnings?.length ? ` · 경고 ${b.warnings.length}건 (변환 로그 참조)` : "") + linkNote, b.data_mode === "synthetic" ? "muted" : "ok");
}

async function loadSyntheticParcels() {
  try {
    const res = await fetch("data/parcels.synthetic.j5parcels.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadParcelsObject(await res.json(), "bundled_synthetic");
  } catch (e) {
    text($("parcels-note"), "가상 필지를 읽지 못함: " + e, "bad");
  }
}

async function loadParcelsFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    await loadParcelsObject(JSON.parse(await file.text()), "file:" + file.name);
  } catch (e) {
    text($("parcels-note"), "필지 파일 해석 실패: " + e, "bad");
  }
  input.value = "";
}

async function clearParcels() {
  await state.store.clearParcels();
  applyParcels(null);
}

function showParcel(feature) {
  const p = feature.properties;
  mapCall((m) => m.selectParcel(feature.id));
  $("parcel-title").textContent = parcelTitle(p);
  $("parcel-mode").textContent = state.parcels?.data_mode === "synthetic" ? "가상 필지" : "연속지적도";
  $("parcel-pnu").textContent = feature.id;
  const src = state.parcels?.source;
  $("parcel-meta").textContent = `도형면적 ${fmtArea(p.area_m2_geom, p.area_missing_reason)} (공부면적 아님)` + (p.jimok ? ` · 지목 ${p.jimok}` : "") + (p.jibun_mismatch ? " · 원본 지번과 PNU 불일치" : "") +
    (src ? ` · ${src.name} ${src.geometry_version}` : "");
  const linksValid = parcelLinksValid();
  const inside = parcelAssets(feature, state.assets, { linksValid });
  $("parcel-assets").replaceChildren(...inside.map(({ asset: a, basis }) => el("li", {},
    el("span", { text: a.label }), el("span", { class: "badge", text: a.data_mode }), el("span", { class: "badge", text: BASIS_LABEL[basis] }),
    el("button", { text: "관측 기록", onclick: () => startObservation(a) }),
  )));
  if (!linksValid && (p.asset_ids ?? []).length) $("parcel-assets").append(el("li", { class: "warn", text: `정본 연결 ${p.asset_ids.length}건은 시드가 바뀐 뒤 확인되지 않아 표시하지 않는다. 같은 파생본의 시드와 필지 파일을 함께 다시 불러온다.` }));
  if (!inside.length) $("parcel-assets").append(el("li", { class: "muted", text: "이 필지에 연결되거나 위치점이 들어 있는 물건이 없다. PC 에서 시드·연결을 넣은 뒤 다시 불러온다." }));
  $("parcel-panel").hidden = false;
}

function closeParcelPanel() {
  $("parcel-panel").hidden = true;
  mapCall((m) => m.selectParcel(null));
}

// ---- 지도 (J5-005) ----
// 지도는 보조 화면이다. 모듈을 못 읽거나 만들거나 갱신하다 실패하면 안내만 남기고 목록·기록·내보내기는 그대로 동작한다.
async function initMap() {
  try {
    const { createMap } = await import("./map.js");
    state.map = createMap($("map-svg"), { onSelect: startObservation, onSelectParcel: showParcel });
    $("map-zoom-in").addEventListener("click", () => mapCall((m) => m.zoomBy(2)));
    $("map-zoom-out").addEventListener("click", () => mapCall((m) => m.zoomBy(0.5)));
    $("map-fit").addEventListener("click", () => mapCall((m) => m.fit()));
  } catch (e) {
    mapFailed(e);
  }
}

function mapFailed(e) {
  try { state.map?.destroy(); } catch {}
  state.map = null;
  text($("map-note"), `지도 표시 불가: ${e?.message || e}. 목록에서 선택한다`, "warn");
}

function mapCall(fn) {
  if (!state.map) return undefined;
  try { return fn(state.map); } catch (e) { mapFailed(e); return undefined; }
}

function mapNote(c) {
  if (!c) return;
  const parcels = state.parcelsCount ? ` · 필지 ${state.parcelsCount}개` : "";
  if (c.total === 0) return text($("map-note"), parcels ? `물건 없음${parcels}` : "", "muted");
  if (c.located === 0) return text($("map-note"), `위치점 있는 물건이 없다. 목록에서 선택한다${parcels}`, "muted");
  let msg = `위치점 ${c.located}개 표시 (가상 ${c.synthetic} · 실제 ${c.privateReal})`;
  if (c.unlocated > 0) msg += ` · 위치점 없는 물건 ${c.unlocated}개 (목록에서 선택)`;
  text($("map-note"), msg + parcels, "muted");
}

// ---- 관측 ----
function startObservation(asset) {
  state.target = asset;
  state.photos = [];
  state.prevPhotos = [];
  loadPrevPhotos(asset.asset_id);
  mapCall((m) => m.select(asset.asset_id));
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
    const item = { name: file.name, bytes: file.size, ext, tags: new Set(), error: null, blob: null, sha256: null, viewpoint_id: "", heading_deg: "", previous_photo_sha256: "" };
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
      // 반복 촬영(연차 비교)용 선택 입력. 이전 사진은 이 기기에 저장된 같은 물건의 사진 중에서 고른다
      const series = el("div", { class: "series" });
      const vp = el("input", { class: "viewpoint", type: "text", maxlength: String(VIEWPOINT_MAX), placeholder: "예: 전면-남측", value: p.viewpoint_id });
      vp.addEventListener("input", () => { p.viewpoint_id = vp.value.trim(); });
      const hd = el("input", { class: "heading", type: "number", min: "0", max: "359", step: "1", inputmode: "numeric", value: p.heading_deg });
      hd.addEventListener("input", () => { p.heading_deg = hd.value === "" ? "" : Number(hd.value); });
      const prev = el("select", { class: "previous" });
      prev.append(el("option", { value: "", text: state.prevPhotos.length ? "없음" : "없음 (이 기기에 이 물건의 이전 사진 없음)" }));
      for (const q of state.prevPhotos) {
        if (q.sha256 === p.sha256) continue;
        prev.append(el("option", { value: q.sha256, text: q.label, selected: q.sha256 === p.previous_photo_sha256 }));
      }
      prev.addEventListener("change", () => {
        p.previous_photo_sha256 = prev.value;
        const q = state.prevPhotos.find((x) => x.sha256 === prev.value);
        if (q?.viewpoint_id && !p.viewpoint_id) { p.viewpoint_id = q.viewpoint_id; vp.value = q.viewpoint_id; }
      });
      series.append(el("label", { text: "촬영 지점 (선택) " }, vp), el("label", { text: "방향 ° (선택) " }, hd), el("label", { text: "이전 사진 (선택) " }, prev));
      box.append(series);
    }
    box.append(el("button", { text: "제거", onclick: () => { state.photos.splice(idx, 1); renderPhotos(); } }));
    return box;
  }));
}

/** 이 기기에 저장된 같은 물건의 사진 목록 (연차 비교의 '이전 사진' 후보). 오래된 것부터. */
async function loadPrevPhotos(assetId) {
  try {
    const recs = (await state.store.listEvents()).filter((r) => r.asset_id === assetId);
    const out = [];
    for (const r of recs) {
      for (const ref of r.event?.attachment_refs ?? []) {
        const when = (ref.taken_at ?? r.event.observed_at ?? "").slice(0, 10);
        const tags = (ref.tags ?? []).map((k) => PHOTO_TAG_LABEL[k] ?? k).join("·") || "태그 없음";
        out.push({ sha256: ref.sha256, viewpoint_id: ref.viewpoint_id ?? "", when, label: `${when} ${tags}${ref.viewpoint_id ? " · " + ref.viewpoint_id : ""} · ${ref.sha256.slice(0, 8)}` });
      }
    }
    out.sort((a, b) => (a.when < b.when ? -1 : a.when > b.when ? 1 : 0));
    if (state.target?.asset_id === assetId) { state.prevPhotos = out; if (state.photos.length) renderPhotos(); }
  } catch (e) {
    state.prevPhotos = [];
  }
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
    attachments: [...uniq.values()].map((p) => ({ sha256: p.sha256, ext: p.ext, bytes: p.bytes, tags: [...p.tags], viewpoint_id: p.viewpoint_id || "",
                                                  heading_deg: p.heading_deg === "" ? null : p.heading_deg, previous_photo_sha256: p.previous_photo_sha256 || "" })),
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
  mapCall((m) => m.select(null));
  await renderRecords();
}

// ---- 내보내기 ----
// state.export = { studyId, dataMode, batches, errors, k, package, url, attempted }
function exportNote(msg, cls = "") { text($("export-note"), msg, cls); }

async function exportPlan() {
  const [studyId, dataMode] = await Promise.all([state.store.getMeta("study_id"), state.store.getMeta("data_mode")]);
  if (!studyId) { exportNote("설정에서 study_id 를 먼저 저장해야 한다", "bad"); return null; }
  const records = await state.store.listEvents();
  const sel = selectRecords(records, { studyId, dataMode: dataMode ?? "synthetic", includeExported: $("include-exported").checked });
  const meta = await state.store.photoMeta();
  const { batches, errors } = planBatches(sel.selected, meta);
  const photoCount = new Set(batches.flatMap((b) => b.shas)).size;
  const est = batches.reduce((s, b) => s + b.estBytes, 0);
  text($("export-summary"), `대상 ${sel.selected.length}건 · 사진 ${photoCount}장 · 예상 ${fmtBytes(est)} · 묶음 ${batches.length}개` +
    (sel.excludedExported ? ` · 이미 내보낸 ${sel.excludedExported}건 제외` : "") + (sel.otherContext ? ` · 다른 study/모드 ${sel.otherContext}건 제외` : ""));
  $("export-errors").replaceChildren(...errors.map((e) => el("li", { class: "bad", text: `${e.event_id.slice(0, 8)}… 제외: ${e.reason}` })));
  return { studyId, dataMode: dataMode ?? "synthetic", batches, errors, k: 0, package: null, url: null, attempted: false, recordsById: new Map(sel.selected.map((r) => [r.event_id, r])) };
}

function fmtBytes(n) {
  return n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : n >= 1e3 ? (n / 1e3).toFixed(0) + " KB" : n + " B";
}

async function exportPrepare() {
  $("export-prepare").disabled = true;
  try {
    if (state.export?.url) { URL.revokeObjectURL(state.export.url); state.export.url = null; }
    // 기존 계획에 남은 묶음이 있으면 그 계획의 k번째 묶음을 만든다. "묶음 준비" 버튼은 state.export 를 비워 새 계획을 잡는다.
    const plan = hasRemainingBatches(state.export) ? state.export : await exportPlan();
    if (!plan) return;
    if (!plan.batches.length) { state.export = null; $("export-save").disabled = true; $("export-confirm").disabled = true; return exportNote("내보낼 기록이 없다", "muted"); }
    const k = plan.k;
    const batch = plan.batches[k];
    exportNote(`묶음 ${k + 1}/${plan.batches.length} 만드는 중…`, "muted");
    const pkg = await buildPackage(batch, plan.recordsById, {
      loadPhoto: async (sha) => { const p = await state.store.getPhoto(sha); return p ? { blob: p.blob, ext: p.ext } : undefined; },
      studyId: plan.studyId, dataMode: plan.dataMode, k: k + 1, n: plan.batches.length,
    });
    plan.package = pkg; plan.attempted = false;
    state.export = plan;
    $("export-save").textContent = `파일 저장 ${k + 1}/${plan.batches.length}`;
    $("export-save").disabled = false;
    $("export-confirm").disabled = true;
    exportNote(`묶음 ${k + 1}/${plan.batches.length} 준비됨: ${pkg.filename} (${fmtBytes(pkg.bytes)}, 관측 ${pkg.eventIds.length}건, 사진 ${pkg.photoCount}장)`, "ok");
  } catch (e) {
    exportNote("묶음 준비 실패: " + (e?.message || e), "bad");
  } finally {
    $("export-prepare").disabled = false;
  }
}

function exportSave() {
  // 사용자 클릭 안에서 동기적으로 다운로드를 건다 (iOS Safari 는 제스처 밖 다운로드를 막는다).
  const ex = state.export;
  if (!ex?.package) return;
  if (ex.url) URL.revokeObjectURL(ex.url);
  ex.url = URL.createObjectURL(ex.package.blob);
  const a = document.createElement("a");
  a.href = ex.url;
  a.download = ex.package.filename;
  document.body.append(a);
  a.click();
  a.remove();
  ex.attempted = true;
  state.store.appendExport({
    package_id: ex.package.packageId, created_at: ex.package.createdAt, filename: ex.package.filename,
    study_id: ex.studyId, data_mode: ex.dataMode, event_ids: ex.package.eventIds, photo_count: ex.package.photoCount, bytes: ex.package.bytes,
  }).then(renderExportHistory).catch((e) => exportNote("이력 기록 실패: " + e, "bad"));
  $("export-confirm").disabled = false;
  exportNote(`${ex.package.filename} 저장을 요청했다. '파일' 앱 등에서 실제로 저장됐는지 확인한 뒤 "파일 저장 확인"을 누른다. 확인 전에는 내보냄으로 표시하지 않는다.`, "warn");
}

async function exportConfirm() {
  const ex = state.export;
  if (!ex?.package || !ex.attempted) return;
  $("export-confirm").disabled = true;
  try {
    await state.store.markExported(ex.package.eventIds, ex.package.packageId, new Date().toISOString());
  } catch (e) {
    $("export-confirm").disabled = false;
    return exportNote("상태 갱신 실패: " + (e?.message || e), "bad");
  }
  if (ex.url) { URL.revokeObjectURL(ex.url); ex.url = null; }
  ex.k += 1; ex.package = null; ex.attempted = false;
  $("export-save").disabled = true;
  await renderRecords();
  await renderExportHistory();
  if (ex.k < ex.batches.length) {
    exportNote(`묶음 ${ex.k}/${ex.batches.length} 확인됨. 다음 묶음을 준비한다.`, "ok");
    await exportPrepare();
  } else {
    exportNote(`묶음 ${ex.batches.length}개 모두 확인됨 (내보냄). PC 반영 여부는 이 화면에서 알 수 없다.`, "ok");
    state.export = null;
  }
}

async function renderExportHistory() {
  const list = await state.store.listExports();
  $("export-history").replaceChildren(...list.slice().reverse().map((e) => el("li", {},
    el("div", { text: `${e.filename} · 관측 ${e.event_ids.length}건 · 사진 ${e.photo_count}장 · ${fmtBytes(e.bytes)}` }),
    el("div", { class: e.confirmed_at ? "ok" : "warn", text: e.confirmed_at ? `저장 확인 ${e.confirmed_at}` : "저장 미확인 (다시 내보낼 수 있음)" }),
  )));
  if (!list.length) $("export-history").append(el("li", { class: "muted", text: "없음" }));
}

// ---- 목록·상태 ----
async function renderRecords() {
  const events = await state.store.listEvents();
  const labels = new Map(state.assets.map((a) => [a.asset_id, a.label]));
  $("record-list").replaceChildren(...events.map((r) => el("li", {},
    el("div", {}, el("strong", { text: labels.get(r.asset_id) ?? r.asset_label ?? r.asset_id }),
      el("span", { class: "badge", text: r.status === "saved" ? "저장됨" : `내보냄 ${(r.exported_in?.[r.exported_in.length - 1] ?? "").slice(0, 8)}` }),
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
  await initMap();
  try {
    const rec = await state.store.getParcels();
    state.seedLoadedAt = (await state.store.getMeta("seed_loaded_at")) ?? null;
    if (rec) { state.parcelsRec = rec; state.parcels = rec.bundle; state.parcelsCount = rec.bundle.features.length; mapCall((m) => m.setParcels(rec.bundle)); }
    parcelsNote(rec ?? null);
  } catch (e) {
    text($("parcels-note"), "저장된 필지를 읽지 못함: " + (e?.message || e), "bad");
  }
  await renderAssets();
  await renderRecords();
  await renderExportHistory();
  $("save-settings").addEventListener("click", saveSettings);
  $("load-synthetic").addEventListener("click", loadSyntheticSeed);
  $("seed-file").addEventListener("change", (e) => loadSeedFile(e.target));
  $("load-synthetic-parcels").addEventListener("click", loadSyntheticParcels);
  $("parcels-file").addEventListener("change", (e) => loadParcelsFile(e.target));
  $("clear-parcels").addEventListener("click", clearParcels);
  $("parcel-close").addEventListener("click", closeParcelPanel);
  $("photos").addEventListener("change", (e) => addPhotos(e.target));
  $("save-observation").addEventListener("click", saveObservation);
  $("cancel-observation").addEventListener("click", () => { $("sec-observe").hidden = true; state.target = null; mapCall((m) => m.select(null)); });
  $("export-prepare").addEventListener("click", () => { state.export = null; exportPrepare(); });
  $("export-save").addEventListener("click", exportSave);
  $("export-confirm").addEventListener("click", exportConfirm);
  $("include-exported").addEventListener("change", () => { state.export = null; $("export-save").disabled = true; $("export-confirm").disabled = true; exportNote("", ""); });
  $("date-only").addEventListener("change", (e) => { $("observed-at").type = e.target.checked ? "date" : "datetime-local"; $("observed-at").value = e.target.checked ? localDate() : toDatetimeLocal(); });
  window.addEventListener("online", refreshStatus);
  window.addEventListener("offline", refreshStatus);
  registerSw();
}

main();
