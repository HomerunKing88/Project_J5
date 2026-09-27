// 앱 진입점 (R1a). 화면: 오늘(대상 목록) → 지도 → 기록 → 내보내기 → 설정. 관측은 전체 화면 작업 패널(sheet)에서 한다 (J5-020).
// 저장 형식·이벤트 바이트·해시·ZIP·IndexedDB 계약은 그대로다. 외부 통신 없음.
import { Store } from "./db.js";
import { sniffImage, SUPPORTED } from "./sniff.js";
import { sha256Hex } from "./hash.js";
import { uuid4, isUuid } from "./uuid.js";
import { isoWithOffset, fromDatetimeLocal, toDatetimeLocal, localDate } from "./time.js";
import { buildEvent, validateEvent, lineBytes, PHOTO_TAGS, PHOTO_TAG_LABEL, CHANGE_STATUS_LABEL, PHOTO_LIMIT, VIEWPOINT_MAX } from "./event.js";
import { validateSeed } from "./seed.js";
import { validateParcels, parcelAssets, BASIS_LABEL, parcelTitle, fmtArea, attrLines, attrsSummary, ZONE_LABELS, findParcels, parcelAt, interiorPoint } from "./parcels.js";
import { selectRecords, planBatches, buildPackage, hasRemainingBatches, studyIdError } from "./export.js";
import { migrationReadiness, migrationText, persistenceText } from "./migrate.js";
import { createNavigator, viewFromHash, shortWhen, assetSummary, nextAsset, exportStep, MODE_LABEL, MODE_SHORT } from "./ui.js";
import { externalMapLinks, bboxCenter, LINK_ATTRS } from "./extmap.js";
import { validateBasemap, LAYERS as BASEMAP_LAYERS, LAYER_LABEL as BASEMAP_LAYER_LABEL } from "./basemap.js";
import { PROVIDERS, resolveTileConfig, prefetchPlan, padBbox, tileUrl, TILE_CACHE, PREFETCH_ZOOMS, KEY_PLACEHOLDER } from "./tiles.js";
import { readViewZip, decodeText, UnzipError, VIEW_LIMITS } from "./unzip.js";
import { validateViewManifest, parseRecordsJsonl, validateTransactionsDoc, checkProjectionConsistency, assetHistory, parcelHistory, groupByYear, viewSummary, LINK_LABEL } from "./view.js";
// 지도 모듈(map.js)은 선택 기능이라 정적 import 하지 않는다. 로드 실패가 앱 전체(목록·기록·내보내기)를 막지 않도록 initMap 안에서 동적으로 불러온다.

export const APP_VERSION = "0.2.7";
const $ = (id) => document.getElementById(id);
const state = { store: null, assets: [], events: [], target: null, photos: [], prevPhotos: [], saving: false, export: null, map: null, parcels: null, parcelsCount: 0, parcelsRec: null, zoneColors: false, seedLoadedAt: null, basemapRec: null, basemapCount: 0, view: null, historyReturnFocus: null, historyAsset: null, tiles: null, prefetching: false, lastMapCounts: null,
                nav: null, returnFocus: null, mapSelected: null };

function text(el, value, cls) {
  el.textContent = value;
  if (cls !== undefined) el.className = cls;
}

function modeBadge(mode) {
  return el("span", { class: `badge mode-${mode}`, text: MODE_SHORT[mode] ?? mode });
}

/** 외부 지도 링크 (J5-021, ADR-15): 좌표가 있으면 "다른 지도에서 보기" 링크를 채우고, 없으면 숨긴다. 누르기 전에는 전송 없음. */
function renderExtLinks(container, point) {
  const links = externalMapLinks(point);
  container.hidden = links.length === 0;
  container.replaceChildren(...(links.length ? [el("span", { text: "다른 지도에서 보기:" }),
    ...links.map((l) => el("a", { href: l.href, text: l.name, ...LINK_ATTRS, ...(l.verified ? {} : { title: "링크 형식 미검증. 열리지 않으면 기록해 둔다" }) }))] : []));
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
  renderModeBadge(dataMode ?? "synthetic");
}

// ---- 배경 지도 타일 (J5-024, ADR-18) ----
// 사용자가 켠 제공자에서만 타일을 받는다. 키는 IndexedDB meta 에만 두고 화면에는 가려서 보인다. 실패해도 위치점·필지·도로 배경은 그대로다.
async function loadTileSettings() {
  const [provider, key, template] = await Promise.all([state.store.getMeta("tiles_provider"), state.store.getMeta("tiles_key"), state.store.getMeta("tiles_template")]);
  $("tiles-provider").value = provider ?? "";
  $("tiles-key").value = key ?? "";
  $("tiles-template").value = template ?? "";
  updateTileFields();
  applyTileSettings({ provider: provider ?? "", key: key ?? "", template: template ?? "" }, { quiet: true });
  renderTileCacheStatus();
}

function updateTileFields() {
  const p = $("tiles-provider").value;
  $("tiles-template-field").hidden = p !== "custom";
  $("tiles-key-field").hidden = !(PROVIDERS[p]?.needsKey || (p === "custom" && $("tiles-template").value.includes(KEY_PLACEHOLDER)));
}

function applyTileSettings({ provider, key, template }, { quiet = false } = {}) {
  if (!provider) {
    state.tiles = null;
    mapCall((m) => m.setTiles(null));
    if (!quiet) text($("tiles-note"), "배경 타일을 끄고 저장했습니다.", "ok");
    return true;
  }
  const cfg = resolveTileConfig({ provider, key, template });
  if (cfg.errors) {
    state.tiles = null;
    mapCall((m) => m.setTiles(null));
    text($("tiles-note"), "배경 지도 설정 오류: " + cfg.errors.join("; "), "bad");
    return false;
  }
  state.tiles = cfg;
  mapCall((m) => m.setTiles({ url: cfg.url, minZoom: cfg.minZoom, maxZoom: cfg.maxZoom, attribution: cfg.attribution }));
  if (!quiet) text($("tiles-note"), `저장했습니다: ${PROVIDERS[provider].name} · ${cfg.url}` + (cfg.needsKey ? " · 인증키는 이 기기에만 저장" : ""), "ok");
  return true;
}

async function saveTileSettings() {
  const provider = $("tiles-provider").value, key = $("tiles-key").value.trim(), template = $("tiles-template").value.trim();
  // 먼저 검증만 하고, 키를 저장한 뒤에 타일을 켠다 (서비스 워커가 첫 요청부터 키를 읽을 수 있게)
  if (provider) {
    const cfg = resolveTileConfig({ provider, key, template });
    if (cfg.errors) { state.tiles = null; mapCall((m) => m.setTiles(null)); return text($("tiles-note"), "배경 지도 설정 오류: " + cfg.errors.join("; "), "bad"); }
  }
  await state.store.setMeta("tiles_provider", provider || null);
  await state.store.setMeta("tiles_key", key || null);
  await state.store.setMeta("tiles_template", template || null);
  try { navigator.serviceWorker?.controller?.postMessage({ type: "tile-key-changed" }); } catch {}
  applyTileSettings({ provider, key, template });
  mapNote(state.lastMapCounts);
  renderTileCacheStatus();
}

function onTileStatus(st) {
  // 그리지 못한 타일(오류 응답이 불투명하게 저장됐을 수 있음)은 캐시에서 빼서 다음에 다시 받게 한다 (리뷰 반영)
  if (st.failedUrl) { try { navigator.serviceWorker?.controller?.postMessage({ type: "tile-evict", url: new URL(st.failedUrl, location.href).href }); } catch {} }
  if (st.failed && st.failed % 8 === 1) mapNote(state.lastMapCounts);
}

/** 서비스 워커가 이 페이지를 제어 중인지 (제어 전에는 받은 타일이 저장되지 않는다). 등록이 진행 중이면 잠시 기다린다. */
async function swControlled(timeoutMs = 4000) {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return false;
  if (navigator.serviceWorker.controller) return true;
  try { await Promise.race([navigator.serviceWorker.ready, new Promise((r) => setTimeout(r, timeoutMs))]); } catch { return false; }
  if (navigator.serviceWorker.controller) return true;
  await new Promise((r) => setTimeout(r, 300));
  return !!navigator.serviceWorker.controller;
}

async function tileCacheCount() {
  try {
    if (typeof caches === "undefined") return null;
    const c = await caches.open(TILE_CACHE);
    return (await c.keys()).length;
  } catch { return null; }
}

async function renderTileCacheStatus() {
  const n = await tileCacheCount();
  const sw = "serviceWorker" in navigator && window.isSecureContext;
  text($("tiles-cache-status"), n === null ? "이 브라우저에서는 타일 저장 수를 셀 수 없습니다." : `${n}장 저장됨` + (sw ? "" : " · 저장은 보안 컨텍스트(https 또는 localhost)에서만 됩니다. 지금은 받아도 저장되지 않습니다."));
}

