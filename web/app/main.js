// 앱 진입점 (R1a). 화면: 현장(지도 + 아래 시트의 대상 목록) · 기록(PC 로 보내기 + 이 폰의 기록) · 설정 (J5-054). 기록하기는 전체 화면 작업 패널(sheet)에서 한다 (J5-020).
// 저장 형식·이벤트 바이트·해시·ZIP·IndexedDB 계약은 그대로다. 외부 통신 없음.
import { Store } from "./db.js";
import { sniffImage, SUPPORTED } from "./sniff.js";
import { sha256Hex } from "./hash.js";
import { uuid4, isUuid } from "./uuid.js";
import { isoWithOffset, fromDatetimeLocal, toDatetimeLocal, localDate } from "./time.js";
import { buildEvent, validateEvent, lineBytes, PHOTO_TAGS, PHOTO_TAG_LABEL, CHANGE_STATUS_LABEL, PHOTO_LIMIT, VIEWPOINT_MAX } from "./event.js";
import { validateSeed, filterAssets, hasTracking, isWatchlist, TRACKING_LABEL, ASSET_FILTERS } from "./seed.js";
import { validateParcels, parcelAssets, BASIS_LABEL, parcelTitle, fmtArea, attrLines, attrsSummary, ZONE_LABELS, findParcels, parcelAt, interiorPoint, priceTrend, priceTrendRow, ownershipKinds, ownershipChanges, parseFilter, hasCriteria, filterParcels, filterRowText, FILTER_LIMIT } from "./parcels.js";
import { selectRecords, planBatches, buildPackage, hasRemainingBatches, studyIdError } from "./export.js";
import { migrationReadiness, migrationText, persistenceText } from "./migrate.js";
import { createNavigator, viewFromHash, shortWhen, assetSummary, nextAsset, exportStep, MODE_LABEL, MODE_SHORT } from "./ui.js";
import { externalMapLinks, bboxCenter, LINK_ATTRS } from "./extmap.js";
import { validateBasemap, LAYERS as BASEMAP_LAYERS, LAYER_LABEL as BASEMAP_LAYER_LABEL } from "./basemap.js";
import { PROVIDERS, resolveTileConfig, prefetchPlan, padBbox, tileUrl, TILE_CACHE, PREFETCH_ZOOMS, KEY_PLACEHOLDER } from "./tiles.js";
import { readViewZip, decodeText, UnzipError, VIEW_LIMITS } from "./unzip.js";
import { validateViewManifest, parseRecordsJsonl, validateTransactionsDoc, checkProjectionConsistency, assetHistory, parcelHistory, groupByYear, viewSummary, LINK_LABEL, parcelYearSummaryInfo, yearSummaryRow, yearSummaryCells, recentDeals } from "./view.js";
// 지도 모듈(map.js)은 선택 기능이라 정적 import 하지 않는다. 로드 실패가 앱 전체(목록·기록·내보내기)를 막지 않도록 initMap 안에서 동적으로 불러온다.

export const APP_VERSION = "0.2.24";
const PARCEL_CARD_YEARS = 5;   // 필지 카드의 연도별 표에 보이는 최근 연도 수 (J5-055)
const PARCEL_CARD_DEALS = 3;   // 필지 카드의 최근 거래 수 (J5-057)
const $ = (id) => document.getElementById(id);
const state = { store: null, parcelFilter: null, assets: [], events: [], target: null, photos: [], prevPhotos: [], saving: false, export: null, map: null, parcels: null, parcelsCount: 0, parcelsRec: null, zoneColors: false, seedLoadedAt: null, seedSource: null, basemapRec: null, basemapCount: 0, view: null, historyReturnFocus: null, historyAsset: null, tiles: null, prefetching: false, lastMapCounts: null,
                nav: null, returnFocus: null, mapSelected: null, assetFilter: "all" };

// 대상 목록은 synthetic·private_real, 필지·배경 번들은 synthetic·real 을 쓴다 (리뷰 반영 PR #103)
const DATA_MODE_TEXT = { synthetic: "연습용 자료", private_real: "실제 자료", real: "실제 자료" };

/** 가져온 자리 (IndexedDB 의 source 값) 를 화면 글로 (J5-056) */
function sourceText(source) {
  if (source === "bundled_synthetic") return "앱에 든 연습용 파일";
  if (typeof source === "string" && source.startsWith("file:")) return `파일 ${source.slice(5)}`;
  if (typeof source === "string" && source.startsWith("view:")) return `PC 자료 파일 ${source.slice(5)}`;
  return String(source ?? "출처 모름");
}

function text(el, value, cls) {
  el.textContent = value;
  if (cls !== undefined) el.className = cls;
}

function modeBadge(mode) {
  return el("span", { class: `badge mode-${mode}`, text: MODE_SHORT[mode] ?? mode });
}

/** 관심 단계·확인 상태 배지 (J5-029, ADR-22). 파생본 시드의 정본 출력값이며 폰에서 바꾸지 못한다. 미검토는 배지 없음. */
function trackingBadges(a) {
  const out = [];
  if (typeof a.tracking_status === "string" && a.tracking_status !== "unreviewed") {
    out.push(el("span", { class: `badge track-${a.tracking_status}` + (isWatchlist(a) ? " track-watchlist" : ""), text: TRACKING_LABEL[a.tracking_status] ?? a.tracking_status,
                          title: "관심 단계 (PC 정본에서 정함)" }));
  }
  if (a.resolution_status === "pending") out.push(el("span", { class: "badge src-pending", text: "PC 확인 전", title: "폰에서 만든 임시 대상입니다. PC 에서 확정합니다 (db asset-confirm)" }));
  return out;
}

/** 지금 적용되는 필터. 관심 단계 없는 시드(수동 시드·연습 자료)에서는 필터가 뜻이 없어 전체를 보인다. 저장된 선택(state.assetFilter)은 그대로 두어 파생본 시드가 돌아오면 다시 적용된다. */
function effectiveFilter() {
  return hasTracking(state.assets) ? state.assetFilter : "all";
}

/** 현재 필터(전체/관찰목록만)를 적용한 물건. 목록·지도·다음 대상에 같은 목록을 쓴다. */
function visibleAssets() {
  return filterAssets(state.assets, effectiveFilter());
}

/** 외부 지도 링크 (J5-021, ADR-15): 좌표가 있으면 "다른 지도에서 보기" 링크를 채우고, 없으면 숨긴다. 누르기 전에는 전송 없음. */
function renderExtLinks(container, point) {
  const links = externalMapLinks(point);
  container.hidden = links.length === 0;
  container.replaceChildren(...(links.length ? [el("span", { text: "다른 지도에서 보기" }),
    ...links.map((l) => el("a", { href: l.href, text: l.name, ...LINK_ATTRS, ...(l.verified ? {} : { title: "링크 형식을 아직 확인하지 않았습니다. 열리지 않으면 알려 주세요" }) }))] : []));
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k.startsWith("aria-")) node.setAttribute(k, v);
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
  if (!quiet) text($("tiles-note"), `저장했습니다: ${PROVIDERS[provider].name} · ${cfg.url}` + (cfg.needsKey ? ". 인증키는 이 폰에만 저장합니다" : ""), "ok");
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
  if (!bbox) return text($("tiles-prefetch-note"), "범위를 정할 대상·필지가 없습니다. 자료를 먼저 가져옵니다.", "warn");
  const plan = prefetchPlan(bbox, { minZoom: Math.max(PREFETCH_ZOOMS.min, state.tiles.minZoom), maxZoom: Math.min(PREFETCH_ZOOMS.max, state.tiles.maxZoom) });
  if (plan.tooMany) return text($("tiles-prefetch-note"), `범위가 넓어 타일이 ${plan.count}장을 넘습니다 (한도 ${plan.cap}). 대상 범위를 좁힙니다.`, "warn");
  if (!(await swControlled())) return text($("tiles-prefetch-note"), "아직 이 폰에 타일을 저장할 준비가 되지 않았습니다. https 주소로 열고 한 번 새로고침한 뒤 다시 누릅니다.", "warn");
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
  if (route && !isUuid(route)) return text($("settings-note"), "조사 경로 ID 는 PC 에서 받은 UUID 그대로 넣습니다", "bad");
  await state.store.setMeta("study_id", studyId);
  await state.store.setMeta("data_mode", $("data-mode").value);
  await state.store.setMeta("route_version_id", route || null);
  text($("settings-note"), "저장했습니다", "ok");
  renderModeBadge($("data-mode").value);
  await renderMigrate(await state.store.listEvents());  // 설정(study_id·모드)이 바뀌면 '현재 설정' 기준이 바뀐다
  await refreshStatus();
}

// ---- 물건 목록 (시드) ----
async function loadSeedObject(seed, source) {
  // 스키마 전체 규칙으로 검증한다. 시드는 통째로 교체하며 저장된 관측은 건드리지 않는다.
  const errs = validateSeed(seed);
  if (errs.length) return text($("seed-note"), "대상 목록 파일을 넣지 못했습니다: " + errs.slice(0, 5).join("; ") + (errs.length > 5 ? ` 외 ${errs.length - 5}건` : ""), "bad");
  await state.store.replaceAssets(seed, source);
  const from = source === "bundled_synthetic" ? "연습용" : source.replace(/^file:/, "");
  await renderAssets();
  await renderRecords();
  // 안내는 목록·기록을 다 그린 뒤에 쓴다 (이 문구가 보이면 화면이 새 목록이다)
  text($("seed-note"), `대상 ${seed.length}곳을 가져왔습니다 (${from}). 이전 목록은 바뀌었고 저장한 기록은 그대로입니다.`, "ok");
}