function prefetchBbox() {
  const pts = [];
  for (const a of state.assets) if (Array.isArray(a.location_point) && a.location_point.length === 2) pts.push(a.location_point);
  const bb = state.parcels?.bbox;
  if (bb) pts.push([bb[0], bb[1]], [bb[2], bb[3]]);
  if (!pts.length) return null;
  const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1]);
  return padBbox([Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)], 300);
}

async function prefetchTiles() {
  if (state.prefetching) return;
  if (!state.tiles) return text($("tiles-prefetch-note"), "먼저 배경 지도를 켜고 저장합니다.", "warn");
  if (!navigator.onLine) return text($("tiles-prefetch-note"), "오프라인이라 받을 수 없습니다.", "warn");
  const bbox = prefetchBbox();
  if (!bbox) return text($("tiles-prefetch-note"), "범위를 정할 대상·필지가 없습니다. 물건 목록이나 필지를 먼저 넣습니다.", "warn");
  const plan = prefetchPlan(bbox, { minZoom: Math.max(PREFETCH_ZOOMS.min, state.tiles.minZoom), maxZoom: Math.min(PREFETCH_ZOOMS.max, state.tiles.maxZoom) });
  if (plan.tooMany) return text($("tiles-prefetch-note"), `범위가 넓어 타일이 ${plan.count}장을 넘습니다 (한도 ${plan.cap}). 대상 범위를 좁힙니다.`, "warn");
  if (!(await swControlled())) return text($("tiles-prefetch-note"), "아직 이 기기에 저장할 준비가 되지 않았습니다 (서비스 워커 미활성: https 또는 localhost 로 열고 한 번 새로고침). 받아도 저장되지 않아 시작하지 않습니다.", "warn");
  state.prefetching = true;
  $("tiles-prefetch").disabled = true;
  const before = (await tileCacheCount()) ?? 0;
  let done = 0, failed = 0;
  const load = (t) => new Promise((resolve) => {
    const img = new Image();
    img.onload = () => { done += 1; resolve(); };
    img.onerror = () => { failed += 1; resolve(); };
    img.src = tileUrl(state.tiles.url, t.z, t.x, t.y);
  });
  try {
    const queue = plan.tiles.slice();
    const workers = Array.from({ length: 4 }, async () => { while (queue.length) { await load(queue.shift()); if ((done + failed) % 25 === 0) text($("tiles-prefetch-note"), `받는 중 ${done + failed}/${plan.count}`, "muted"); } });
    await Promise.all(workers);
    const stored = Math.max(0, ((await tileCacheCount()) ?? 0) - before);
    const zr = `${plan.tiles[0]?.z ?? "?"}~${plan.tiles[plan.tiles.length - 1]?.z ?? "?"}`;
    text($("tiles-prefetch-note"), `받기 끝: ${done}장 성공 · ${failed}장 실패 · 새로 저장 ${stored}장 (확대 ${zr} 단계, 범위 여유 300 m)` + (failed ? ". 실패한 타일은 다음에 다시 받습니다" : "") + (done && !stored ? ". 저장된 수가 늘지 않았습니다 (이미 저장됐거나 저장 공간 부족)" : ""), failed ? "warn" : "ok");
  } finally {
    state.prefetching = false;
    $("tiles-prefetch").disabled = false;
    renderTileCacheStatus();
  }
}

async function clearTileCache() {
  try {
    if (typeof caches !== "undefined") await caches.delete(TILE_CACHE);
    text($("tiles-prefetch-note"), "저장된 타일을 지웠습니다. 다음에 볼 때 다시 받습니다.", "muted");
  } catch (e) {
    text($("tiles-prefetch-note"), "지우지 못함: " + (e?.message || e), "bad");
  }
  renderTileCacheStatus();
}

function renderModeBadge(mode) {
  const b = $("mode-badge");
  b.textContent = MODE_LABEL[mode] ?? mode;
  b.className = `mode-badge mode-${mode}`;
}

async function saveSettings() {
  const studyId = $("study-id").value.trim();
  const route = $("route-version").value.trim();
  const idErr = studyIdError(studyId);
  if (idErr) return text($("settings-note"), idErr, "bad");
  if (route && !isUuid(route)) return text($("settings-note"), "조사 경로 ID 는 UUID 형식이어야 한다", "bad");
  await state.store.setMeta("study_id", studyId);
  await state.store.setMeta("data_mode", $("data-mode").value);
  await state.store.setMeta("route_version_id", route || null);
  text($("settings-note"), "저장됨", "ok");
  renderModeBadge($("data-mode").value);
  await renderMigrate(await state.store.listEvents());  // 설정(study_id·모드)이 바뀌면 '현재 설정' 기준이 바뀐다
  await refreshStatus();
}

// ---- 물건 목록 (시드) ----
async function loadSeedObject(seed, source) {
  // 스키마 전체 규칙으로 검증한다. 시드는 통째로 교체하며 저장된 관측은 건드리지 않는다.
  const errs = validateSeed(seed);
  if (errs.length) return text($("seed-note"), "물건 목록 파일 오류: " + errs.slice(0, 5).join("; ") + (errs.length > 5 ? ` 외 ${errs.length - 5}건` : ""), "bad");
  await state.store.replaceAssets(seed, source);
  const from = source === "bundled_synthetic" ? "연습용" : source.replace(/^file:/, "");
  await renderAssets();
  await renderRecords();
  // 안내는 목록·기록을 다 그린 뒤에 쓴다 (이 문구가 보이면 화면이 새 목록이다)
  text($("seed-note"), `물건 ${seed.length}개를 불러왔습니다 (${from}). 이전 목록은 교체됐고 저장된 기록은 그대로입니다.`, "ok");
}

async function loadSyntheticSeed() {
  try {
    const res = await fetch("data/assets.seed.synthetic.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadSeedObject(await res.json(), "bundled_synthetic");
  } catch (e) {
    text($("seed-note"), "연습용 물건 목록을 읽지 못함: " + e, "bad");
  }
}

async function loadSeedFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    await loadSeedObject(JSON.parse(await file.text()), "file:" + file.name);
  } catch (e) {
    text($("seed-note"), "물건 목록 파일을 읽지 못함: " + e, "bad");
  }
  input.value = "";
}

function assetSub(a) {
  return a.address ? a.address : a.location_point ? "지도에 위치 표시됨" : "위치 정보 없음 (목록에서 선택)";
}

async function renderAssets() {
  state.assets = await state.store.listAssets();
  state.events = await state.store.listEvents();
  state.seedLoadedAt = (await state.store.getMeta("seed_loaded_at")) ?? null;
  if (state.parcelsRec) parcelsNote(state.parcelsRec);
  const list = $("asset-list");
  list.replaceChildren(...state.assets.map((a) => {
    const s = assetSummary(a.asset_id, state.events);
    const meta = el("span", { class: "meta" });
    if (s.count) {
      meta.append(el("span", { class: `badge status-${s.lastStatus}`, text: CHANGE_STATUS_LABEL[s.lastStatus] ?? s.lastStatus }),
                  el("span", { text: `마지막 기록 ${shortWhen(s.lastAt)} · 기록 ${s.count}건 · 사진 ${s.photos}장` }));
      if (s.unexported) meta.append(el("span", { class: "warn", text: `안 내보냄 ${s.unexported}건` }));
    } else {
      meta.append(el("span", { text: "이 기기에 기록 없음" }));
    }
    meta.append(el("button", { class: "quiet", text: "이력", onclick: () => openAssetHistory(a) }));
    if (a.origin === "device") {
      meta.append(el("span", { class: "badge src-device", text: "이 기기에서 만듦 · PC 반영 전" }));
      if (!s.count) meta.append(el("button", { class: "quiet", text: "삭제", onclick: () => deleteDeviceAsset(a) }));
    }
    const li = el("li", { class: state.mapSelected === a.asset_id ? "selected" : "" },
      el("span", { class: "title", text: a.label }),
      el("button", { text: "기록하기", onclick: () => startObservation(a) }),
      el("span", { class: "sub" }, modeBadge(a.data_mode), document.createTextNode(" " + assetSub(a))),
      meta,
    );
    li.dataset.assetId = a.asset_id;
    return li;
  }));
  if (!state.assets.length) list.append(el("li", { class: "empty", text: "아직 대상이 없습니다. 설정의 자료 관리에서 물건 목록을 가져오세요." }));
  // 목록을 먼저 채운 뒤 지도를 갱신한다. 지도 실패는 목록에 영향을 주지 않는다.
  mapCall((m) => mapNote(m.setAssets(state.assets)));
  // 지도 선택 카드는 현재 목록의 물건 객체에 다시 묶는다. 목록 교체로 사라진 물건이면 카드를 닫는다 (리뷰 반영: 옛 물건으로 기록되지 않게)
  const selected = state.mapSelected ? state.assets.find((a) => a.asset_id === state.mapSelected) : null;
  if (state.mapSelected && !selected) clearMapSelection();
  else if (selected) selectOnMap(selected, { focus: false });
  renderSeedStatus();
  await refreshStatus();
}

function renderSeedStatus() {
  const n = state.assets.length;
  const when = shortWhen(state.seedLoadedAt);
  text($("seed-status"), n ? `물건 ${n}개 (${MODE_SHORT[state.assets[0].data_mode] ?? ""}) · 마지막 가져오기 ${when ?? "시각 모름"}` : "가져온 물건 목록이 없습니다.");
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
  applyZoneColors();
  applySearchUi();
  parcelsNote(rec);
  mapCall((m) => mapNote(m.setAssets(state.assets)));
}

// ---- 용도지역 색 (J5-025, ADR-19): 필지 속성이 있을 때만 켤 수 있고, 켬 여부는 이 기기에 저장한다 ----
// ---- 필지 찾기 (J5-027): 지번·PNU 검색과 현재 위치. 불러온 필지 안에서만 찾고, 위치는 누를 때 한 번 읽어 그리기만 한다 (저장·전송 없음) ----
function applySearchUi() {
  const has = state.parcelsCount > 0;
  $("parcel-search-form").hidden = !has;
  $("map-locate").disabled = !("geolocation" in navigator);
  $("map-locate").title = "geolocation" in navigator ? "" : "이 브라우저에서는 위치를 읽을 수 없습니다";
  if (!has) { $("parcel-search-results").replaceChildren(); $("parcel-search-results").hidden = true; text($("parcel-search-note"), "", "muted"); }
}

function openParcel(feature, { focus = true, move = true } = {}) {
  if (move) mapCall((m) => m.focusParcel(feature.id));
  showParcel(feature);
  if (focus) $("parcel-history").focus();
}

function searchParcels(q) {
  const feats = state.parcels?.features ?? [];
  const list = $("parcel-search-results");
  list.replaceChildren();
  list.hidden = true;
  if (!feats.length) return text($("parcel-search-note"), "필지 파일을 먼저 넣습니다.", "warn");
  const r = findParcels(feats, q);
  if (!r.query) return text($("parcel-search-note"), "지번(예: 182-13, 산1-2, 동 이름 182-13) 또는 PNU 19자리를 넣습니다.", "warn");
  if (!r.matches.length) return text($("parcel-search-note"), `'${String(q).trim()}' 에 맞는 필지가 불러온 ${feats.length}개 안에 없습니다. 동 이름·지번을 확인하거나 PC 에서 범위를 넓힌 파일을 넣습니다.`, "warn");
  if (r.matches.length === 1) {
    openParcel(r.matches[0]);
    return text($("parcel-search-note"), `${parcelTitle(r.matches[0].properties)} 필지로 이동`, "ok");
  }
  list.replaceChildren(...r.matches.map((f) => el("li", {}, el("span", { class: "title", text: parcelTitle(f.properties) }), el("span", { class: "mono muted", text: f.id }),
    el("button", { text: "열기", onclick: () => { openParcel(f); list.hidden = true; } }))));
  list.hidden = false;
  text($("parcel-search-note"), `${r.total}개 일치${r.total > r.matches.length ? ` (앞 ${r.matches.length}개만 표시)` : ""}. 하나를 고릅니다.`, "muted");
}

function locateMe() {
  if (!("geolocation" in navigator)) return text($("parcel-search-note"), "이 브라우저에서는 위치를 읽을 수 없습니다.", "warn");
  if (!window.isSecureContext) return text($("parcel-search-note"), "위치는 보안 컨텍스트(https 또는 localhost)에서만 읽을 수 있습니다.", "warn");
  const btn = $("map-locate");
  btn.disabled = true;
  text($("parcel-search-note"), "현재 위치를 읽는 중…", "muted");
  navigator.geolocation.getCurrentPosition((pos) => {
    btn.disabled = false;
    const { longitude: lon, latitude: lat, accuracy } = pos.coords;
    // 배경 타일이 켜져 있으면 화면을 옮기지 않는다: 옮기면 현재 위치 주변의 타일을 제공자에게 요청해 위치가 드러난다 (리뷰 반영 PR #73, ADR-20).
    // 점·정확도 원은 지금 보이는 범위 안에서만 그리고, 필지 패널은 화면 이동 없이 연다. 화면을 옮길지는 사용자가 손으로 정한다.
    const move = !state.tiles;
    mapCall((m) => m.setLocation({ lon, lat, accuracy }, { center: move }));
    const acc = Number.isFinite(accuracy) ? ` (정확도 ±${Math.round(accuracy)} m)` : "";
    const tileNote = move ? "" : " 배경 타일이 켜져 있어 화면을 옮기지 않았습니다(옮기면 그 주변 타일을 제공자에게 요청합니다).";
    const f = parcelAt(state.parcels?.features ?? [], [lon, lat]);
    if (f) {
      openParcel(f, { focus: false, move });
      text($("parcel-search-note"), `현재 위치는 ${parcelTitle(f.properties)} 필지 안${acc}. 위치는 저장하지 않습니다.${tileNote}`, "ok");
    } else {
      text($("parcel-search-note"), (state.parcelsCount ? `현재 위치${acc}가 불러온 필지 범위 밖입니다. 위치는 저장하지 않습니다.` : `현재 위치를 표시했습니다${acc}. 필지 파일을 넣으면 그 자리의 필지를 엽니다.`) + tileNote, "muted");
    }
  }, (err) => {
    btn.disabled = false;
    const why = err.code === 1 ? "위치 권한이 거부됐습니다 (브라우저 설정에서 허용)" : err.code === 2 ? "위치를 구할 수 없습니다 (실내·기내 모드)" : "위치 읽기 시간이 지났습니다";
    text($("parcel-search-note"), why + ". 위치는 저장·전송하지 않습니다.", "warn");
  }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
}

function applyZoneColors() {
  const sm = attrsSummary(state.parcels);
  const btn = $("map-zones");
  btn.disabled = !sm.withAttrs;
  btn.setAttribute("aria-pressed", state.zoneColors && sm.withAttrs ? "true" : "false");
  btn.textContent = state.zoneColors && sm.withAttrs ? "용도지역 색 끄기" : "용도지역 색";
  mapCall((m) => m.setZoneColors(state.zoneColors && sm.withAttrs));
  const legend = $("zone-legend");
  if (state.zoneColors && sm.withAttrs) {
    legend.replaceChildren(...[...sm.byZone.entries()].filter(([k]) => k !== "none").sort((a, b) => b[1] - a[1])
      .map(([k, n]) => el("span", {}, el("span", { class: `lg lg-zone zone-${k}` }), `${ZONE_LABELS[k] ?? k} ${n}`)));
    legend.hidden = false;
  } else {
    legend.replaceChildren();
    legend.hidden = true;
  }
}

async function toggleZoneColors() {
  state.zoneColors = !state.zoneColors;
  try { await state.store.setMeta("zone_colors", state.zoneColors); } catch { /* 저장 실패해도 화면은 바꾼다 */ }
  applyZoneColors();
}

function parcelsNote(rec) {
  if (!rec) return text($("parcels-note"), "필지 없음. 지도에는 물건 위치만 보입니다.", "muted");
  const b = rec.bundle, s = b.source;
  const crs = s.crs?.epsg ? `EPSG:${s.crs.epsg}` : (s.crs?.name ?? "?");
  const hasLinks = b.features.some((f) => (f.properties.asset_ids ?? []).length);
  const linkNote = hasLinks ? (parcelLinksValid() ? " · 정본 연결 포함" : " · 정본 연결 포함 (시드가 바뀐 뒤라 표시하지 않음: 같은 파생본의 시드와 함께 다시 불러온다)") : "";
  const when = shortWhen(rec.loaded_at);
  const sm = attrsSummary(b);
  const attrsNote = sm.withAttrs ? ` · 필지 속성 ${sm.withAttrs}개 (${(b.attrs_sources ?? []).map((a) => a.name).join(", ") || "출처 미기재"})` : "";
  text($("parcels-note"), `필지 ${b.features.length}개 (${b.data_mode}) · ${s.name} · 도형 기준일 ${s.geometry_version} · ${crs} · 이용허락 ${s.license ?? "미확인"}` + attrsNote +
    (b.source_dataset_version != null ? ` · 정본 v${b.source_dataset_version}` : " · 정본 버전 모름 (구본 여부 알 수 없음)") + ` · ${rec.source}` + (when ? ` · 가져오기 ${when}` : "") +
    (b.warnings?.length ? ` · 경고 ${b.warnings.length}건 (변환 로그 참조)` : "") + linkNote, b.data_mode === "synthetic" ? "muted" : "ok");
}

async function loadSyntheticParcels() {
  try {
    const res = await fetch("data/parcels.synthetic.j5parcels.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadParcelsObject(await res.json(), "bundled_synthetic");
  } catch (e) {
    text($("parcels-note"), "연습용 필지를 읽지 못함: " + e, "bad");
  }
}

async function loadParcelsFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    await loadParcelsObject(JSON.parse(await file.text()), "file:" + file.name);
  } catch (e) {
    text($("parcels-note"), "필지 파일을 읽지 못함: " + e, "bad");
  }
  input.value = "";
}