async function loadSyntheticSeed() {
  try {
    const res = await fetch("data/assets.seed.synthetic.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadSeedObject(await res.json(), "bundled_synthetic");
  } catch (e) {
    text($("seed-note"), "연습용 대상 목록을 읽지 못했습니다: " + e, "bad");
  }
}

/** 시작하기의 '연습용 자료로 둘러보기': 연습용 대상·필지·배경을 함께 넣어 지도가 바로 보이게 한다. 이미 넣은 필지·배경 파일이 있으면 그대로 둔다 */
async function loadSyntheticAll() {
  await loadSyntheticSeed();
  if (!state.parcelsRec) await loadSyntheticParcels();
  if (!state.basemapRec) await loadSyntheticBasemap();
  text($("home-note"), $("seed-note").textContent, $("seed-note").className);
  showFieldAfterLoad();
}

async function loadSeedFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    await loadSeedObject(JSON.parse(await file.text()), "file:" + file.name);
  } catch (e) {
    text($("seed-note"), "대상 목록 파일을 읽지 못했습니다: " + e, "bad");
  }
  input.value = "";
}

function assetSub(a) {
  return a.address ? a.address : a.location_point ? "지도에 위치 있음" : "위치 없음, 목록에서 고릅니다";
}

async function renderAssets() {
  state.assets = await state.store.listAssets();
  state.events = await state.store.listEvents();
  state.seedLoadedAt = (await state.store.getMeta("seed_loaded_at")) ?? null;
  state.seedSource = (await state.store.getMeta("seed_source")) ?? null;
  if (state.parcelsRec) parcelsNote(state.parcelsRec);
  applyAssetFilterUi();
  const visible = visibleAssets();
  const list = $("asset-list");
  // 한 줄 = 대상 하나 (J5-054). 줄을 누르면 지도에서 고르고(아래 카드), 오른쪽 "기록" 은 바로 기록하기를 연다. 버튼 안에 버튼을 두지 않으려고 이력·삭제는 줄 아래(row-extra)에 둔다
  list.replaceChildren(...visible.map((a) => {
    const s = assetSummary(a.asset_id, state.events);
    const meta = el("span", { class: "meta" });
    if (s.count) {
      meta.append(el("span", { class: `badge status-${s.lastStatus}`, text: CHANGE_STATUS_LABEL[s.lastStatus] ?? s.lastStatus }),
                  el("span", { text: shortWhen(s.lastAt) }), el("span", { text: `기록 ${s.count}건` }));
      if (s.photos) meta.append(el("span", { text: `사진 ${s.photos}장` }));
      if (s.unexported) meta.append(el("span", { class: "unsent", text: `안 보냄 ${s.unexported}` }));
    } else {
      meta.append(el("span", { text: "아직 기록 없음" }));
    }
    const extra = el("div", { class: "row-extra" });
    if (s.count || state.view) extra.append(el("button", { class: "quiet small", text: "이력", onclick: () => openAssetHistory(a) }));
    extra.append(...trackingBadges(a));
    if (a.origin === "device") {
      extra.append(el("span", { class: "badge src-device", text: "이 폰에서 만듦" }));
      if (!s.count) extra.append(el("button", { class: "quiet small", text: "삭제", onclick: () => deleteDeviceAsset(a) }));
    }
    const li = el("li", { class: state.mapSelected === a.asset_id ? "selected" : "" },
      el("button", { class: "row-main", onclick: () => selectOnMap(a) },
        el("span", { class: "title", text: a.label }),
        el("span", { class: "sub" }, modeBadge(a.data_mode), document.createTextNode(" " + assetSub(a))),
        meta),
      el("button", { class: "row-act secondary", text: "기록", "aria-label": `${a.label} 기록하기`, onclick: () => startObservation(a) }),
    );
    if (extra.childNodes.length) li.append(extra);
    li.dataset.assetId = a.asset_id;
    return li;
  }));
  if (!state.assets.length) list.append(el("li", { class: "empty", text: "아직 대상이 없습니다. 위의 시작하기에서 자료를 가져옵니다." }));
  else if (!visible.length) list.append(el("li", { class: "empty", text: "관찰목록에 든 대상이 없습니다. 관심 단계는 PC 에서 정합니다. '전체' 를 누르면 모두 보입니다." }));
  // 목록을 먼저 채운 뒤 지도를 갱신한다. 지도 실패는 목록에 영향을 주지 않는다.
  updateMapInset();   // 목록이 바뀌어 시트 높이가 달라졌을 수 있다: 맞추기 전에 덮인 영역을 다시 잰다
  mapCall((m) => mapNote(m.setAssets(visible)));
  // 조건 찾기 결과는 물건 목록에 따라 달라진다(물건 조건·결과 줄의 물건 수): 켜져 있으면 마지막으로 찾은 조건으로 다시 그린다 (리뷰 반영 PR #83)
  if (state.parcelFilter) renderParcelFilter(state.parcelFilter);
  // 지도 선택 카드는 현재 목록의 물건 객체에 다시 묶는다. 목록 교체·필터로 사라진 물건이면 카드를 닫는다 (리뷰 반영: 옛 물건으로 기록되지 않게)
  const selected = state.mapSelected ? visible.find((a) => a.asset_id === state.mapSelected) : null;
  if (state.mapSelected && !selected) clearMapSelection();
  else if (selected) selectOnMap(selected, { focus: false });
  renderSeedStatus();
  await refreshStatus();
}

function renderSeedStatus() {
  const n = state.assets.length;
  const when = shortWhen(state.seedLoadedAt);
  const watch = state.assets.filter(isWatchlist).length;
  text($("seed-status"), n ? `대상 ${n}곳 (${MODE_SHORT[state.assets[0].data_mode] ?? ""})${hasTracking(state.assets) ? `, 관찰목록 ${watch}곳` : ""}. ${state.seedSource ? `${sourceText(state.seedSource)}, ` : ""}${when ?? "시각 모름"} 가져옴.` : "가져온 대상 목록이 없습니다.");
}

// ---- 관찰목록 필터 (J5-029, ADR-22): 파생본 시드의 관심 단계로 '전체/관찰목록만' 을 고른다. 선택은 이 기기(meta asset_filter)에 남는다. 관심 단계 자체는 PC 에서만 바꾼다 ----
function applyAssetFilterUi() {
  const bar = $("asset-filter");
  const tracked = hasTracking(state.assets);
  const filter = effectiveFilter();
  bar.hidden = !tracked;
  for (const b of bar.querySelectorAll("button[data-filter]")) b.setAttribute("aria-pressed", String(b.dataset.filter === filter));
  const watch = state.assets.filter(isWatchlist).length;
  text($("asset-filter-note"), tracked ? (filter === "watchlist" ? `관찰목록 ${watch}곳만 보는 중 (전체 ${state.assets.length}곳)` : `관찰목록 ${watch}곳`) : "", "pencil");
}

async function setAssetFilter(filter) {
  if (!ASSET_FILTERS.includes(filter) || filter === state.assetFilter) return;
  state.assetFilter = filter;
  await state.store.setMeta("asset_filter", filter);
  await renderAssets();
}