async function clearParcels() {
  await state.store.clearParcels();
  applyParcels(null);
}

// ---- PC 자료 파일 (조회 파생본 .j5view.zip, J5-023, ADR-17) ----
// 물건 목록·필지·정본 기록·거래를 한 번에 넣는다. 사진은 넣지 않는다. 이 기기의 관측·사진은 건드리지 않는다. 최신 여부는 파일만으로 모른다.
async function loadViewFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  input.value = "";
  if (file.size > VIEW_LIMITS.file) return text($("view-note"), `파일이 너무 큽니다 (${fmtBytes(file.size)}, 한도 ${fmtBytes(VIEW_LIMITS.file)}). PC 에서 사진 없이 다시 만듭니다`, "bad");
  text($("view-note"), "읽는 중…", "muted");
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const { manifest, files, photosSkipped } = await readViewZip(bytes);
    const [studyId, dataMode] = await Promise.all([state.store.getMeta("study_id"), state.store.getMeta("data_mode")]);
    const errs = validateViewManifest(manifest, { studyId: studyId || null, dataMode: dataMode ?? null });
    if (errs.length) return text($("view-note"), "PC 자료 파일 오류: " + errs.slice(0, 5).join("; "), "bad");
    const seed = JSON.parse(decodeText(files.get("assets.seed.json")));
    const seedErrs = validateSeed(seed);
    if (seedErrs.length) return text($("view-note"), "파일 안 물건 목록 오류: " + seedErrs.slice(0, 5).join("; "), "bad");
    const parcels = files.has("parcels.geojson") ? JSON.parse(decodeText(files.get("parcels.geojson"))) : null;
    if (parcels) {
      const pe = validateParcels(parcels);
      if (pe.length) return text($("view-note"), "파일 안 필지 오류: " + pe.slice(0, 5).join("; "), "bad");
    }
    const records = parseRecordsJsonl(decodeText(files.get("records.jsonl")));
    const transactions = files.has("transactions.json") ? JSON.parse(decodeText(files.get("transactions.json"))) : null;
    if (transactions) {
      const te = validateTransactionsDoc(transactions);
      if (te.length) return text($("view-note"), "파일 안 거래 목록 오류: " + te.slice(0, 5).join("; "), "bad");
    }
    // 파일들이 같은 정본 버전에서 나왔는지 manifest 와 대조한다 (리뷰 반영: 다른 정본의 거래·필지가 섞여 들어오지 않게)
    const ce = checkProjectionConsistency(manifest, { transactions, parcels });
    if (ce.length) return text($("view-note"), "PC 자료 파일 오류: " + ce.slice(0, 5).join("; "), "bad");
    // 설정이 비어 있으면 파일의 작업 공간·자료 종류를 설정으로 쓴다 (다르면 위에서 거절됨). 설정·물건·필지·정본 기록·거래를 한 트랜잭션으로 넣는다 (리뷰 반영):
    // 중간에 실패하면 이전 상태가 그대로 남고, 파생본에 필지가 없으면 기존 필지는 지운다 (같은 정본 버전의 자료만 남게)
    const source = "view:" + file.name;
    const { files: _f, ...manifestMeta } = manifest;
    await state.store.replaceProjection({
      settings: studyId ? null : { study_id: manifest.study_id, data_mode: manifest.data_mode },
      assets: seed, parcels, view: { manifest: manifestMeta, records, transactions, photos_skipped: photosSkipped }, source,
    });
    if (!studyId) await loadSettings();
    state.view = await state.store.getView();
    applyParcels(await state.store.getParcels());
    await renderAssets();
    await renderRecords();
    renderViewStatus();
    const sm = viewSummary(state.view);
    text($("view-note"), `PC 자료를 가져왔습니다: 정본 v${sm.version} · 물건 ${seed.length}개 · 필지 ${parcels ? parcels.count : 0}개 · 정본 기록 ${records.length}건 · 거래 ${sm.transactions}건` +
      (photosSkipped ? ` · 사진 ${photosSkipped}장은 넣지 않음` : "") + ". 이 기기의 기록은 그대로입니다.", "ok");
  } catch (e) {
    text($("view-note"), (e instanceof UnzipError ? `PC 자료 파일을 읽지 못함 [${e.code}]: ` : "PC 자료 파일을 읽지 못함: ") + (e?.message || e), "bad");
  }
}

function renderViewStatus() {
  const sm = viewSummary(state.view);
  if (!sm) return text($("view-status"), "가져온 PC 자료가 없습니다. 정본 기록·거래 이력은 PC 자료 파일을 넣으면 보입니다.");
  const gen = sm.generatedAt ? shortWhen(sm.generatedAt) ?? sm.generatedAt : "?";
  text($("view-status"), `정본 v${sm.version} (PC 생성 ${gen}) · 정본 기록 ${sm.records}건 · 거래 ${sm.transactions}건 · 가져오기 ${shortWhen(sm.loadedAt) ?? "?"} · 더 새 정본이 있는지는 PC 에서 확인`);
}

async function clearView() {
  await state.store.clearView();
  state.view = null;
  renderViewStatus();
  text($("view-note"), "PC 기록·거래를 지웠습니다. 물건 목록·필지·이 기기의 기록은 그대로입니다.", "muted");
}

function historyData() {
  return { events: state.events, records: state.view?.records ?? [], transactions: state.view?.transactions?.transactions ?? [] };
}

function historyItemNode(it) {
  const head = el("span", { class: "tl-head" }, el("span", { class: `badge ${it.kind === "transaction" ? "kind-transaction" : it.kind === "land_attrs" ? "kind-attrs" : ""}`, text: it.kindLabel }));
  if (it.subject) head.append(el("strong", { text: it.subject }));
  if (it.kind === "land_attrs") head.append(el("span", { class: "badge", text: it.first ? "토지 자료 처음 확인" : "토지 자료 변경" }));
  if (it.kind === "transaction") {
    if (it.match === "exact") head.append(el("span", { class: "badge match-exact", text: "이 필지" }));
    else if (it.match === "prefix") head.append(el("span", { class: "badge match-prefix", text: "번지대 (필지 미확정)" }));
    else if (it.match === "linked") head.append(el("span", { class: "badge match-exact", text: LINK_LABEL[it.link] ?? it.link }));
    if (it.masked && it.match !== "prefix") head.append(el("span", { class: "badge", text: "지번 일부 가려짐" }));
  }
  head.append(el("span", { class: `badge ${it.source === "device" ? "src-device" : "src-pc"}`, text: it.source === "device" ? (it.deviceStatus === "exported" ? "이 기기 · 내보냄" : "이 기기") : "정본" }));
  if (it.superseded) head.append(el("span", { class: "badge", text: "정정으로 대체됨" }));
  const li = el("li", { class: it.superseded ? "superseded" : "" }, el("span", { class: "tl-date", text: it.date || "날짜 미상" }), head, el("span", { class: "tl-text", text: it.text || "(내용 없음)" }));
  if (it.where) li.append(el("span", { class: "tl-where", text: it.where }));
  return li;
}

function openHistory({ title, sub, items, asset = null }) {
  if ($("sec-history").hidden) state.historyReturnFocus = document.activeElement;
  state.historyAsset = asset;
  $("history-title").textContent = title;
  $("history-sub").textContent = sub;
  const sm = viewSummary(state.view);
  text($("history-source"), sm ? `PC 자료: 정본 v${sm.version} (PC 생성 ${shortWhen(sm.generatedAt) ?? sm.generatedAt}) 기준. 더 새 정본이 있는지는 PC 에서 확인합니다.` : "PC 자료 파일을 아직 넣지 않아 이 기기의 관측만 보입니다.", "help");
  const groups = groupByYear(items);
  $("history-list").replaceChildren(...groups.map((g) => el("section", {}, el("h3", { text: g.year }), el("ul", {}, ...g.items.map(historyItemNode)))));
  $("history-empty").hidden = groups.length > 0;
  $("history-record").hidden = !asset;
  $("sec-history").hidden = false;
  $("history-title").setAttribute("tabindex", "-1");
  $("history-title").focus();
}

function openAssetHistory(asset) {
  const items = assetHistory(asset.asset_id, historyData());
  openHistory({ title: asset.label, sub: `${assetSub(asset)} · 기록 ${items.length}건`, items, asset });
}

function openParcelHistory(feature, assetsInside) {
  const p = feature.properties;
  const items = parcelHistory(feature, assetsInside, historyData());
  openHistory({ title: `${parcelTitle(p)} 필지`, sub: `필지 번호 ${feature.id} · 안의 물건 ${assetsInside.length}개 · 기록·거래 ${items.length}건`, items, asset: null });
}

function closeHistory() {
  $("sec-history").hidden = true;
  const back = state.historyReturnFocus;
  state.historyReturnFocus = null;
  const usable = back && back !== document.body && back.isConnected && !back.hidden && !back.closest("[hidden]") && typeof back.focus === "function";
  if (usable) back.focus();
  else {
    const h = document.querySelector("section.view:not([hidden]) h2");
    if (h) { h.setAttribute("tabindex", "-1"); h.focus(); }
  }
}

// ---- 배경 도형 (J5-022, ADR-16) ----
// 배경은 지도의 참고 층이다. 없거나 실패해도 목록·기록·내보내기·필지는 그대로 동작한다. 기록과 연결되지 않는다.
async function loadBasemapObject(doc, source) {
  const errs = validateBasemap(doc);
  if (errs.length) return text($("basemap-note"), "배경 파일 오류: " + errs.slice(0, 5).join("; ") + (errs.length > 5 ? ` 외 ${errs.length - 5}건` : ""), "bad");
  await state.store.replaceBasemap(doc, source);
  applyBasemap(await state.store.getBasemap());
}

function applyBasemap(rec) {
  state.basemapRec = rec ?? null;
  state.basemapCount = rec?.bundle?.features.length ?? 0;
  mapCall((m) => m.setBasemap(rec?.bundle ?? null));
  basemapNote(rec);
  mapCall((m) => mapNote(m.setAssets(state.assets)));
}

function basemapNote(rec) {
  if (!rec) return text($("basemap-note"), "배경 없음. 지도에는 물건 위치와 필지만 보입니다.", "muted");
  const b = rec.bundle, s0 = b.sources[0];
  const parts = BASEMAP_LAYERS.filter((k) => b.counts[k] > 0).map((k) => `${BASEMAP_LAYER_LABEL[k]} ${b.counts[k]}`).join(" · ");
  const when = shortWhen(rec.loaded_at);
  text($("basemap-note"), `배경 도형 ${b.count}개 (${parts}) · ${b.data_mode === "synthetic" ? "연습용" : s0.name} · 도형 기준일 ${s0.geometry_version} · 이용허락 ${s0.license ?? "미확인"}` +
    (when ? ` · 가져오기 ${when}` : ""), "muted");
}

async function loadSyntheticBasemap() {
  try {
    const res = await fetch("data/basemap.synthetic.j5basemap.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadBasemapObject(await res.json(), "bundled_synthetic");
  } catch (e) {
    text($("basemap-note"), "연습용 배경을 읽지 못함: " + e, "bad");
  }
}

async function loadBasemapFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    await loadBasemapObject(JSON.parse(await file.text()), "file:" + file.name);
  } catch (e) {
    text($("basemap-note"), "배경 파일을 읽지 못함: " + e, "bad");
  }
  input.value = "";
}

async function clearBasemap() {
  await state.store.clearBasemap();
  applyBasemap(null);
}

function showParcel(feature) {
  const p = feature.properties;
  mapCall((m) => m.selectParcel(feature.id));
  $("parcel-title").textContent = parcelTitle(p);
  $("parcel-mode").textContent = state.parcels?.data_mode === "synthetic" ? "연습용 필지" : "연속지적도";
  $("parcel-pnu").textContent = feature.id;
  const src = state.parcels?.source;
  $("parcel-meta").textContent = `도형면적 ${fmtArea(p.area_m2_geom, p.area_missing_reason)} (공부면적 아님)` + (p.jimok ? ` · 지목 ${p.jimok}` : "") + (p.jibun_mismatch ? " · 원본 지번과 PNU 불일치" : "") +
    (src ? ` · ${src.name} ${src.geometry_version}` : "");
  renderExtLinks($("parcel-ext"), bboxCenter(p.bbox));
  const lines = attrLines(p.attrs, src?.geometry_version ?? null);
  $("parcel-attrs").replaceChildren(...lines.map(([k, v]) => el("li", {}, el("span", { class: "k", text: k }), el("span", { class: "v", text: v }))));
  $("parcel-attrs").hidden = !lines.length;
  $("parcel-attrs-note").hidden = !lines.length;
  const linksValid = parcelLinksValid();
  const inside = parcelAssets(feature, state.assets, { linksValid });
  $("parcel-assets").replaceChildren(...inside.map(({ asset: a, basis }) => el("li", {},
    el("span", { class: "title", text: a.label }), modeBadge(a.data_mode), el("span", { class: "badge", text: BASIS_LABEL[basis] }),
    el("button", { text: "기록하기", onclick: () => startObservation(a) }),
  )));
  if (!linksValid && (p.asset_ids ?? []).length) $("parcel-assets").append(el("li", { class: "warn", text: `정본 연결 ${p.asset_ids.length}건은 시드가 바뀐 뒤 확인되지 않아 표시하지 않는다. 같은 파생본의 시드와 필지 파일을 함께 다시 불러온다.` }));
  if (!inside.length) $("parcel-assets").append(el("li", { class: "empty", text: "이 필지에 연결되거나 위치점이 들어 있는 물건이 없다. 아래 버튼으로 이 필지를 물건으로 만들어 바로 기록할 수 있다 (PC 반영 때 임시 매입 단위로 승계)." }));
  $("parcel-new-asset").hidden = inside.length > 0;
  $("parcel-new-asset").onclick = () => createAssetFromParcel(feature);
  $("parcel-history").onclick = () => openParcelHistory(feature, inside.map((x) => x.asset));
  $("parcel-panel").hidden = false;
}

/** 필지에서 물건 만들기 (J5-028, ADR-21): 이 기기에만 있는 임시 매입 단위. 위치점은 필지 안의 점, 이름은 동·지번. 내보내기에 assets.new.json 으로 실려 PC 가 pending 물건으로 만든다. */
async function createAssetFromParcel(feature) {
  const pt = interiorPoint(feature);
  if (!pt) return text($("parcel-search-note"), "이 필지 안의 점을 정하지 못해 물건을 만들 수 없습니다.", "warn");
  const dataMode = (await state.store.getMeta("data_mode")) ?? "synthetic";
  const asset = { asset_id: uuid4(), label: parcelTitle(feature.properties), location_point: pt, data_mode: dataMode, created_at: isoWithOffset(), notes: null, pnu: feature.id, origin: "device" };
  try {
    await state.store.addDeviceAsset(asset);
  } catch (e) {
    return text($("parcel-search-note"), "물건을 저장하지 못했습니다: " + (e?.message || e), "bad");
  }
  await renderAssets();
  closeParcelPanel();
  text($("parcel-search-note"), `${asset.label} 물건을 이 기기에 만들었습니다 (PC 반영 전). 기록을 내보내면 PC 가 임시 매입 단위로 승계합니다.`, "ok");
  startObservation(asset);
}

async function deleteDeviceAsset(asset) {
  try {
    await state.store.deleteDeviceAsset(asset.asset_id);
    text($("seed-note"), `${asset.label} (이 기기에서 만든 물건) 을 지웠습니다.`, "muted");
  } catch (e) {
    text($("seed-note"), "지우지 못했습니다: " + (e?.message || e), "warn");
  }
  await renderAssets();
  await renderRecords();
}

function closeParcelPanel() {
  $("parcel-panel").hidden = true;
  renderExtLinks($("parcel-ext"), null);
  mapCall((m) => m.selectParcel(null));
}

// ---- 지도 (J5-005) ----
// 지도는 보조 화면이다. 모듈을 못 읽거나 만들거나 갱신하다 실패하면 안내만 남기고 목록·기록·내보내기는 그대로 동작한다.
async function initMap() {
  try {
    const { createMap } = await import("./map.js");
    state.map = createMap($("map-svg"), { onSelect: selectOnMap, onSelectParcel: showParcel, onTileStatus });
    $("map-zoom-in").addEventListener("click", () => mapCall((m) => m.zoomBy(2)));
    $("map-zoom-out").addEventListener("click", () => mapCall((m) => m.zoomBy(0.5)));
    $("map-fit").addEventListener("click", () => mapCall((m) => m.fit()));
    $("map-zones").addEventListener("click", toggleZoneColors);
  } catch (e) {
    mapFailed(e);
  }
}