// ---- 필지 (J5-013B-1, ADR-13) ----
// 필지 번들은 지도의 보조 층이다. 없거나 실패해도 목록·기록·내보내기는 그대로 동작한다.
async function loadParcelsObject(doc, source) {
  const errs = validateParcels(doc);
  if (errs.length) return text($("parcels-note"), "필지 파일을 넣지 못했습니다: " + errs.slice(0, 5).join("; ") + (errs.length > 5 ? ` 외 ${errs.length - 5}건` : ""), "bad");
  // 파생본의 필지 파일은 정본의 study_id 를 담는다. 설정과 다르면 다른 정본의 연결이므로 넣지 않는다.
  const studyId = await state.store.getMeta("study_id");
  if (doc.study_id && studyId && doc.study_id !== studyId) {
    return text($("parcels-note"), `필지 파일의 작업 공간(${doc.study_id})이 설정(${studyId})과 다릅니다. 같은 작업 공간에서 만든 파일을 넣습니다`, "bad");
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
  updateMapInset();
  mapCall((m) => m.setParcels(state.parcels));
  applyZoneColors();
  applySearchUi();
  parcelsNote(rec);
  updateMapInset();
  mapCall((m) => mapNote(m.setAssets(visibleAssets())));
}

// ---- 용도지역 색 (J5-025, ADR-19): 필지 속성이 있을 때만 켤 수 있고, 켬 여부는 이 기기에 저장한다 ----
// ---- 필지 찾기 (J5-027): 지번·PNU 검색과 현재 위치. 불러온 필지 안에서만 찾고, 위치는 누를 때 한 번 읽어 그리기만 한다 (저장·전송 없음) ----
function applySearchUi() {
  const has = state.parcelsCount > 0;
  $("parcel-search-form").hidden = !has;
  $("map-locate").disabled = !("geolocation" in navigator);
  $("map-locate").hidden = !("geolocation" in navigator);
  if (!has) { $("parcel-search-results").replaceChildren(); $("parcel-search-results").hidden = true; text($("parcel-search-note"), "", "muted"); }
  applyFilterUi();
}

// ---- 조건으로 필지 찾기 (J5-033): 불러온 필지의 속성으로 거르고 지도에 테두리로 표시한다. 필지 속성이 있는 파일에서만 보인다 ----
function applyFilterUi() {
  const feats = state.parcels?.features ?? [];
  const withAttrs = feats.some((f) => f.properties?.attrs);
  $("parcel-filter").hidden = !withAttrs;
  // 번들이 바뀌면 이전 결과는 다른 자료의 것이다: 목록·안내·지도 강조를 지운다 (지도 쪽 강조는 setParcels 가 지운다)
  state.parcelFilter = null;
  $("parcel-filter-results").replaceChildren();
  $("parcel-filter-results").hidden = true;
  text($("parcel-filter-note"), "", "muted");
  const zone = $("pf-zone"), owner = $("pf-owner");
  const keepZone = zone.value, keepOwner = owner.value;
  zone.replaceChildren(el("option", { value: "", text: "전체" }), ...Object.entries(ZONE_LABELS).map(([k, v]) => el("option", { value: k, text: v })));
  owner.replaceChildren(el("option", { value: "", text: "전체" }), ...ownershipKinds(feats).map(({ kind, count }) => el("option", { value: kind, text: `${kind} (${count})` })));
  if ([...zone.options].some((o) => o.value === keepZone)) zone.value = keepZone;
  if ([...owner.options].some((o) => o.value === keepOwner)) owner.value = keepOwner;
}

function runParcelFilter() {
  const list = $("parcel-filter-results");
  list.replaceChildren();
  list.hidden = true;
  state.parcelFilter = null;
  const { criteria, errors } = parseFilter({ zone: $("pf-zone").value, areaMin: $("pf-area-min").value, areaMax: $("pf-area-max").value,
    priceMin: $("pf-price-min").value, priceMax: $("pf-price-max").value, owner: $("pf-owner").value, restricted: $("pf-restricted").checked, assets: $("pf-assets").value });
  if (errors.length) { mapCall((m) => m.setMarked(null)); return text($("parcel-filter-note"), "조건 확인: " + errors.join(" · "), "warn"); }
  if (!hasCriteria(criteria)) { mapCall((m) => m.setMarked(null)); return text($("parcel-filter-note"), "조건을 하나 이상 고릅니다.", "warn"); }
  state.parcelFilter = criteria;
  renderParcelFilter(criteria);
}

/** 조건 찾기 결과를 그린다. 물건 목록이 바뀌면(기기 물건 만들기·삭제, 시드 교체) renderAssets 가 마지막으로 찾은 조건으로 다시 부른다:
 *  물건 조건·결과 줄의 물건 수가 옛 목록의 것으로 남지 않게 (리뷰 반영 PR #83). 폼에서 고치고 아직 찾지 않은 값은 쓰지 않는다. */
function renderParcelFilter(criteria) {
  const feats = state.parcels?.features ?? [];
  const list = $("parcel-filter-results");
  list.replaceChildren();
  list.hidden = true;
  // 필지 안의 물건 (J5-036): 필지 패널과 같은 규칙(정본 연결은 시드와 필지 파일이 같은 파생본일 때만 + 위치점 포함). 필지마다 한 번만 계산한다
  const linksValid = parcelLinksValid();
  const cache = new Map();
  const assetsOf = (f) => {
    if (!cache.has(f.id)) cache.set(f.id, parcelAssets(f, state.assets, { linksValid }).map((x) => x.asset));
    return cache.get(f.id);
  };
  const r = filterParcels(feats, criteria, { assetsOf });
  const shown = mapCall((m) => m.setMarked(r.matches.length ? filterParcels(feats, criteria, { limit: Infinity, assetsOf }).matches.map((f) => f.id) : null));
  const unk = Object.entries({ zone: "용도지역", area: "공부면적", price: "공시지가", owner: "소유구분", plan: "규제" }).filter(([k]) => r.unknown[k]).map(([k, v]) => `${v} ${r.unknown[k]}`);
  const tail = (unk.length ? ` · 값 없는 필지: ${unk.join(", ")}` : "") + (r.undetermined ? ` · 값이 없어 맞는지 모르는 필지 ${r.undetermined}개는 결과에서 뺌` : "");
  if (!r.total) return text($("parcel-filter-note"), `속성 있는 필지 ${r.withAttrs}개 중 맞는 필지가 없습니다${tail}.`, "warn");
  list.replaceChildren(...r.matches.map((f) => el("li", {}, el("span", { class: "title", text: parcelTitle(f.properties) }), el("span", { class: "sub", text: filterRowText(f.properties.attrs, assetsOf(f)) }),
    el("button", { class: "secondary", text: "열기", onclick: () => openParcel(f) }))));
  list.hidden = false;
  text($("parcel-filter-note"), `속성 있는 필지 ${r.withAttrs}개 중 ${r.total}개 일치` + (r.total > r.matches.length ? ` (공부면적 큰 순 앞 ${FILTER_LIMIT}개만 목록)` : " (공부면적 큰 순)") +
    (shown != null ? ` · 지도에 테두리로 표시` : "") + tail, "ok");
}

function clearParcelFilter() {
  state.parcelFilter = null;
  $("parcel-filter-form").reset();
  $("parcel-filter-results").replaceChildren();
  $("parcel-filter-results").hidden = true;
  text($("parcel-filter-note"), "", "muted");
  mapCall((m) => m.setMarked(null));
}

function openParcel(feature, { focus = true, move = true } = {}) {
  showParcel(feature);   // 시트를 먼저 펼쳐야 필지 맞추기가 시트에 덮이지 않는 곳에 그린다
  if (move) mapCall((m) => m.focusParcel(feature.id));
  if (focus) $("parcel-history").focus({ preventScroll: true });
}

function searchParcels(q) {
  const feats = state.parcels?.features ?? [];
  const list = $("parcel-search-results");
  list.replaceChildren();
  list.hidden = true;
  openSheet();
  if (!feats.length) return text($("parcel-search-note"), "필지 자료가 없어 찾을 수 없습니다. 설정에서 PC 자료 파일을 가져옵니다.", "warn");
  const r = findParcels(feats, q);
  if (!r.query) return text($("parcel-search-note"), "지번(예: 182-13, 산1-2, 예지동 182-13)이나 필지 번호 19자리를 넣습니다.", "warn");
  if (!r.matches.length) return text($("parcel-search-note"), `'${String(q).trim()}' 필지가 가져온 ${feats.length}개 안에 없습니다. 동 이름과 지번을 확인하거나, PC 에서 범위를 넓혀 자료 파일을 다시 만듭니다.`, "warn");
  if (r.matches.length === 1) {
    openParcel(r.matches[0]);
    return text($("parcel-search-note"), "", "note");
  }
  list.replaceChildren(...r.matches.map((f) => el("li", {}, el("span", { class: "title", text: parcelTitle(f.properties) }), el("span", { class: "sub num", text: f.id }),
    el("button", { class: "secondary", text: "열기", onclick: () => { openParcel(f); list.hidden = true; } }))));
  list.hidden = false;
  text($("parcel-search-note"), `${r.total}개 찾음${r.total > r.matches.length ? ` (앞 ${r.matches.length}개만 보입니다)` : ""}. 하나를 고릅니다.`, "pencil");
}

function locateMe() {
  if (!("geolocation" in navigator)) return text($("parcel-search-note"), "이 브라우저에서는 위치를 읽을 수 없습니다.", "warn");
  openSheet();
  if (!window.isSecureContext) return text($("parcel-search-note"), "위치는 https 주소로 연 앱에서만 읽을 수 있습니다.", "warn");
  const btn = $("map-locate");
  btn.disabled = true;
  text($("parcel-search-note"), "현재 위치를 읽는 중…", "pencil");
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
      text($("parcel-search-note"), `지금 ${parcelTitle(f.properties)} 필지 안에 있습니다${acc}. 위치는 저장하지 않습니다.${tileNote}`, "ok");
    } else {
      text($("parcel-search-note"), (state.parcelsCount ? `지금 위치${acc}는 가져온 필지 범위 밖입니다. 위치는 저장하지 않습니다.` : `지금 위치를 표시했습니다${acc}. 필지 자료가 있으면 그 자리의 필지를 엽니다.`) + tileNote, "pencil");
    }
  }, (err) => {
    btn.disabled = false;
    const why = err.code === 1 ? "위치 권한이 꺼져 있습니다. 브라우저 설정에서 이 사이트의 위치를 허용합니다" : err.code === 2 ? "위치를 구하지 못했습니다. 실내이거나 기내 모드인지 확인합니다" : "위치를 읽는 데 시간이 너무 걸렸습니다. 다시 누릅니다";
    text($("parcel-search-note"), why + ".", "warn");
  }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
}

function applyZoneColors() {
  const sm = attrsSummary(state.parcels);
  const btn = $("map-zones");
  btn.disabled = !sm.withAttrs;
  btn.hidden = !sm.withAttrs;   // 토지 자료가 든 필지 파일일 때만 보인다
  btn.setAttribute("aria-pressed", state.zoneColors && sm.withAttrs ? "true" : "false");
  btn.textContent = "용도지역";
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
  if (!rec) return text($("parcels-note"), "필지 자료 없음. 지도에는 대상 위치만 보입니다.", "pencil");
  // 문장 단위로 (J5-056): 무엇이 몇 개, 어디서 온 자료, 언제 가져왔는지. 좌표계·이용허락·정본 버전은 확인용으로 남긴다
  const b = rec.bundle, s = b.source;
  const crs = s.crs?.epsg ? `EPSG:${s.crs.epsg}` : (s.crs?.name ?? "모름");
  const hasLinks = b.features.some((f) => (f.properties.asset_ids ?? []).length);
  const linkNote = hasLinks ? (parcelLinksValid() ? " PC 에서 이은 대상 포함." : " PC 에서 이은 대상은 대상 목록이 바뀐 뒤라 보이지 않습니다. PC 자료 파일을 다시 가져오면 보입니다.") : "";
  const when = shortWhen(rec.loaded_at);
  const sm = attrsSummary(b);
  const attrsNote = sm.withAttrs ? ` 토지 자료가 든 필지 ${sm.withAttrs}개 (${(b.attrs_sources ?? []).map((a) => a.name).join(", ") || "출처 적혀 있지 않음"}).` : "";
  text($("parcels-note"), `필지 ${b.features.length}개 (${DATA_MODE_TEXT[b.data_mode] ?? b.data_mode}). ${s.name}, 도형 기준일 ${s.geometry_version}, 좌표계 ${crs}, 이용허락 ${s.license ?? "미확인"}.` + attrsNote +
    (b.source_dataset_version != null ? ` 정본 v${b.source_dataset_version}.` : " 정본 버전이 적혀 있지 않아 오래된 파일인지 알 수 없습니다.") + ` ${sourceText(rec.source)}` + (when ? `, ${when} 가져옴.` : ".") +
    warningsNote(b.warnings) + linkNote, b.data_mode === "synthetic" ? "pencil" : "ok");
}

/** 번들 경고 안내. PC 가 폰 범위로 필지 일부만 실었다는 경고(J5-039, "폰 범위:")는 글 그대로 보인다: 폰에 없는 필지를 없는 필지로 오해하지 않게 */
function warningsNote(warnings) {
  const ws = Array.isArray(warnings) ? warnings : [];
  const scope = ws.filter((w) => typeof w === "string" && w.startsWith("폰 범위:"));
  const rest = ws.length - scope.length;
  return scope.map((w) => ` ${w}.`).join("") + (rest ? ` 변환 경고 ${rest}건 (PC 의 변환 기록에서 봅니다).` : "");
}

async function loadSyntheticParcels() {
  try {
    const res = await fetch("data/parcels.synthetic.j5parcels.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadParcelsObject(await res.json(), "bundled_synthetic");
  } catch (e) {
    text($("parcels-note"), "연습용 필지를 읽지 못했습니다: " + e, "bad");
  }
}

async function loadParcelsFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    await loadParcelsObject(JSON.parse(await file.text()), "file:" + file.name);
  } catch (e) {
    text($("parcels-note"), "필지 파일을 읽지 못했습니다: " + e, "bad");
  }
  input.value = "";
}

async function clearParcels() {
  await state.store.clearParcels();
  applyParcels(null);
}

// ---- PC 자료 파일 (조회 파생본 .j5view.zip, J5-023, ADR-17) ----
// 물건 목록·필지·정본 기록·거래를 한 번에 넣는다. 사진은 넣지 않는다. 이 기기의 관측·사진은 건드리지 않는다. 최신 여부는 파일만으로 모른다.
async function loadViewFile(input, note = $("view-note")) {
  const file = input.files?.[0];
  if (!file) return;
  input.value = "";
  // 첫 화면(시작하기)과 설정 두 곳에서 부른다. 안내는 누른 자리 옆(note)에 쓰고, 설정의 안내도 같은 글로 맞춘다
  const say = (msg, cls) => { text(note, msg, cls); if (note !== $("view-note")) text($("view-note"), msg, cls); };
  if (file.size > VIEW_LIMITS.file) return say(`파일이 너무 큽니다 (${fmtBytes(file.size)}, 한도 ${fmtBytes(VIEW_LIMITS.file)}). PC 에서 다시 만듭니다`, "bad");
  say("읽는 중…", "pencil");
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const { manifest, files, photosSkipped } = await readViewZip(bytes);
    const [studyId, dataMode] = await Promise.all([state.store.getMeta("study_id"), state.store.getMeta("data_mode")]);
    const errs = validateViewManifest(manifest, { studyId: studyId || null, dataMode: dataMode ?? null });
    if (errs.length) return say("PC 자료 파일을 넣지 못했습니다: " + errs.slice(0, 5).join("; "), "bad");
    const seed = JSON.parse(decodeText(files.get("assets.seed.json")));
    const seedErrs = validateSeed(seed);
    if (seedErrs.length) return say("파일 안의 대상 목록이 올바르지 않습니다: " + seedErrs.slice(0, 5).join("; "), "bad");
    const parcels = files.has("parcels.geojson") ? JSON.parse(decodeText(files.get("parcels.geojson"))) : null;
    if (parcels) {
      const pe = validateParcels(parcels);
      if (pe.length) return say("파일 안의 필지가 올바르지 않습니다: " + pe.slice(0, 5).join("; "), "bad");
    }
    const records = parseRecordsJsonl(decodeText(files.get("records.jsonl")));
    const transactions = files.has("transactions.json") ? JSON.parse(decodeText(files.get("transactions.json"))) : null;
    if (transactions) {
      const te = validateTransactionsDoc(transactions);
      if (te.length) return say("파일 안의 거래 목록이 올바르지 않습니다: " + te.slice(0, 5).join("; "), "bad");
    }
    // 파일들이 같은 정본 버전에서 나왔는지 manifest 와 대조한다 (리뷰 반영: 다른 정본의 거래·필지가 섞여 들어오지 않게)
    const ce = checkProjectionConsistency(manifest, { transactions, parcels });
    if (ce.length) return say("PC 자료 파일을 넣지 못했습니다: " + ce.slice(0, 5).join("; "), "bad");
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
    say(`PC 자료를 가져왔습니다. 대상 ${seed.length}곳, 필지 ${parcels ? parcels.count : 0}개, PC 기록 ${records.length}건, 거래 ${sm.transactions}건 (정본 v${sm.version})` +
      (photosSkipped ? `. 사진 ${photosSkipped}장은 넣지 않았습니다` : "") + ". 이 폰의 기록은 그대로입니다.", "ok");
  } catch (e) {
    say((e instanceof UnzipError ? `PC 자료 파일을 읽지 못했습니다 [${e.code}]: ` : "PC 자료 파일을 읽지 못했습니다: ") + (e?.message || e), "bad");
  }
}

function renderViewStatus() {
  const sm = viewSummary(state.view);
  if (!sm) return text($("view-status"), "아직 가져오지 않았습니다. PC 기록과 거래 이력은 이 파일을 가져오면 보입니다.");
  const gen = sm.generatedAt ? shortWhen(sm.generatedAt) ?? sm.generatedAt : "?";
  text($("view-status"), `PC 에서 ${gen} 에 만든 자료 (정본 v${sm.version}). PC 기록 ${sm.records}건, 거래 ${sm.transactions}건. ${shortWhen(sm.loadedAt) ?? "?"} 에 가져왔습니다. 더 새 자료가 있는지는 PC 에서 확인합니다.`);
}

async function clearView() {
  await state.store.clearView();
  state.view = null;
  renderViewStatus();
  text($("view-note"), "PC 기록과 거래를 지웠습니다. 대상 목록, 필지, 이 폰의 기록은 그대로입니다.", "pencil");
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
  head.append(el("span", { class: `badge ${it.source === "device" ? "src-device" : "src-pc"}`, text: it.source === "device" ? (it.deviceStatus === "exported" ? "이 폰 · 보냄" : "이 폰") : "PC" }));
  if (it.superseded) head.append(el("span", { class: "badge", text: "정정으로 대체됨" }));
  const li = el("li", { class: it.superseded ? "superseded" : "" }, el("span", { class: "tl-date", text: it.date || "날짜 미상" }), head, el("span", { class: "tl-text", text: it.text || "(내용 없음)" }));
  if (it.where) li.append(el("span", { class: "tl-where", text: it.where }));
  return li;
}

function openHistory({ title, sub, items, asset = null, years = [], yearsOmitted = null }) {
  if ($("sec-history").hidden) state.historyReturnFocus = document.activeElement;
  state.historyAsset = asset;
  $("history-title").textContent = title;
  $("history-sub").textContent = sub;
  const sm = viewSummary(state.view);
  text($("history-source"), sm ? `PC 에서 ${shortWhen(sm.generatedAt) ?? sm.generatedAt} 에 만든 자료(정본 v${sm.version})와 이 폰의 기록입니다.` : "PC 자료를 아직 가져오지 않아 이 폰의 기록만 보입니다.", "help");
  // 필지 연도별 요약 (J5-047, 표는 J5-055): 필지 이력에서만. 모르는 연도는 0 이 아니라 사유로 적는다
  renderYearsTable($("history-years-table"), years);
  $("history-years").hidden = !years.length;
  // 상한(40개 연도)보다 앞 연도는 폰에 보이지 않는다. 빠진 것을 알리고 PC 명령을 안내한다 (J5-051)
  const om = yearsOmitted;
  text($("history-years-more"), om ? `${om.from}~${om.to}년 가운데 자료가 있는 ${om.count}개 연도는 여기 보이지 않습니다. PC 에서 \`j5 db parcels-years\` 로 봅니다.` : "", "help");
  $("history-years-more").hidden = !om;
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
  openHistory({ title: asset.label, sub: `${assetSub(asset)}, 기록 ${items.length}건`, items, asset });
}

/** PC 자료 파일을 만든 해 (연도별 요약의 끝 연도, PC parcels-years 의 올해와 같은 뜻). 파일이 없으면 null */
function viewYear() {
  const g = state.view?.manifest?.generated_at;
  return typeof g === "string" && /^\d{4}-/.test(g) ? Number(g.slice(0, 4)) : null;
}

/** 필지의 이력 항목과 연도별 요약 (필지 카드의 표와 필지 이력이 같은 값을 쓴다) */
function parcelYears(feature, assetsInside) {
  const p = feature.properties;
  const items = parcelHistory(feature, assetsInside, historyData());
  const prices = priceTrend(p.attrs, p.attrs_history).map((q) => ({ year: q.year, price: q.price }));
  const { rows, omitted } = parcelYearSummaryInfo(feature, items, state.view?.transactions ?? null, prices, { ownership: ownershipChanges(p.attrs, p.attrs_history), thisYear: viewYear() });
  return { items, rows, omitted };
}

/** 연도별 요약 표 (J5-055). 줄마다 data-year·data-tx 와 이 폰의 요약 글 한 줄(title, yearSummaryRow). PC db parcels-years 와는 글이 아니라 칸의 값으로 대조한다(PC 글은 마지막 거래 금액·날짜 등을 더 적는다).
 *  limit 를 주면 최근 연도부터 그만큼만.
 *  모든 줄에 소유 칸이 없으면(토지소유 정보를 주지 않음) 소유 열을 감춘다 */
function renderYearsTable(table, rows, { limit = Infinity } = {}) {
  const shown = rows.slice(0, limit);
  const hasOwner = shown.some((r) => r.owner);
  table.classList.toggle("no-owner", !hasOwner);
  table.tBodies[0].replaceChildren(...shown.map((r) => {
    const c = yearSummaryCells(r);
    const priceTd = c.price == null ? el("td", { class: "none", text: "자료 없음" })
      : el("td", { class: "num" }, document.createTextNode(c.price), ...(c.delta ? [el("span", { class: "delta", text: c.delta })] : []));
    const txTd = el("td", { class: c.txCounted ? "num" : "none" }, document.createTextNode(c.tx), ...c.txNotes.map((n) => el("span", { class: "cell-note", text: n })));
    const tr = el("tr", { title: yearSummaryRow(r).join(" ") },
      el("th", { scope: "row", text: c.year }), priceTd, txTd,
      el("td", { class: `col-owner${c.owner === "없음" || c.owner === "자료 없음" ? " none" : " num"}`, text: c.owner ?? "" }));
    tr.dataset.year = c.year;
    tr.dataset.tx = r.tx;
    return tr;
  }));
}