function mapFailed(e) {
  try { state.map?.destroy(); } catch {}
  state.map = null;
  $("map-empty").hidden = false;
  $("map-empty").replaceChildren(el("div", {}, el("strong", { text: "지도를 그릴 수 없습니다" }), document.createTextNode("오늘 화면의 목록에서 대상을 골라 기록할 수 있습니다.")));
  text($("map-note"), `지도 표시 불가: ${e?.message || e}. 목록에서 선택한다`, "warn");
}

function mapCall(fn) {
  if (!state.map) return undefined;
  try { return fn(state.map); } catch (e) { mapFailed(e); return undefined; }
}

function mapNote(c) {
  if (!c) return;
  state.lastMapCounts = c;
  $("map-empty").hidden = c.total > 0 || state.parcelsCount > 0 || state.basemapCount > 0 || !!state.tiles;
  const ts = state.tiles ? mapCall((m) => m.tileStatus()) : null;
  const tileNote = state.tiles ? (ts?.failed ? ` · 배경 타일 ${ts.failed}장 못 받음 (${navigator.onLine ? "제공자 응답 없음 또는 키·주소 확인" : "오프라인, 저장된 타일만 보임"})` : " · 배경 타일 켜짐") : "";
  const parcels = (state.parcelsCount ? ` · 필지 ${state.parcelsCount}개` : "") + (state.basemapCount ? ` · 배경 ${state.basemapCount}개` : "") + tileNote;
  if (c.total === 0) return text($("map-note"), parcels ? `물건 없음${parcels}` : "", "muted");
  if (c.located === 0) return text($("map-note"), `위치점 있는 물건이 없다. 목록에서 선택한다${parcels}`, "muted");
  let msg = `위치점 ${c.located}개 표시 (가상 ${c.synthetic} · 실제 ${c.privateReal})`;
  if (c.unlocated > 0) msg += ` · 위치점 없는 물건 ${c.unlocated}개 (목록에서 선택)`;
  text($("map-note"), msg + parcels, "muted");
}

/** 지도의 점을 탭하면 바로 기록 화면을 열지 않고 대상 요약과 '기록 시작' 을 보여 준다. */
function selectOnMap(asset, { focus = true } = {}) {
  state.mapSelected = asset.asset_id;
  mapCall((m) => m.select(asset.asset_id));
  const s = assetSummary(asset.asset_id, state.events);
  $("map-selected-label").textContent = asset.label;
  $("map-selected-sub").textContent = assetSub(asset) + (s.count ? ` · 마지막 기록 ${shortWhen(s.lastAt)} (${CHANGE_STATUS_LABEL[s.lastStatus] ?? ""})` : " · 이 기기에 기록 없음");
  renderExtLinks($("map-selected-ext"), asset.location_point);
  $("map-selected").hidden = false;
  $("map-selected-start").onclick = () => startObservation(asset);
  $("map-selected-history").onclick = () => openAssetHistory(asset);
  for (const li of $("asset-list").children) li.classList.toggle("selected", li.dataset.assetId === asset.asset_id);
  if (focus) $("map-selected-start").focus();
}

function clearMapSelection() {
  state.mapSelected = null;
  $("map-selected").hidden = true;
  $("map-selected-start").onclick = null;
  $("map-selected-history").onclick = null;
  renderExtLinks($("map-selected-ext"), null);
  mapCall((m) => m.select(null));
  for (const li of $("asset-list").children) li.classList.remove("selected");
}

// ---- 관측 ----
function statusValue() {
  return document.querySelector('input[name="change-status"]:checked')?.value ?? "";
}

function setStatusValue(v) {
  for (const r of document.querySelectorAll('input[name="change-status"]')) r.checked = r.value === v;
}

function startObservation(asset) {
  state.target = asset;
  state.photos = [];
  state.prevPhotos = [];
  // 되돌릴 포커스는 처음 열 때의 요소다. '다음 대상 기록' 으로 이어서 열 때는 유지한다 (리뷰 반영)
  if ($("sec-observe").hidden) state.returnFocus = document.activeElement;
  loadPrevPhotos(asset.asset_id);
  mapCall((m) => m.select(asset.asset_id));
  $("target-label").textContent = asset.label;
  const s = assetSummary(asset.asset_id, state.events);
  $("target-sub").textContent = assetSub(asset) + (s.count ? ` · 마지막 기록 ${shortWhen(s.lastAt)}` : "");
  setStatusValue("");
  $("date-only").checked = false;
  $("observed-at").type = "datetime-local";
  $("observed-at").value = toDatetimeLocal(new Date());
  updateObservedSummary();
  $("note").value = "";
  $("photos").value = "";
  $("photo-list").replaceChildren();
  text($("observe-note"), "", "");
  $("observe-done").hidden = true;
  $("observe-form").hidden = false;
  $("sec-observe").hidden = false;
  $("observe-title").setAttribute("tabindex", "-1");
  $("observe-title").focus();
}

function closeObservation({ toView = null } = {}) {
  $("sec-observe").hidden = true;
  state.target = null;
  mapCall((m) => m.select(state.mapSelected));
  if (toView) state.nav.show(toView);
  const back = state.returnFocus;
  state.returnFocus = null;
  if (toView) return;
  // 처음 연 요소로 되돌린다. 그 요소가 없거나 본문(body)이면 현재 화면의 제목으로 옮겨 숨은 대화상자에 포커스가 남지 않게 한다
  const usable = back && back !== document.body && back.isConnected && !back.hidden && !back.closest("[hidden]") && typeof back.focus === "function";
  if (usable) back.focus();
  else {
    const h = document.querySelector("section.view:not([hidden]) h2");
    if (h) { h.setAttribute("tabindex", "-1"); h.focus(); }
  }
}

function updateObservedSummary() {
  const v = $("observed-at").value;
  $("observed-summary").textContent = v ? `(${$("date-only").checked ? v : v.replace("T", " ")})` : "";
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
      if (state.photos.some((p) => p.sha256 === item.sha256)) item.error = "같은 사진이 이미 선택됨 (이 항목을 제거)";
    }
    state.photos.push(item);
  }
  renderPhotos();
  input.value = "";
}