/** 필지 카드의 최근 거래 (J5-057). PC 자료(거래 파일)가 없으면 감춘다: 거래가 없는 것인지 모으지 않은 것인지 이 폰에서 알 수 없다 */
function renderRecentDeals(items) {
  const hasTx = !!state.view?.transactions;
  $("parcel-deals").hidden = !hasTx;
  if (!hasTx) return;
  const d = recentDeals(items, PARCEL_CARD_DEALS);
  $("parcel-deals-list").replaceChildren(...d.rows.map((r) => el("li", {},
    el("span", { class: "deal-date num", text: r.date }),
    el("span", { class: "deal-main" }, el("strong", { class: "num", text: r.amount }),
      ...(r.match === "linked" ? [el("span", { class: "badge match-exact", text: "연결" })] : []),
      ...(r.details.length ? [el("span", { class: "deal-sub", text: r.details.join(", ") })] : [])))));
  $("parcel-deals-list").hidden = !d.rows.length;
  const notes = [];
  if (!d.rows.length) notes.push("PC 자료에 이 필지와 지번이 같은 거래가 없습니다. 거래를 모은 해는 위 연도별 표에서 봅니다.");
  else if (d.total > d.rows.length) notes.push(`이 필지 거래 ${d.total}건 가운데 최근 ${d.rows.length}건입니다.`);
  if (d.prefix) notes.push(`지번 일부가 가려진 번지대 거래 ${d.prefix}건은 이 필지의 거래로 확정하지 않아 넣지 않았습니다. 필지 이력에서 봅니다.`);
  text($("parcel-deals-note"), notes.join(" "), "pencil small");
  $("parcel-deals-note").hidden = !notes.length;
}

function openParcelHistory(feature, assetsInside) {
  const p = feature.properties;
  const { items, rows: years, omitted } = parcelYears(feature, assetsInside);
  openHistory({ title: `${parcelTitle(p)} 필지`, sub: `필지 번호 ${feature.id}. 대상 ${assetsInside.length}곳, 기록·거래 ${items.length}건`, items, asset: null, years, yearsOmitted: omitted });
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
  if (errs.length) return text($("basemap-note"), "배경 파일을 넣지 못했습니다: " + errs.slice(0, 5).join("; ") + (errs.length > 5 ? ` 외 ${errs.length - 5}건` : ""), "bad");
  await state.store.replaceBasemap(doc, source);
  applyBasemap(await state.store.getBasemap());
}

function applyBasemap(rec) {
  state.basemapRec = rec ?? null;
  state.basemapCount = rec?.bundle?.features.length ?? 0;
  updateMapInset();
  mapCall((m) => m.setBasemap(rec?.bundle ?? null));
  basemapNote(rec);
  updateMapInset();
  mapCall((m) => mapNote(m.setAssets(visibleAssets())));
}

function basemapNote(rec) {
  if (!rec) return text($("basemap-note"), "배경 자료 없음. 지도에는 대상 위치와 필지만 보입니다.", "pencil");
  const b = rec.bundle, s0 = b.sources[0];
  const parts = BASEMAP_LAYERS.filter((k) => b.counts[k] > 0).map((k) => `${BASEMAP_LAYER_LABEL[k]} ${b.counts[k]}`).join(", ");
  const when = shortWhen(rec.loaded_at);
  // 필지 상태와 같은 형식: 개수와 자료 종류, 자료 이름·기준일·이용허락, 가져온 자리와 시각 (리뷰 반영 PR #103)
  text($("basemap-note"), `건물·도로 윤곽 ${b.count}개 (${parts}), ${DATA_MODE_TEXT[b.data_mode] ?? b.data_mode}. ${s0.name}, 도형 기준일 ${s0.geometry_version}, 이용허락 ${s0.license ?? "미확인"}.` +
    ` ${sourceText(rec.source)}` + (when ? `, ${when} 가져옴.` : "."), b.data_mode === "synthetic" ? "pencil" : "ok");
}

async function loadSyntheticBasemap() {
  try {
    const res = await fetch("data/basemap.synthetic.j5basemap.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadBasemapObject(await res.json(), "bundled_synthetic");
  } catch (e) {
    text($("basemap-note"), "연습용 배경을 읽지 못했습니다: " + e, "bad");
  }
}

async function loadBasemapFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    await loadBasemapObject(JSON.parse(await file.text()), "file:" + file.name);
  } catch (e) {
    text($("basemap-note"), "배경 파일을 읽지 못했습니다: " + e, "bad");
  }
  input.value = "";
}

async function clearBasemap() {
  await state.store.clearBasemap();
  applyBasemap(null);
}

function showParcel(feature) {
  const p = feature.properties;
  // 시트 위 카드는 하나만 (J5-054): 필지를 열면 고른 대상 카드는 닫는다
  if (state.mapSelected) clearMapSelection();
  mapCall((m) => m.selectParcel(feature.id));
  $("parcel-title").textContent = parcelTitle(p);
  $("parcel-mode").textContent = state.parcels?.data_mode === "synthetic" ? "연습용 필지" : "필지";
  $("parcel-pnu").textContent = feature.id;
  const src = state.parcels?.source;
  $("parcel-meta").textContent = (p.jimok ? `지목 ${p.jimok}, ` : "") + `지도상 면적 ${fmtArea(p.area_m2_geom, p.area_missing_reason)} (대장 면적 아님)` + (p.jibun_mismatch ? ". 원본 지번과 필지 번호가 맞지 않습니다" : "") +
    (src ? `. 경계 ${src.name} ${src.geometry_version}` : "");
  renderExtLinks($("parcel-ext"), bboxCenter(p.bbox));
  const lines = attrLines(p.attrs, src?.geometry_version ?? null);
  $("parcel-attrs").replaceChildren(...lines.map(([k, v]) => el("li", {}, el("span", { class: "k", text: k }), el("span", { class: "v", text: v }))));
  $("parcel-attrs").hidden = !lines.length;
  $("parcel-attrs-note").hidden = !lines.length;
  // 공시지가 추이 (J5-030): 값이 바뀐 지점이 둘 이상일 때만 표로 보인다. 하나면 위 속성 목록의 공시지가가 전부다
  const trend = priceTrend(p.attrs, p.attrs_history);
  $("parcel-price").hidden = trend.length < 2;
  $("parcel-price-list").replaceChildren(...(trend.length < 2 ? [] : trend.map((q) => {
    const [base, value, delta, seen] = priceTrendRow(q);
    return el("li", {}, el("span", { class: "k", text: base }),
      el("span", { class: "v" }, el("strong", { text: value }), document.createTextNode(delta ? ` (${delta})` : ""), el("span", { class: "muted", text: ` · ${seen}` })));
  })));
  const linksValid = parcelLinksValid();
  const inside = parcelAssets(feature, state.assets, { linksValid });
  $("parcel-assets").replaceChildren(...inside.map(({ asset: a, basis }) => el("li", {},
    el("span", { class: "title", text: a.label }), modeBadge(a.data_mode), ...trackingBadges(a), el("span", { class: "badge", text: BASIS_LABEL[basis] }),
    el("button", { class: "secondary", text: "기록", "aria-label": `${a.label} 기록하기`, onclick: () => startObservation(a) }),
  )));
  if (!linksValid && (p.asset_ids ?? []).length) $("parcel-assets").append(el("li", { class: "warn", text: `PC 에서 이 필지에 이은 대상 ${p.asset_ids.length}곳은 대상 목록이 바뀐 뒤라 보이지 않습니다. PC 자료 파일을 다시 가져오면 보입니다.` }));
  if (!inside.length) $("parcel-assets").append(el("li", { class: "empty", text: "이 필지에 든 대상이 없습니다. 아래 버튼으로 이 필지를 대상으로 만들어 바로 기록할 수 있습니다." }));
  // 연도별 표 (J5-055): 최근 5개 연도. 전체와 기록·거래는 필지 이력에서
  const { rows: years, items: parcelItems } = parcelYears(feature, inside.map((x) => x.asset));
  renderRecentDeals(parcelItems);
  renderYearsTable($("parcel-years-table"), years, { limit: PARCEL_CARD_YEARS });
  $("parcel-years").hidden = !years.length;
  $("parcel-years-all").textContent = years.length > PARCEL_CARD_YEARS ? `모든 연도(${years.length}개)와 기록 보기` : "기록·거래 보기";
  $("parcel-years-all").onclick = () => openParcelHistory(feature, inside.map((x) => x.asset));
  $("parcel-new-asset").hidden = inside.length > 0;
  $("parcel-new-asset").onclick = () => createAssetFromParcel(feature);
  $("parcel-history").onclick = () => openParcelHistory(feature, inside.map((x) => x.asset));
  $("parcel-panel").hidden = false;
  openSheet();
  $("field-sheet").querySelector(".sheet-body").scrollTop = 0;
}

/** 필지에서 물건 만들기 (J5-028, ADR-21): 이 기기에만 있는 임시 매입 단위. 위치점은 필지 안의 점, 이름은 동·지번. 내보내기에 assets.new.json 으로 실려 PC 가 pending 물건으로 만든다. */
async function createAssetFromParcel(feature) {
  const pt = interiorPoint(feature);
  if (!pt) return text($("parcel-search-note"), "이 필지 안의 점을 정하지 못해 대상을 만들 수 없습니다.", "warn");
  const dataMode = (await state.store.getMeta("data_mode")) ?? "synthetic";
  const asset = { asset_id: uuid4(), label: parcelTitle(feature.properties), location_point: pt, data_mode: dataMode, created_at: isoWithOffset(), notes: null, pnu: feature.id, origin: "device" };
  try {
    await state.store.addDeviceAsset(asset);
  } catch (e) {
    return text($("parcel-search-note"), "대상을 저장하지 못했습니다: " + (e?.message || e), "bad");
  }
  await renderAssets();
  closeParcelPanel();
  text($("parcel-search-note"), `${asset.label} 을 이 폰의 대상으로 만들었습니다. PC 로 보내면 PC 에서 확인 전 대상으로 들어갑니다.`, "ok");
  startObservation(asset);
}