function renderPhotos() {
  $("photo-list").replaceChildren(...state.photos.map((p, idx) => {
    const box = el("div", { class: "photo-item" });
    box.append(el("div", { text: `${p.name} · ${fmtBytes(p.bytes)} · ${p.ext ?? "형식 모름"}` }));
    if (p.error) box.append(el("div", { class: "bad", text: p.error }));
    else {
      const tags = el("div", { class: "tags" });
      for (const t of PHOTO_TAGS) {
        const box2 = el("input", { type: "checkbox", checked: p.tags.has(t) });
        box2.addEventListener("change", () => { box2.checked ? p.tags.add(t) : p.tags.delete(t); });
        tags.append(el("label", {}, box2, ` ${PHOTO_TAG_LABEL[t]}`));
      }
      box.append(el("div", { class: "field-name", text: "무엇을 찍었나요? (선택)" }), tags);
      // 반복 촬영(연차 비교)용 선택 입력. 이전 사진은 이 기기에 저장된 같은 물건의 사진 중에서 고른다. 기본 흐름을 막지 않게 접어 둔다
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
      box.append(el("details", { class: "disclosure" }, el("summary", { text: "촬영 정보 추가 (선택)" }), series));
      box.append(el("div", { class: "muted small", text: "고유값 " + p.sha256.slice(0, 12) + "…" }));
    }
    box.append(el("button", { text: "제거", class: "danger", onclick: () => { state.photos.splice(idx, 1); renderPhotos(); } }));
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
  $("save-observation").textContent = "저장 중…";
  try {
    await doSaveObservation();
  } finally {
    state.saving = false;
    $("save-observation").disabled = false;
    $("save-observation").textContent = "저장";
  }
}

async function doSaveObservation() {
  const note = $("note").value;
  const status = statusValue();
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
  if (problems.length) return text($("observe-note"), "저장하지 못했습니다: " + problems.join(". "), "bad");

  const [routeVersionId, studyId, dataMode] = await Promise.all([
    state.store.getMeta("route_version_id"), state.store.getMeta("study_id"), state.store.getMeta("data_mode"),
  ]);
  if (!studyId) return text($("observe-note"), "설정에서 작업 공간 이름을 먼저 저장해야 한다", "bad");
  if (state.target.data_mode !== (dataMode ?? "synthetic")) {
    return text($("observe-note"), `물건의 자료 종류(${MODE_SHORT[state.target.data_mode]})와 설정(${MODE_SHORT[dataMode ?? "synthetic"]})이 다르다. 설정을 맞춘 뒤 저장`, "bad");
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
  await renderRecords();
  await renderAssets();
  showObservationDone(record, photos.length);
}

function showObservationDone(record, photoCount) {
  const asset = state.target;
  $("done-text").textContent = `${asset.label} · ${CHANGE_STATUS_LABEL[record.event.payload.change_status]} · 사진 ${photoCount}장. 이 기기에 저장됐고 아직 내보내지 않았습니다. PC 반영 여부는 이 화면에서 알 수 없습니다.`;
  const next = nextAsset(state.assets, asset.asset_id, state.events);
  $("done-next").hidden = !next;
  if (next) $("done-next").textContent = `다음 대상 기록: ${next.label}`;
  $("done-next").onclick = () => { if (next) startObservation(next); };
  $("observe-form").hidden = true;
  $("observe-done").hidden = false;
  $("done-title").setAttribute("tabindex", "-1");
  $("done-title").focus();
}

// ---- 내보내기 ----
// state.export = { studyId, dataMode, batches, errors, k, package, url, attempted }
function exportNote(msg, cls = "") { text($("export-note"), msg, cls); }

async function exportPlan({ quiet = false } = {}) {
  const [studyId, dataMode] = await Promise.all([state.store.getMeta("study_id"), state.store.getMeta("data_mode")]);
  if (!studyId) { if (!quiet) exportNote("설정에서 작업 공간 이름을 먼저 저장해야 한다", "bad"); text($("export-summary"), "작업 공간 이름이 없어 내보낼 수 없습니다. 설정에서 저장하세요.", "summary warn"); return null; }
  const records = await state.store.listEvents();
  const sel = selectRecords(records, { studyId, dataMode: dataMode ?? "synthetic", includeExported: $("include-exported").checked });
  const meta = await state.store.photoMeta();
  const { batches, errors } = planBatches(sel.selected, meta);
  const photoCount = new Set(batches.flatMap((b) => b.shas)).size;
  const est = batches.reduce((s, b) => s + b.estBytes, 0);
  text($("export-summary"), `대상 ${sel.selected.length}건 · 사진 ${photoCount}장 · 예상 ${fmtBytes(est)} · 묶음 ${batches.length}개` +
    (sel.excludedExported ? ` · 이미 내보낸 ${sel.excludedExported}건 제외` : "") + (sel.otherContext ? ` · 다른 작업 공간/자료 종류 ${sel.otherContext}건 제외` : "") +
    (sel.selected.length ? "" : " · 내보낼 기록이 없습니다"), "summary");
  $("export-errors").replaceChildren(...errors.map((e) => el("li", { class: "bad", text: `${e.event_id.slice(0, 8)}… 제외: ${e.reason}` })));
  return { studyId, dataMode: dataMode ?? "synthetic", batches, errors, k: 0, package: null, url: null, attempted: false, recordsById: new Map(sel.selected.map((r) => [r.event_id, r])) };
}

function fmtBytes(n) {
  return n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : n >= 1e3 ? (n / 1e3).toFixed(0) + " KB" : n + " B";
}

function renderExportSteps() {
  const ex = state.export;
  const step = ex ? exportStep({ hasPackage: !!ex.package, attempted: !!ex.attempted, done: false }) : 1;
  for (const li of $("export-steps").children) {
    const n = Number(li.dataset.step);
    li.classList.toggle("done", n < step);
    li.classList.toggle("current", n === step);
    if (n === step) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  }
}

async function exportPrepare() {
  $("export-prepare").disabled = true;
  try {
    if (state.export?.url) { URL.revokeObjectURL(state.export.url); state.export.url = null; }
    // 기존 계획에 남은 묶음이 있으면 그 계획의 k번째 묶음을 만든다. "파일 만들기" 버튼은 state.export 를 비워 새 계획을 잡는다.
    const plan = hasRemainingBatches(state.export) ? state.export : await exportPlan();
    if (!plan) return;
    if (!plan.batches.length) { state.export = null; $("export-save").disabled = true; $("export-confirm").disabled = true; return exportNote("내보낼 기록이 없다", "muted"); }
    const k = plan.k;
    const batch = plan.batches[k];
    exportNote(`묶음 ${k + 1}/${plan.batches.length} 만드는 중…`, "muted");
    const pkg = await buildPackage(batch, plan.recordsById, {
      loadPhoto: async (sha) => { const p = await state.store.getPhoto(sha); return p ? { blob: p.blob, ext: p.ext } : undefined; },
      studyId: plan.studyId, dataMode: plan.dataMode, k: k + 1, n: plan.batches.length,
      newAssets: await state.store.listDeviceAssets(),   // 이 묶음의 기록이 가리키는 기기 생성 물건만 assets.new.json 으로 (J5-028)
    });
    plan.package = pkg; plan.attempted = false;
    state.export = plan;
    $("export-save").textContent = `파일 저장 ${k + 1}/${plan.batches.length}`;
    $("export-save").disabled = false;
    $("export-confirm").disabled = true;
    exportNote(`묶음 ${k + 1}/${plan.batches.length} 준비됨: ${pkg.filename} (${fmtBytes(pkg.bytes)}, 관측 ${pkg.eventIds.length}건, 사진 ${pkg.photoCount}장${pkg.newAssetCount ? `, 기기가 만든 물건 ${pkg.newAssetCount}개` : ""}). 다음은 "파일 저장" 입니다.`, "ok");
  } catch (e) {
    exportNote("묶음 준비 실패: " + (e?.message || e), "bad");
  } finally {
    $("export-prepare").disabled = false;
    renderExportSteps();
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
  renderExportSteps();
  exportNote(`${ex.package.filename} 저장을 요청했다. '파일' 앱 등에서 실제로 저장됐는지 확인한 뒤 "저장 확인"을 누른다. 확인 전에는 아직 내보냄이 아니다.`, "warn");
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
  await renderAssets();
  await renderExportHistory();
  if (ex.k < ex.batches.length) {
    exportNote(`묶음 ${ex.k}/${ex.batches.length} 확인됨. 다음 묶음을 준비한다.`, "ok");
    await exportPrepare();
  } else {
    exportNote(`묶음 ${ex.batches.length}개 모두 확인됨 (내보냄). PC 반영·백업 여부는 이 화면에서 알 수 없다.`, "ok");
    state.export = null;
    await exportPlan({ quiet: true });
    renderExportSteps();
  }
}

async function renderExportHistory() {
  const list = await state.store.listExports();
  $("export-history").replaceChildren(...list.slice().reverse().map((e) => el("li", {},
    el("div", { text: `${e.filename} · 관측 ${e.event_ids.length}건 · 사진 ${e.photo_count}장 · ${fmtBytes(e.bytes)}` }),
    el("div", { class: e.confirmed_at ? "ok" : "warn", text: e.confirmed_at ? `저장 확인 ${e.confirmed_at}` : "저장 미확인 (다시 내보낼 수 있음)" }),
  )));
  if (!list.length) $("export-history").append(el("li", { class: "empty", text: "없음" }));
}

// ---- 목록·상태 ----
async function renderRecords() {
  const events = await state.store.listEvents();
  state.events = events;
  const labels = new Map(state.assets.map((a) => [a.asset_id, a.label]));
  $("record-list").replaceChildren(...events.map((r) => el("li", {},
    el("span", { class: "title" }, el("span", { text: labels.get(r.asset_id) ?? r.asset_label ?? r.asset_id }),
      labels.has(r.asset_id) ? el("span") : el("span", { class: "warn", text: " (현재 목록에 없는 물건)" })),
    el("span", { class: r.status === "saved" ? "badge state-saved" : "badge state-exported", text: r.status === "saved" ? "저장됨" : "내보냄" }),
    el("span", { class: "sub" }, el("span", { class: `badge status-${r.event.payload.change_status}`, text: CHANGE_STATUS_LABEL[r.event.payload.change_status] }),
      document.createTextNode(` ${r.event.observed_at.replace("T", " ").slice(0, 16)} · 사진 ${r.event.attachment_refs.length}장 · `), modeBadge(r.data_mode), document.createTextNode(` ${r.study_id}`)),
    r.event.payload.note ? el("div", { class: "note", text: r.event.payload.note }) : el("span"),
  )));
  if (!events.length) $("record-list").append(el("li", { class: "empty", text: "저장된 기록이 없습니다. 오늘 화면에서 대상을 골라 기록하세요." }));
  text($("record-count"), `${events.length}건`);
  await renderMigrate(events);
  await refreshStatus();
}

// ---- 기기·도메인 이전 준비 (J5-017D) ----
async function renderMigrate(events) {
  const [studyId, dataMode] = await Promise.all([state.store.getMeta("study_id"), state.store.getMeta("data_mode")]);
  const r = migrationReadiness(events, { studyId: studyId ?? null, dataMode: dataMode ?? "synthetic" });
  const m = migrationText(r);
  text($("migrate-note"), m.text, m.cls);
  text($("migrate-origin"), location.origin);
  let persisted = null;
  try {
    if (navigator.storage?.persisted) persisted = await navigator.storage.persisted();
  } catch { persisted = null; }
  text($("migrate-storage"), persistenceText(persisted));
  renderHome(r);
}

function renderHome(readiness) {
  const c = { assets: state.assets.length, events: state.events.length };
  const unexported = readiness.unexported;
  $("stat-targets").textContent = String(c.assets);
  $("stat-records").textContent = String(c.events);
  $("stat-unexported").textContent = String(unexported);
  $("stat-unexported").parentElement.classList.toggle("zero", unexported === 0);
  let last = null;
  for (const r of state.events) if (!last || r.saved_at > last.saved_at) last = r;
  $("home-last").textContent = last ? `최근 저장: ${shortWhen(last.saved_at)} · ${last.asset_label ?? ""} · ${last.status === "saved" ? "이 기기에 저장됨 (아직 안 내보냄)" : "파일로 내보냄 (PC 반영 여부는 PC 에서 확인)"}` : "아직 저장된 기록이 없습니다.";
  $("home-empty").hidden = c.assets > 0;
  $("home-cta").hidden = c.assets === 0;
  $("start-observing").textContent = c.events ? "관측 계속하기" : "관측 시작";
  const nb = $("nav-export-badge");
  nb.hidden = unexported === 0;
  nb.textContent = String(unexported);
  nb.setAttribute("aria-label", `안 내보낸 기록 ${unexported}건`);
  const rb = $("nav-records-badge");
  rb.hidden = c.events === 0;
  rb.textContent = String(c.events);
  rb.setAttribute("aria-label", `기록 ${c.events}건`);
}

async function refreshStatus() {
  const c = await state.store.counts();
  const studyId = await state.store.getMeta("study_id");
  const mode = (await state.store.getMeta("data_mode")) ?? "synthetic";
  text($("status-line"), `앱 ${APP_VERSION} · ${mode} · study ${studyId ?? "(미설정)"} · 물건 ${c.assets} · 관측 ${c.events} · 사진 ${c.photos} · ${navigator.onLine ? "온라인" : "오프라인"}`);
  $("offline-banner").hidden = navigator.onLine;
}

function registerSw() {
  // 최소 앱 캐시. 보안 컨텍스트가 아니면 등록되지 않으며 앱은 그대로 동작한다.
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register("./sw.js").catch(() => {});
}

/** 오늘 화면의 '관측 시작': 이 기기에 기록이 없는 첫 대상, 없으면 첫 대상의 기록 화면을 연다. */
function startObservingFromHome() {
  const next = nextAsset(state.assets, null, state.events) ?? state.assets[0];
  if (next) startObservation(next);
}

function onViewShown(name, changed) {
  if (name === "map") mapCall((m) => { m.resize(); if (changed) m.fit(); });
  if (name === "export") { exportPlan({ quiet: true }).then(renderExportSteps).catch(() => {}); }
}

async function main() {
  try {
    state.store = await Store.open();
  } catch (e) {
    const f = $("fatal");
    f.hidden = false;
    f.textContent = "이 브라우저에서는 기록을 저장할 수 없습니다 (브라우저 저장소를 열 수 없음: " + e + "). 다른 브라우저나 일반 창에서 여세요.";
    return;
  }
  const sections = Object.fromEntries(Array.from(document.querySelectorAll("section.view")).map((s) => [s.dataset.view, s]));
  state.nav = createNavigator({ sections, navButtons: Array.from(document.querySelectorAll(".bottom-nav .nav-btn")), onShow: onViewShown });
  state.nav.show(viewFromHash(location.hash), { focus: false });
  await loadSettings();
  // 버튼·입력 리스너는 목록·기록을 그리기 전에 붙인다. 첫 화면 직후의 탭이 무시되지 않게 한다.
  $("save-settings").addEventListener("click", saveSettings);
  $("load-synthetic").addEventListener("click", loadSyntheticSeed);
  $("home-load-synthetic").addEventListener("click", loadSyntheticSeed);
  $("home-go-settings").addEventListener("click", () => state.nav.show("settings"));
  $("home-go-export").addEventListener("click", () => state.nav.show("export"));
  $("map-go-list").addEventListener("click", () => state.nav.show("home"));
  $("start-observing").addEventListener("click", startObservingFromHome);
  $("seed-file").addEventListener("change", (e) => loadSeedFile(e.target));
  $("load-synthetic-parcels").addEventListener("click", loadSyntheticParcels);
  $("parcels-file").addEventListener("change", (e) => loadParcelsFile(e.target));
  // 필지 찾기·현재 위치는 지도가 못 떠도 동작한다 (검색·포함 판정은 순수 함수, 패널은 DOM). 지도 초기화와 무관하게 여기서 잇는다 (리뷰 반영 PR #73)
  $("parcel-search-form").addEventListener("submit", (e) => { e.preventDefault(); searchParcels($("parcel-search").value); });
  $("map-locate").addEventListener("click", locateMe);
  $("clear-parcels").addEventListener("click", clearParcels);
  $("load-synthetic-basemap").addEventListener("click", loadSyntheticBasemap);
  $("basemap-file").addEventListener("change", (e) => loadBasemapFile(e.target));
  $("clear-basemap").addEventListener("click", clearBasemap);
  $("tiles-provider").addEventListener("change", updateTileFields);
  $("tiles-template").addEventListener("input", updateTileFields);
  $("tiles-save").addEventListener("click", saveTileSettings);
  $("tiles-prefetch").addEventListener("click", prefetchTiles);
  $("tiles-clear").addEventListener("click", clearTileCache);
  $("view-file").addEventListener("change", (e) => loadViewFile(e.target));
  $("clear-view").addEventListener("click", clearView);
  $("history-close").addEventListener("click", closeHistory);
  $("history-close-2").addEventListener("click", closeHistory);
  $("history-record").addEventListener("click", () => { const a = state.historyAsset; closeHistory(); if (a) startObservation(a); });
  $("parcel-close").addEventListener("click", closeParcelPanel);
  $("photos").addEventListener("change", (e) => addPhotos(e.target));
  $("save-observation").addEventListener("click", saveObservation);
  $("cancel-observation").addEventListener("click", () => closeObservation());
  $("cancel-observation-2").addEventListener("click", () => closeObservation());
  $("done-list").addEventListener("click", () => closeObservation({ toView: "home" }));
  $("done-records").addEventListener("click", () => closeObservation({ toView: "records" }));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("sec-history").hidden) closeHistory();
    else if (!$("sec-observe").hidden) closeObservation();
  });
  $("export-prepare").addEventListener("click", () => { state.export = null; exportPrepare(); });
  $("export-save").addEventListener("click", exportSave);
  $("export-confirm").addEventListener("click", exportConfirm);
  $("include-exported").addEventListener("change", () => { state.export = null; $("export-save").disabled = true; $("export-confirm").disabled = true; exportNote("", ""); exportPlan({ quiet: true }).then(renderExportSteps).catch(() => {}); });
  $("date-only").addEventListener("change", (e) => { $("observed-at").type = e.target.checked ? "date" : "datetime-local"; $("observed-at").value = e.target.checked ? localDate() : toDatetimeLocal(); updateObservedSummary(); });
  $("observed-at").addEventListener("input", updateObservedSummary);
  window.addEventListener("online", refreshStatus);
  window.addEventListener("offline", refreshStatus);
  await initMap();
  try {
    await loadTileSettings();
  } catch (e) {
    text($("tiles-note"), "배경 지도 설정을 읽지 못함: " + (e?.message || e), "bad");
  }
  try {
    const rec = await state.store.getParcels();
    state.seedLoadedAt = (await state.store.getMeta("seed_loaded_at")) ?? null;
    if (rec) { state.parcelsRec = rec; state.parcels = rec.bundle; state.parcelsCount = rec.bundle.features.length; mapCall((m) => m.setParcels(rec.bundle)); }
    state.zoneColors = (await state.store.getMeta("zone_colors")) === true;
    applyZoneColors();
    applySearchUi();
    parcelsNote(rec ?? null);
  } catch (e) {
    text($("parcels-note"), "저장된 필지를 읽지 못함: " + (e?.message || e), "bad");
  }
  try {
    state.view = (await state.store.getView()) ?? null;
    renderViewStatus();
  } catch (e) {
    text($("view-note"), "저장된 PC 자료를 읽지 못함: " + (e?.message || e), "bad");
  }
  try {
    const rec = await state.store.getBasemap();
    if (rec) { state.basemapRec = rec; state.basemapCount = rec.bundle.features.length; mapCall((m) => m.setBasemap(rec.bundle)); }
    basemapNote(rec ?? null);
  } catch (e) {
    text($("basemap-note"), "저장된 배경을 읽지 못함: " + (e?.message || e), "bad");
  }
  await renderAssets();
  await renderRecords();
  await renderExportHistory();
  renderExportSteps();
  if (state.nav.current === "map") mapCall((m) => { m.resize(); m.fit(); });
  registerSw();
}

main();