async function deleteDeviceAsset(asset) {
  try {
    await state.store.deleteDeviceAsset(asset.asset_id);
    text($("seed-note"), `이 폰에서 만든 대상 ${asset.label} 을 지웠습니다.`, "pencil");
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
  $("map-empty").replaceChildren(el("div", {}, el("strong", { text: "지도를 그릴 수 없습니다" }), document.createTextNode("아래 목록에서 대상을 골라 기록할 수 있습니다.")));
  text($("map-note"), `지도를 그리지 못했습니다 (${e?.message || e}). 목록에서 고릅니다.`, "warn");
  openSheet();
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
  // 지도 아래 한 줄 요약 (J5-054): 지도에 무엇이 그려졌는지와 지도에 없는 대상 수. 문장 단위로 끊어 가운뎃점 줄이 길게 이어지지 않게 한다
  const tileNote = state.tiles ? (ts?.failed ? ` 배경 타일 ${ts.failed}장을 받지 못했습니다 (${navigator.onLine ? "제공자 응답 없음, 키·주소를 확인합니다" : "오프라인이라 저장된 타일만 보입니다"}).` : " 배경 타일 켜짐.") : "";
  const layers = [state.parcelsCount ? `필지 ${state.parcelsCount}개` : "", state.basemapCount ? `건물·도로 ${state.basemapCount}개` : ""].filter(Boolean).join(", ");
  const parcels = (layers ? ` ${layers}.` : "") + tileNote;
  if (c.total === 0) return text($("map-note"), parcels ? `대상 없음.${parcels}` : "", "pencil small");
  if (c.located === 0) return text($("map-note"), `지도에 위치가 있는 대상이 없습니다. 목록에서 고릅니다.${parcels}`, "pencil small");
  const kinds = [c.synthetic ? `연습용 ${c.synthetic}` : "", c.privateReal ? `실제 ${c.privateReal}` : ""].filter(Boolean).join(", ");
  let msg = `지도에 대상 ${c.located}곳 (${kinds}).`;
  if (c.unlocated > 0) msg += ` 위치 없는 대상 ${c.unlocated}곳은 목록에만 있습니다.`;
  text($("map-note"), msg + parcels, "pencil small");
}

/** 지도의 점이나 목록 줄을 누르면 바로 기록 화면을 열지 않고 시트 위에 대상 카드와 '기록하기' 를 보여 준다. */
function selectOnMap(asset, { focus = true } = {}) {
  state.mapSelected = asset.asset_id;
  mapCall((m) => m.select(asset.asset_id));
  const s = assetSummary(asset.asset_id, state.events);
  $("map-selected-label").textContent = asset.label;
  $("map-selected-sub").textContent = assetSub(asset) + (s.count ? `. 마지막 기록 ${shortWhen(s.lastAt)}, ${CHANGE_STATUS_LABEL[s.lastStatus] ?? ""}` : ". 아직 기록 없음");
  $("map-selected-badges").replaceChildren(...trackingBadges(asset));
  renderExtLinks($("map-selected-ext"), asset.location_point);
  $("map-selected").hidden = false;
  $("start-observing").hidden = true;
  $("map-selected-start").onclick = () => startObservation(asset);
  $("map-selected-history").onclick = () => openAssetHistory(asset);
  for (const li of $("asset-list").children) li.classList.toggle("selected", li.dataset.assetId === asset.asset_id);
  if (focus) { if (!$("parcel-panel").hidden) closeParcelPanel(); openSheet(); $("field-sheet").querySelector(".sheet-body").scrollTop = 0; mapCall((m) => m.reveal(asset.asset_id)); $("map-selected-start").focus(); }
}

function clearMapSelection() {
  state.mapSelected = null;
  $("map-selected").hidden = true;
  $("start-observing").hidden = state.assets.length === 0;
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
  $("target-sub").textContent = assetSub(asset) + (s.count ? `. 마지막 기록 ${shortWhen(s.lastAt)}` : ". 첫 기록");
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
    if (ext === "heic") item.error = "HEIC 사진은 넣을 수 없습니다. 카메라 설정의 '높은 호환성' 으로 찍거나 JPEG 로 바꿔 다시 고릅니다";
    else if (!SUPPORTED.has(ext)) item.error = "JPEG·PNG·WebP 사진만 넣을 수 있습니다";
    else if (file.size > PHOTO_LIMIT) item.error = `20MB 를 넘는 사진입니다 (${fmtBytes(file.size)})`;
    if (!item.error) {
      const buf = await file.arrayBuffer();
      item.sha256 = await sha256Hex(buf);
      item.blob = new Blob([buf], { type: file.type || "application/octet-stream" });
      if (state.photos.some((p) => p.sha256 === item.sha256)) item.error = "같은 사진이 이미 선택됨. 이 항목을 뺍니다";
    }
    state.photos.push(item);
  }
  renderPhotos();
  input.value = "";
}

function renderPhotos() {
  $("photo-list").replaceChildren(...state.photos.map((p, idx) => {
    const box = el("div", { class: "photo-item" });
    box.append(el("div", { class: "photo-name", text: `${p.name} (${fmtBytes(p.bytes)})` }));
    if (p.error) box.append(el("div", { class: "bad", text: p.error }));
    else {
      const tags = el("div", { class: "tags" });
      for (const t of PHOTO_TAGS) {
        const box2 = el("input", { type: "checkbox", checked: p.tags.has(t) });
        box2.addEventListener("change", () => { box2.checked ? p.tags.add(t) : p.tags.delete(t); });
        tags.append(el("label", {}, box2, ` ${PHOTO_TAG_LABEL[t]}`));
      }
      box.append(el("div", { class: "field-name", text: "무엇을 찍었나요" }), tags);
      // 반복 촬영(연차 비교)용 선택 입력. 이전 사진은 이 기기에 저장된 같은 물건의 사진 중에서 고른다. 기본 흐름을 막지 않게 접어 둔다
      const series = el("div", { class: "series" });
      const vp = el("input", { class: "viewpoint", type: "text", maxlength: String(VIEWPOINT_MAX), placeholder: "예: 전면-남측", value: p.viewpoint_id });
      vp.addEventListener("input", () => { p.viewpoint_id = vp.value.trim(); });
      const hd = el("input", { class: "heading", type: "number", min: "0", max: "359", step: "1", inputmode: "numeric", value: p.heading_deg });
      hd.addEventListener("input", () => { p.heading_deg = hd.value === "" ? "" : Number(hd.value); });
      const prev = el("select", { class: "previous" });
      prev.append(el("option", { value: "", text: state.prevPhotos.length ? "없음" : "없음 (이 폰에 이 대상의 이전 사진 없음)" }));
      for (const q of state.prevPhotos) {
        if (q.sha256 === p.sha256) continue;
        prev.append(el("option", { value: q.sha256, text: q.label, selected: q.sha256 === p.previous_photo_sha256 }));
      }
      prev.addEventListener("change", () => {
        p.previous_photo_sha256 = prev.value;
        const q = state.prevPhotos.find((x) => x.sha256 === prev.value);
        if (q?.viewpoint_id && !p.viewpoint_id) { p.viewpoint_id = q.viewpoint_id; vp.value = q.viewpoint_id; }
      });
      series.append(el("label", { text: "촬영 지점 " }, vp), el("label", { text: "방향 ° " }, hd), el("label", { text: "비교할 이전 사진 " }, prev));
      box.append(el("details", { class: "disclosure quiet" }, el("summary", { text: "해마다 같은 자리에서 찍는다면" }), series));
    }
    box.append(el("button", { text: "빼기", class: "danger small", onclick: () => { state.photos.splice(idx, 1); renderPhotos(); } }));
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
  if (!status) problems.push("달라짐·그대로·확인 어려움 가운데 하나를 고릅니다");
  const observedAt = dateOnly ? ($("observed-at").value || "").slice(0, 10) : fromDatetimeLocal($("observed-at").value);
  if (!observedAt) problems.push("본 시각을 다시 넣습니다");
  const bad = state.photos.filter((p) => p.error);
  if (bad.length) problems.push("빨간 글이 붙은 사진을 뺍니다");
  const good = state.photos.filter((p) => !p.error);
  const uniq = new Map(good.map((p) => [p.sha256, p]));
  if (uniq.size !== good.length) problems.push("같은 사진이 두 번 들어 있습니다");
  if (problems.length) return text($("observe-note"), "아직 저장하지 않았습니다. " + problems.join(". ") + ".", "bad");

  const [routeVersionId, studyId, dataMode] = await Promise.all([
    state.store.getMeta("route_version_id"), state.store.getMeta("study_id"), state.store.getMeta("data_mode"),
  ]);
  if (!studyId) return text($("observe-note"), "아직 저장하지 않았습니다. 설정에서 작업 공간 이름을 먼저 저장합니다.", "bad");
  if (state.target.data_mode !== (dataMode ?? "synthetic")) {
    return text($("observe-note"), `아직 저장하지 않았습니다. 이 대상은 ${MODE_SHORT[state.target.data_mode]} 자료인데 설정은 ${MODE_SHORT[dataMode ?? "synthetic"]} 자료입니다. 설정의 자료 종류를 맞춘 뒤 저장합니다.`, "bad");
  }
  const ev = buildEvent({
    eventId: uuid4(), assetId: state.target.asset_id, observedAt, precision: dateOnly ? "date" : "datetime",
    deviceCreatedAt: isoWithOffset(), routeVersionId: routeVersionId || null, changeStatus: status, note: note.trim() ? note : null,
    attachments: [...uniq.values()].map((p) => ({ sha256: p.sha256, ext: p.ext, bytes: p.bytes, tags: [...p.tags], viewpoint_id: p.viewpoint_id || "",
                                                  heading_deg: p.heading_deg === "" ? null : p.heading_deg, previous_photo_sha256: p.previous_photo_sha256 || "" })),
  });
  const errs = validateEvent(ev);
  if (errs.length) return text($("observe-note"), "아직 저장하지 않았습니다. " + errs.join(", "), "bad");

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
    return text($("observe-note"), `저장하지 못했습니다 (${[e?.name, e?.message || e].filter(Boolean).join(": ")}). 기록은 남지 않았습니다. 폰 저장 공간을 확인하고 다시 저장합니다.`, "bad");
  }
  text($("observe-note"), "이 폰에 저장했습니다.", "ok");
  await renderRecords();
  await renderAssets();
  showObservationDone(record, photos.length);
}

function showObservationDone(record, photoCount) {
  const asset = state.target;
  $("done-text").textContent = `${asset.label}, ${CHANGE_STATUS_LABEL[record.event.payload.change_status]}${photoCount ? `, 사진 ${photoCount}장` : ""}. 이 폰에 저장했고 아직 PC 로 보내지 않았습니다.`;
  const next = nextAsset(visibleAssets(), asset.asset_id, state.events);
  $("done-next").hidden = !next;
  if (next) $("done-next").textContent = `다음: ${next.label}`;
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
  if (!studyId) { if (!quiet) exportNote("설정에서 작업 공간 이름을 먼저 저장합니다.", "bad"); text($("export-summary"), "작업 공간 이름이 없어 보낼 수 없습니다. 설정에서 저장합니다.", "summary warn"); return null; }
  const records = await state.store.listEvents();
  const sel = selectRecords(records, { studyId, dataMode: dataMode ?? "synthetic", includeExported: $("include-exported").checked });
  const meta = await state.store.photoMeta();
  const { batches, errors } = planBatches(sel.selected, meta);
  const photoCount = new Set(batches.flatMap((b) => b.shas)).size;
  const est = batches.reduce((s, b) => s + b.estBytes, 0);
  // 보낼 것을 먼저 문장으로 (J5-054). 묶음은 둘 이상일 때만 말한다
  const head = sel.selected.length ? `보낼 기록 ${sel.selected.length}건${photoCount ? `, 사진 ${photoCount}장` : ""} (약 ${fmtBytes(est)})` + (batches.length > 1 ? `. 파일 ${batches.length}개로 나눠 만듭니다` : "") + "." : "보낼 기록이 없습니다.";
  text($("export-summary"), head + (sel.excludedExported ? ` 이미 보낸 ${sel.excludedExported}건은 넣지 않습니다.` : "") + (sel.otherContext ? ` 다른 작업 공간·자료 종류의 기록 ${sel.otherContext}건은 넣지 않습니다.` : ""), "summary");
  $("export-errors").replaceChildren(...errors.map((e) => el("li", { class: "bad", text: `기록 ${e.event_id.slice(0, 8)}… 은 넣지 않았습니다: ${e.reason}` })));
  return { studyId, dataMode: dataMode ?? "synthetic", batches, errors, k: 0, package: null, url: null, attempted: false, recordsById: new Map(sel.selected.map((r) => [r.event_id, r])) };
}

function fmtBytes(n) {
  return n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : n >= 1e3 ? (n / 1e3).toFixed(0) + " KB" : n + " B";
}

function renderExportSteps() {
  const ex = state.export;
  const step = ex ? exportStep({ hasPackage: !!ex.package, attempted: !!ex.attempted, done: false }) : 1;
  // 지금 단계의 버튼만 보인다 (J5-054). 저장을 요청한 뒤에는 '저장 확인' 과 함께 '다시 저장' 을 남긴다
  $("export-prepare").hidden = step >= 3;
  $("export-save").hidden = step < 3;
  $("export-save").className = step === 4 ? "secondary" : "primary";
  if (step === 4) $("export-save").textContent = "다시 저장";
  $("export-confirm").hidden = step < 4;
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
    if (!plan.batches.length) { state.export = null; $("export-save").disabled = true; $("export-confirm").disabled = true; return exportNote("보낼 기록이 없습니다.", "pencil"); }
    const k = plan.k;
    const batch = plan.batches[k];
    exportNote(`파일 ${k + 1}/${plan.batches.length} 만드는 중…`, "pencil");
    const pkg = await buildPackage(batch, plan.recordsById, {
      loadPhoto: async (sha) => { const p = await state.store.getPhoto(sha); return p ? { blob: p.blob, ext: p.ext } : undefined; },
      studyId: plan.studyId, dataMode: plan.dataMode, k: k + 1, n: plan.batches.length,
      newAssets: await state.store.listDeviceAssets(),   // 이 묶음의 기록이 가리키는 기기 생성 물건만 assets.new.json 으로 (J5-028)
    });
    plan.package = pkg; plan.attempted = false;
    state.export = plan;
    $("export-save").textContent = plan.batches.length > 1 ? `파일 저장 ${k + 1}/${plan.batches.length}` : "파일 저장";
    $("export-save").disabled = false;
    $("export-confirm").disabled = true;
    exportNote(`파일을 만들었습니다 (기록 ${pkg.eventIds.length}건${pkg.photoCount ? `, 사진 ${pkg.photoCount}장` : ""}${pkg.newAssetCount ? `, 이 폰에서 만든 대상 ${pkg.newAssetCount}곳` : ""}, ${fmtBytes(pkg.bytes)}). 이제 "파일 저장" 을 누릅니다.`, "ok");
  } catch (e) {
    exportNote("파일을 만들지 못했습니다: " + (e?.message || e), "bad");
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
  }).then(renderExportHistory).catch((e) => exportNote("만든 파일 목록에 남기지 못했습니다: " + e, "bad"));
  $("export-confirm").disabled = false;
  renderExportSteps();
  exportNote(`${ex.package.filename} 저장을 요청했습니다. 파일 앱에서 저장된 것을 확인한 뒤 "저장 확인" 을 누릅니다. 누르기 전에는 보낸 기록으로 치지 않습니다.`, "warn");
}

async function exportConfirm() {
  const ex = state.export;
  if (!ex?.package || !ex.attempted) return;
  $("export-confirm").disabled = true;
  try {
    await state.store.markExported(ex.package.eventIds, ex.package.packageId, new Date().toISOString());
  } catch (e) {
    $("export-confirm").disabled = false;
    return exportNote("보낸 기록으로 바꾸지 못했습니다. 다시 누릅니다: " + (e?.message || e), "bad");
  }
  if (ex.url) { URL.revokeObjectURL(ex.url); ex.url = null; }
  ex.k += 1; ex.package = null; ex.attempted = false;
  $("export-save").disabled = true;
  await renderRecords();
  await renderAssets();
  await renderExportHistory();
  if (ex.k < ex.batches.length) {
    exportNote(`파일 ${ex.k}/${ex.batches.length} 을 확인했습니다. 다음 파일을 만듭니다.`, "ok");
    await exportPrepare();
  } else {
    exportNote(`보냈습니다. 이제 파일을 PC 로 옮겨 반영합니다. 반영과 백업은 PC 에서 확인합니다.`, "ok");
    state.export = null;
    await exportPlan({ quiet: true });
    renderExportSteps();
  }
}

async function renderExportHistory() {
  const list = await state.store.listExports();
  $("export-history").replaceChildren(...list.slice().reverse().map((e) => el("li", {},
    el("div", { class: "num", text: e.filename }),
    el("div", { class: "pencil small", text: `기록 ${e.event_ids.length}건, 사진 ${e.photo_count}장, ${fmtBytes(e.bytes)}` }),
    el("div", { class: e.confirmed_at ? "ok small" : "warn small", text: e.confirmed_at ? `저장 확인 ${shortWhen(e.confirmed_at) ?? e.confirmed_at}` : "저장 확인 안 됨. 그 기록은 다시 보낼 수 있습니다" }),
  )));
  if (!list.length) $("export-history").append(el("li", { class: "empty", text: "아직 만든 파일이 없습니다." }));
}

// ---- 목록·상태 ----
async function renderRecords() {
  const events = await state.store.listEvents();
  state.events = events;
  const labels = new Map(state.assets.map((a) => [a.asset_id, a.label]));
  // 최근 기록이 위로 (J5-054). 아직 PC 로 안 보낸 기록은 형광펜 표시
  const sorted = events.slice().sort((a, b) => (a.saved_at < b.saved_at ? 1 : a.saved_at > b.saved_at ? -1 : 0));
  $("record-list").replaceChildren(...sorted.map((r) => el("li", {},
    el("span", { class: "title" }, el("span", { text: labels.get(r.asset_id) ?? r.asset_label ?? r.asset_id }),
      labels.has(r.asset_id) ? el("span") : el("span", { class: "warn small", text: " 지금 목록에 없는 대상" })),
    el("span", { class: r.status === "saved" ? "badge state-saved" : "badge state-exported", text: r.status === "saved" ? "안 보냄" : "보냄" }),
    el("span", { class: "sub" }, el("span", { class: `badge status-${r.event.payload.change_status}`, text: CHANGE_STATUS_LABEL[r.event.payload.change_status] }),
      el("span", { class: "num", text: r.event.observed_at.replace("T", " ").slice(0, 16) }), r.event.attachment_refs.length ? el("span", { text: `사진 ${r.event.attachment_refs.length}장` }) : "", modeBadge(r.data_mode)),
    r.event.payload.note ? el("div", { class: "note", text: r.event.payload.note }) : el("span"),
  )));
  if (!events.length) $("record-list").append(el("li", { class: "empty", text: "아직 기록이 없습니다. 현장 화면에서 대상을 골라 기록합니다." }));
  text($("record-count"), events.length ? `${events.length}건` : "");
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
  $("stat-unexported").parentElement.classList.toggle("has", unexported > 0);
  let last = null;
  for (const r of state.events) if (!last || r.saved_at > last.saved_at) last = r;
  $("home-last").textContent = last ? `최근 기록 ${shortWhen(last.saved_at)}, ${last.asset_label ?? ""}. ${last.status === "saved" ? "아직 PC 로 안 보냈습니다." : "PC 로 보냈습니다."}` : "";
  $("home-empty").hidden = c.assets > 0;
  if (!c.assets) openSheet();
  $("start-observing").hidden = c.assets === 0 || !!state.mapSelected;   // 대상 카드가 열려 있으면 카드의 '기록하기' 하나만 둔다
  $("start-observing").textContent = c.events ? "이어서 기록" : "기록 시작";
  // 기록 탭의 배지는 아직 PC 로 안 보낸 기록 수 (보낼 일이 남았다는 표시)
  const rb = $("nav-records-badge");
  rb.hidden = unexported === 0;
  rb.textContent = String(unexported);
  rb.setAttribute("aria-label", `안 보낸 기록 ${unexported}건`);
}

async function refreshStatus() {
  const c = await state.store.counts();
  const studyId = await state.store.getMeta("study_id");
  const mode = (await state.store.getMeta("data_mode")) ?? "synthetic";
  text($("status-line"), `앱 ${APP_VERSION} · ${mode} · study ${studyId ?? "(미설정)"} · 대상 ${c.assets} · 기록 ${c.events} · 사진 ${c.photos} · ${navigator.onLine ? "온라인" : "오프라인"}`);
  $("offline-banner").hidden = navigator.onLine;
  syncBannerSpace();
}

/** 오프라인 배너의 실제 높이만큼 상단 바 아래 화면을 내린다 (리뷰 반영 PR #101). 글자 크기·폭에 따라 배너가 여러 줄이 되므로 고정값을 쓰지 않는다.
 *  CSSOM 으로 CSS 변수 하나만 바꾼다 (style 속성 문자열을 쓰지 않아 CSP style-src 'self' 와 맞다) */
function syncBannerSpace() {
  const h = $("offline-banner").hidden ? 0 : Math.ceil($("offline-banner").getBoundingClientRect().height);
  document.documentElement.style.setProperty("--banner-h", `${h}px`);
}

function registerSw() {
  // 최소 앱 캐시. 보안 컨텍스트가 아니면 등록되지 않으며 앱은 그대로 동작한다.
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register("./sw.js").catch(() => {});
}

/** 현장 시트의 '기록 시작': 현재 필터 안에서 이 폰에 기록이 없는 첫 대상, 없으면 첫 대상의 기록 화면을 연다. 필터로 보이는 대상이 없으면 숨은 대상으로 넘어가지 않고 안내한다 (리뷰 반영 PR #75). */
function startObservingFromHome() {
  const visible = visibleAssets();
  const next = nextAsset(visible, null, state.events) ?? visible[0];
  if (next) return startObservation(next);
  if (state.assets.length && effectiveFilter() === "watchlist") {
    openSheet();
    text($("asset-filter-note"), "관찰목록에 대상이 없어 시작하지 않았습니다. '전체' 를 누른 뒤 시작합니다.", "warn");
    $("asset-filter").querySelector('button[data-filter="all"]').focus();
  }
}

function onViewShown(name, changed) {
  if (name === "map") { updateMapInset(); mapCall((m) => { m.resize(); if (changed) m.fit(); }); }
  if (name === "records") { exportPlan({ quiet: true }).then(renderExportSteps).catch(() => {}); }
}

// ---- 현장 시트 (J5-054): 접힘(peek, 대상 수만) ↔ 펼침(open, 목록·카드). 넓은 화면에서는 CSS 가 늘 펼친다 ----
function setSheet(open) {
  const sheet = $("field-sheet");
  sheet.dataset.state = open ? "open" : "peek";
  $("sheet-toggle").setAttribute("aria-expanded", String(open));
  $("sheet-toggle").querySelector(".sr-only").textContent = open ? "목록 접기" : "목록 펼치기";
  updateMapInset();
}
function openSheet() { setSheet(true); }

/** 지도에 덮인 영역(아래 시트, 위 찾기 칸)을 알려 전체 보기·필지 맞추기가 보이는 곳에 그려지게 한다. 넓은 화면에서는 시트가 지도 옆이라 아래 덮개가 없다 */
function updateMapInset() {
  const map = $("map-svg").getBoundingClientRect();
  if (!map.height) return;
  const sheet = $("field-sheet").getBoundingClientRect();
  // 위 덮개는 찾기 칸·지도 버튼 줄까지. 용도지역 범례는 지도 위에 겹쳐 두고 덮개로 치지 않는다 (켜면 화면이 좁아지지 않게)
  const tops = [$("parcel-search-form"), $("map-top").querySelector(".map-chips")].map((e) => e.getBoundingClientRect()).filter((r) => r.height > 0);
  const top = tops.length ? Math.max(...tops.map((r) => r.bottom)) - map.top : 0;
  const bottom = sheet.left <= map.left + 1 && sheet.top < map.bottom ? map.bottom - sheet.top : 0;
  mapCall((m) => m.setInset({ top, bottom }));
}

/** 시작하기에서 자료를 넣은 뒤: 시트를 접어 지도가 보이게 하고 전체 보기 */
function showFieldAfterLoad() {
  if (!state.assets.length && !state.parcelsCount) return;
  setSheet(false);
  mapCall((m) => m.fit());
}

async function main() {
  try {
    state.store = await Store.open();
  } catch (e) {
    const f = $("fatal");
    f.hidden = false;
    f.textContent = "이 브라우저에서는 기록을 저장할 수 없습니다 (브라우저 저장소를 열 수 없음: " + e + "). Safari 일반 창이나 다른 브라우저로 엽니다.";
    // 화면을 채우는 현장 지도(고정 위치)가 안내를 덮지 않게 화면과 탭을 모두 숨긴다 (리뷰 반영 PR #101)
    for (const v of document.querySelectorAll("section.view")) v.hidden = true;
    document.querySelector(".bottom-nav").hidden = true;
    return;
  }
  const sections = Object.fromEntries(Array.from(document.querySelectorAll("section.view")).map((s) => [s.dataset.view, s]));
  state.nav = createNavigator({ sections, navButtons: Array.from(document.querySelectorAll(".bottom-nav .nav-btn")), onShow: onViewShown });
  state.nav.show(viewFromHash(location.hash), { focus: false });
  await loadSettings();
  // 버튼·입력 리스너는 목록·기록을 그리기 전에 붙인다. 첫 화면 직후의 탭이 무시되지 않게 한다.
  $("save-settings").addEventListener("click", saveSettings);
  $("load-synthetic").addEventListener("click", loadSyntheticSeed);
  $("home-load-synthetic").addEventListener("click", loadSyntheticAll);
  $("home-view-file").addEventListener("change", async (e) => { await loadViewFile(e.target, $("home-note")); if ($("home-note").className === "ok") showFieldAfterLoad(); });
  $("home-go-settings").addEventListener("click", () => state.nav.show("settings"));
  $("start-observing").addEventListener("click", startObservingFromHome);
  $("sheet-toggle").addEventListener("click", () => setSheet($("field-sheet").dataset.state === "peek"));
  $("map-selected-close").addEventListener("click", () => { clearMapSelection(); $("sheet-toggle").focus(); });
  for (const b of $("asset-filter").querySelectorAll("button[data-filter]")) b.addEventListener("click", () => setAssetFilter(b.dataset.filter));
  $("seed-file").addEventListener("change", (e) => loadSeedFile(e.target));
  $("load-synthetic-parcels").addEventListener("click", loadSyntheticParcels);
  $("parcels-file").addEventListener("change", (e) => loadParcelsFile(e.target));
  // 필지 찾기·현재 위치는 지도가 못 떠도 동작한다 (검색·포함 판정은 순수 함수, 패널은 DOM). 지도 초기화와 무관하게 여기서 잇는다 (리뷰 반영 PR #73)
  $("parcel-search-form").addEventListener("submit", (e) => { e.preventDefault(); searchParcels($("parcel-search").value); });
  $("parcel-filter-form").addEventListener("submit", (e) => { e.preventDefault(); runParcelFilter(); });
  $("pf-clear").addEventListener("click", clearParcelFilter);
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
  $("done-list").addEventListener("click", () => closeObservation({ toView: "map" }));
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
    if (rec) { state.parcelsRec = rec; state.parcels = rec.bundle; state.parcelsCount = rec.bundle.features.length; }
    state.zoneColors = (await state.store.getMeta("zone_colors")) === true;
    const savedFilter = await state.store.getMeta("asset_filter");
    state.assetFilter = ASSET_FILTERS.includes(savedFilter) ? savedFilter : "all";
    // 찾기 칸·지도 버튼을 먼저 보이게 한 뒤 덮인 영역을 재고 필지를 그린다 (첫 맞추기가 덮개를 알게)
    applySearchUi();
    applyZoneColors();
    updateMapInset();
    if (rec) mapCall((m) => m.setParcels(rec.bundle));
    applyZoneColors();
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
  // 처음 시트: 자료가 없거나 지도를 못 그리면 펼쳐 시작하기·목록을 보이고, 그 밖에는 접어 지도를 보인다
  setSheet(!state.map || (state.assets.length === 0 && state.parcelsCount === 0));
  // 시트·찾기 칸의 크기가 바뀌면(펼치기·글자 크기·회전) 지도에 다시 알린다
  if (typeof ResizeObserver === "function") {
    try { const ro = new ResizeObserver(() => updateMapInset()); ro.observe($("field-sheet")); ro.observe($("map-top")); } catch {}
    try { new ResizeObserver(() => syncBannerSpace()).observe($("offline-banner")); } catch {}
  }
  if (state.nav.current === "map") mapCall((m) => { m.resize(); m.fit(); });
  registerSw();
}

main();
