// 필지 번들(.j5parcels.json, ADR-13) 검증과 기하 유틸. schemas/parcels_bundle.schema.json 과 같은 규칙의 핵심 부분.
// PNU 는 필지 외부 ID 이고 asset_id 가 아니다. 도형면적(area_m2_geom)은 공부면적이 아니다. 외부 통신 없음.

export const PARCELS_FORMAT = "1.0.0";
export const MAX_PARCELS = 8000;
const PNU_RE = /^[0-9]{19}$/;
const TOP_ALLOWED = new Set(["type", "j5parcels", "data_mode", "generated_at", "source", "clip", "count", "bbox", "warnings", "features", "study_id", "source_dataset_version", "attrs_sources"]);
/** 자료 종류별 허용 필드 (정본 ATTR_GROUPS 와 같다). 이력 항목의 바뀐 필드는 그 종류의 필드만 허용한다 (소유자 이름 같은 필드는 이력으로도 들어오지 못한다). */
const ATTR_GROUP_KEYS = Object.freeze({
  land_feature: new Set(["jimok_name", "registered_area_m2", "official_land_price_krw_m2", "price_base_year", "price_base_month", "use_zone_1", "use_zone_2", "land_use_situation", "road_side", "terrain_height", "terrain_form"]),
  land_plan: new Set(["plan_zones", "plan_zones_truncated"]),
  land_ownership: new Set(["ownership_kind_code", "ownership_kind", "co_owner_count", "ownership_changed_on", "ownership_change_cause_code", "national_institution_code"]),
});
const ATTR_KINDS = new Set(Object.keys(ATTR_GROUP_KEYS));
const ATTR_KEYS = new Set(["jimok_name", "registered_area_m2", "official_land_price_krw_m2", "price_base_year", "price_base_month", "use_zone_1", "use_zone_2", "land_use_situation", "road_side",
  "terrain_height", "terrain_form", "plan_zones", "plan_zones_truncated", "ownership_kind_code", "ownership_kind", "co_owner_count", "ownership_changed_on", "ownership_change_cause_code", "national_institution_code", "as_of"]);
const PROP_REQUIRED = ["pnu", "label", "emd_code", "emd_name", "mountain", "bon", "bu", "jimok", "jibun_raw", "jibun_mismatch", "area_m2_geom", "area_missing_reason", "bbox"];

const isLonLat = (p) => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite) && p[0] >= -180 && p[0] <= 180 && p[1] >= -90 && p[1] <= 90;
const isRing = (r) => Array.isArray(r) && r.length >= 4 && r.every(isLonLat);
const isPolygon = (p) => Array.isArray(p) && p.length >= 1 && p.every(isRing);
const isBbox = (b) => Array.isArray(b) && b.length === 4 && b.every(Number.isFinite) && b[0] <= b[2] && b[1] <= b[3];

/** 문제 목록. 빈 배열이면 유효. 앞 몇 건만 모으고 큰 파일에서 멈추지 않는다. */
export function validateParcels(doc) {
  const errs = [];
  if (doc === null || typeof doc !== "object" || Array.isArray(doc)) return ["객체가 아님"];
  for (const k of Object.keys(doc)) if (!TOP_ALLOWED.has(k)) errs.push(`허용되지 않은 필드 ${k}`);
  if (doc.type !== "FeatureCollection") errs.push("type 은 FeatureCollection");
  if (doc.j5parcels !== PARCELS_FORMAT) errs.push(`j5parcels 형식 버전은 ${PARCELS_FORMAT} (파일: ${doc.j5parcels})`);
  if (!["synthetic", "real"].includes(doc.data_mode)) errs.push("data_mode 는 synthetic 또는 real");
  if (typeof doc.generated_at !== "string") errs.push("generated_at 누락");
  const src = doc.source;
  if (!src || typeof src !== "object") errs.push("source 누락");
  else {
    for (const k of ["name", "file", "shp_sha256", "dbf_sha256", "crs", "encoding", "record_count", "geometry_version", "license", "fields"]) if (!(k in src)) errs.push(`source.${k} 누락`);
    if (typeof src.geometry_version !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(src.geometry_version)) errs.push("source.geometry_version 은 YYYY-MM-DD");
    if (!src.crs || typeof src.crs !== "object" || !("epsg" in src.crs) || !("name" in src.crs)) errs.push("source.crs 누락");
  }
  if (!doc.clip || !isBbox(doc.clip.bbox)) errs.push("clip.bbox 누락");
  if ("attrs_sources" in doc && !(Array.isArray(doc.attrs_sources) && doc.attrs_sources.every((a) => a && typeof a === "object" && typeof a.kind === "string" && typeof a.name === "string"))) errs.push("attrs_sources 형식");
  if ("study_id" in doc && (typeof doc.study_id !== "string" || !doc.study_id.length)) errs.push("study_id 형식");
  if ("source_dataset_version" in doc && !(Number.isInteger(doc.source_dataset_version) && doc.source_dataset_version >= 0)) errs.push("source_dataset_version 형식");
  if (!Array.isArray(doc.features)) { errs.push("features 가 배열이 아님"); return errs; }
  if (doc.features.length > MAX_PARCELS) errs.push(`필지가 ${MAX_PARCELS}개를 넘음 (${doc.features.length})`);
  if (doc.count !== doc.features.length) errs.push(`count(${doc.count})와 features 수(${doc.features.length})가 다름`);
  if (doc.bbox !== null && !isBbox(doc.bbox)) errs.push("bbox 형식");
  const ids = new Set();
  for (let i = 0; i < doc.features.length && errs.length < 20; i++) {
    const f = doc.features[i];
    const at = `features[${i}]`;
    if (!f || typeof f !== "object" || f.type !== "Feature") { errs.push(`${at} Feature 가 아님`); continue; }
    if (!PNU_RE.test(f.id)) errs.push(`${at} id 가 19자리 PNU 가 아님`);
    else if (ids.has(f.id)) errs.push(`${at} PNU 중복 ${f.id}`);
    ids.add(f.id);
    const g = f.geometry;
    if (!g || (g.type !== "Polygon" && g.type !== "MultiPolygon")) errs.push(`${at} geometry 는 Polygon/MultiPolygon`);
    else if (g.type === "Polygon" ? !isPolygon(g.coordinates) : !(Array.isArray(g.coordinates) && g.coordinates.length >= 1 && g.coordinates.every(isPolygon))) errs.push(`${at} 좌표 형식`);
    const p = f.properties;
    if (!p || typeof p !== "object") { errs.push(`${at} properties 누락`); continue; }
    for (const k of PROP_REQUIRED) if (!(k in p)) errs.push(`${at} properties.${k} 누락`);
    if (p.pnu !== f.id) errs.push(`${at} properties.pnu 와 id 가 다름`);
    if (typeof p.label !== "string" || !p.label.length || p.label.length > 20) errs.push(`${at} label`);
    if (!isBbox(p.bbox)) errs.push(`${at} bbox`);
    if ("asset_ids" in p && !(Array.isArray(p.asset_ids) && p.asset_ids.every((x) => typeof x === "string"))) errs.push(`${at} asset_ids`);
    if (p.area_m2_geom === null ? typeof p.area_missing_reason !== "string" : (typeof p.area_m2_geom !== "number" || p.area_m2_geom < 0)) errs.push(`${at} area_m2_geom (null 이면 area_missing_reason 필요)`);
    if ("attrs" in p) {
      const a = p.attrs;
      if (!a || typeof a !== "object" || Array.isArray(a)) errs.push(`${at} attrs 가 객체가 아님`);
      else {
        for (const k of Object.keys(a)) if (!ATTR_KEYS.has(k)) { errs.push(`${at} attrs.${k} 는 허용되지 않은 필드`); break; }
        if ("plan_zones" in a && !(Array.isArray(a.plan_zones) && a.plan_zones.every((z) => z && typeof z === "object" && typeof z.code === "string"))) errs.push(`${at} attrs.plan_zones 형식`);
        for (const k of ["registered_area_m2", "official_land_price_krw_m2", "co_owner_count"]) if (a[k] != null && !(typeof a[k] === "number" && a[k] >= 0)) errs.push(`${at} attrs.${k} 형식`);
        if ("as_of" in a && !(typeof a.as_of === "string" && /^\d{4}-\d{2}-\d{2}$/.test(a.as_of))) errs.push(`${at} attrs.as_of 는 YYYY-MM-DD`);
      }
    }
    if ("attrs_history" in p) {
      const h = p.attrs_history;
      if (!Array.isArray(h) || h.length > 200) errs.push(`${at} attrs_history 형식`);
      else if (!h.every((c) => c && typeof c === "object" && /^\d{4}-\d{2}-\d{2}$/.test(c.as_of) && ATTR_KINDS.has(c.kind) && typeof c.first === "boolean" && c.changes && typeof c.changes === "object" && !Array.isArray(c.changes)
        && Object.values(c.changes).every((d) => d && typeof d === "object" && "from" in d && "to" in d))) errs.push(`${at} attrs_history 항목 형식`);
      else {
        const bad = h.flatMap((c) => Object.keys(c.changes).filter((k) => !ATTR_GROUP_KEYS[c.kind].has(k)));
        if (bad.length) errs.push(`${at} attrs_history 에 허용되지 않은 필드 ${bad[0]}`);
      }
    }
  }
  return errs;
}

// ---- 필지 속성 (J5-025, ADR-19): VWorld 토지특성·이용계획·소유구분에서 읽은 값 그대로. 확인·판단이 아니다. ----

/** 용도지역 이름 → 색 분류 키 (styles.css 의 .parcel.zone-<키>). 모르면 "other", 없으면 null. 이름의 부분 문자열로만 나누며 법적 판단이 아니다. */
export function zoneCategory(name) {
  if (typeof name !== "string" || !name.trim()) return null;
  const n = name.replace(/\s+/g, "");
  if (n.includes("전용주거")) return "res1";
  if (n.includes("일반주거")) return "res2";
  if (n.includes("준주거")) return "res3";
  if (n.includes("상업")) return "com";
  if (n.includes("공업")) return "ind";
  if (n.includes("녹지")) return "green";
  if (n.includes("관리") || n.includes("농림") || n.includes("자연환경")) return "rural";
  return "other";
}
export const ZONE_LABELS = Object.freeze({ res1: "전용주거", res2: "일반주거", res3: "준주거", com: "상업", ind: "공업", green: "녹지", rural: "관리·농림·자연환경", other: "기타 용도지역" });

/** 숫자를 천 단위 쉼표로 (toLocaleString 은 환경마다 달라 직접). */
export function fmtInt(n) {
  if (!Number.isFinite(n)) return null;
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** 필지 패널에 보일 [항목, 값] 목록. 값이 없는 항목은 "미확인" 으로 두어 결측을 감추지 않는다. 소유는 구분·인수·변동일뿐이다.
 *  기준일은 속성의 as_of(파생본) 이며 없으면 fallbackAsOf(변환 번들의 도형 기준일). 도형 기준일과 다를 수 있어 따로 보인다. */
export function attrLines(a, fallbackAsOf = null) {
  if (!a || typeof a !== "object") return [];
  const asOf = a.as_of ?? fallbackAsOf;
  const miss = "미확인";
  const zone = [a.use_zone_1, a.use_zone_2].filter((z) => typeof z === "string" && z).join(" · ");
  const price = Number.isFinite(a.official_land_price_krw_m2) ? `${fmtInt(a.official_land_price_krw_m2)}원/㎡` + (a.price_base_year ? ` (${a.price_base_year}년${a.price_base_month ? ` ${a.price_base_month}월` : ""} 기준)` : "") : miss;
  const zones = Array.isArray(a.plan_zones) ? a.plan_zones : [];
  const planText = zones.length ? zones.map((z) => (z.name ? z.name : z.code) + (z.relation && z.relation !== "포함" ? `(${z.relation})` : "")).join(", ") + (a.plan_zones_truncated ? " … (이름 일부는 원본 열 길이에 잘림, 코드만 있음)" : "") : miss;
  const own = a.ownership_kind ?? (a.ownership_kind_code ? `구분 코드 ${a.ownership_kind_code}` : null);
  return [
    ["지목", a.jimok_name ?? miss],
    ["공부면적", Number.isFinite(a.registered_area_m2) ? `${a.registered_area_m2.toFixed(1)} ㎡ (토지대장)` : miss],
    ["공시지가", price],
    ["용도지역", zone || miss],
    ["이용상황", a.land_use_situation ?? miss],
    ["도로접면", a.road_side ?? miss],
    ["지형", [a.terrain_height, a.terrain_form].filter(Boolean).join(" · ") || miss],
    ["규제·지역지구", planText],
    ["소유구분", (own ?? miss) + (Number.isFinite(a.co_owner_count) && a.co_owner_count > 1 ? ` · 공유 ${a.co_owner_count}인` : "") + (a.ownership_changed_on ? ` · 변동 ${a.ownership_changed_on}` : "")],
    ["속성 기준일", asOf ? `${asOf} (토지 자료 기준, 도형 기준일과 다를 수 있음)` : miss],
  ];
}

export const ATTR_FIELD_LABELS = Object.freeze({ jimok_name: "지목", registered_area_m2: "공부면적", official_land_price_krw_m2: "공시지가", price_base_year: "공시 기준연도", price_base_month: "공시 기준월",
  use_zone_1: "용도지역", use_zone_2: "용도지역 2", land_use_situation: "이용상황", road_side: "도로접면", terrain_height: "지형 높이", terrain_form: "지형 형상", plan_zones: "규제·지역지구",
  plan_zones_truncated: "이름 목록 잘림", ownership_kind_code: "소유 구분 코드", ownership_kind: "소유구분", co_owner_count: "공유인수", ownership_changed_on: "소유 변동일",
  ownership_change_cause_code: "변동 원인 코드", national_institution_code: "국가기관 구분" });
export const ATTR_KIND_LABELS = Object.freeze({ land_feature: "토지특성", land_plan: "토지이용계획", land_ownership: "토지소유" });

/** 속성 값 하나를 글로. 규제 목록은 이름(없으면 코드)·관계, 공시지가는 원/㎡, 없음은 "없음". */
export function fmtAttrValue(field, v) {
  if (v == null) return "없음";
  if (field === "plan_zones") return Array.isArray(v) && v.length ? v.map((z) => (z.name ?? z.code) + (z.relation && z.relation !== "포함" ? `(${z.relation})` : "")).join(", ") : "없음";
  if (field === "official_land_price_krw_m2") return `${fmtInt(v)}원/㎡`;
  if (field === "registered_area_m2") return `${v} ㎡`;
  if (typeof v === "boolean") return v ? "예" : "아니오";
  return String(v);
}

/** 이력 글에서 빼는 필드: 이름 필드가 같이 있는 코드, 잘림 표시, 공시 기준 연월(공시지가 변경에 딸림). 정본·PC 조회(parcels-history)에는 모두 있다. */
const ATTR_HISTORY_SKIP = new Set(["ownership_kind_code", "ownership_change_cause_code", "national_institution_code", "plan_zones_truncated", "price_base_year", "price_base_month"]);

/** 변화 항목(attrs_history 의 한 건) → 한 줄 글. 처음 확인은 "지목 대 · 공시지가 …", 변경은 "공시지가 A → B · …". 필드 순서는 ATTR_FIELD_LABELS 순. */
export function attrChangeText(c) {
  const keys = Object.keys(c.changes ?? {}).filter((k) => !ATTR_HISTORY_SKIP.has(k)).sort((a, b) => Object.keys(ATTR_FIELD_LABELS).indexOf(a) - Object.keys(ATTR_FIELD_LABELS).indexOf(b));
  return keys.map((k) => {
    const d = c.changes[k], label = ATTR_FIELD_LABELS[k] ?? k;
    return c.first ? `${label} ${fmtAttrValue(k, d.to)}` : `${label} ${fmtAttrValue(k, d.from)} → ${fmtAttrValue(k, d.to)}`;
  }).join(" · ");
}

// ---- 공시지가 추이 (J5-030): 파생본의 현재 속성(attrs)과 변화 항목(attrs_history)에서 기준일마다의 공시지가를 되살린다 ----
const PRICE_FIELDS = ["official_land_price_krw_m2", "price_base_year", "price_base_month"];

/**
 * 공시지가 점 목록(오래된 것부터). 현재 속성에서 토지특성 변화 항목을 거꾸로 짚어 각 기준일의 값을 정확히 되살린다(변화 항목은 이전 값과 새 값을 함께 담는다).
 * 점: { as_of, price, year, month, first, earlier, deltaPct, sameBase }. as_of 가 null 이면 파생본의 이력 상한으로 앞부분이 잘려 기준일을 모르는 이전 값이다.
 * deltaPct 는 바로 앞 점 대비 증감률(%)이며 앞 점이 없거나 0 이면 null. sameBase 는 앞 점과 공시 기준연월이 같은데 값이 다른 경우(정정 등, 확인 필요).
 * 값은 자료 그대로이며 판단이 아니다. 공시지가가 없는 기준일은 점으로 만들지 않는다.
 */
export function priceTrend(attrs, history) {
  if (!attrs || typeof attrs !== "object") return [];
  const feats = (Array.isArray(history) ? history : []).filter((h) => h && h.kind === "land_feature" && h.changes && typeof h.as_of === "string")
    .slice().sort((a, b) => (a.as_of < b.as_of ? -1 : a.as_of > b.as_of ? 1 : 0));
  let state = Object.fromEntries(PRICE_FIELDS.map((k) => [k, attrs[k] ?? null]));
  const pts = [];
  if (!feats.length) {
    if (Number.isFinite(state.official_land_price_krw_m2)) pts.push({ as_of: attrs.as_of ?? null, ...state, first: false, earlier: false });
  } else {
    for (let i = feats.length - 1; i >= 0; i--) {
      const h = feats[i];
      if (h.first || PRICE_FIELDS.some((k) => k in h.changes)) pts.push({ as_of: h.as_of, ...state, first: !!h.first, earlier: false });
      const before = { ...state };
      for (const k of PRICE_FIELDS) if (k in h.changes) before[k] = h.changes[k].from ?? null;
      state = before;
    }
    // 가장 앞의 남은 항목이 '처음 확인' 이 아니면 파생본 이력 상한으로 앞부분이 잘린 것이다. 그 이전 값은 기준일을 모른다
    if (!feats[0].first && PRICE_FIELDS.some((k) => k in feats[0].changes)) pts.push({ as_of: null, ...state, first: false, earlier: true });
  }
  const out = pts.reverse().filter((q) => Number.isFinite(q.official_land_price_krw_m2)).map((q) => ({
    as_of: q.as_of, price: q.official_land_price_krw_m2, year: q.price_base_year ?? null, month: q.price_base_month ?? null, first: q.first, earlier: q.earlier,
  }));
  // 같은 값·같은 기준연월이 이어지면 하나로 (값이 바뀐 지점만 보인다)
  const dedup = out.filter((q, i) => i === 0 || !(q.price === out[i - 1].price && q.year === out[i - 1].year && q.month === out[i - 1].month));
  return dedup.map((q, i) => {
    const prev = i ? dedup[i - 1] : null;
    return { ...q, deltaPct: prev && prev.price > 0 ? ((q.price - prev.price) / prev.price) * 100 : null,
             sameBase: !!prev && prev.year != null && prev.year === q.year && prev.month === q.month };
  });
}

/** 추이 한 점을 화면용 글 [기준, 값, 증감, 확인]. */
export function priceTrendRow(q) {
  const base = q.year ? `${q.year}년${q.month ? ` ${q.month}월` : ""} 기준` : "기준연월 미확인";
  const delta = q.deltaPct == null ? "" : `${q.deltaPct > 0 ? "+" : ""}${q.deltaPct.toFixed(1)}%`;
  const seen = q.earlier ? "이전 자료 (기준일 모름)" : q.as_of ? `확인 ${q.as_of}` : "기준일 미확인";
  return [base, `${fmtInt(q.price)}원/㎡`, delta, seen + (q.sameBase ? " · 같은 기준연월의 값이 바뀜 (정정 여부 확인)" : "")];
}

/** 번들의 속성 요약: 속성 있는 필지 수와 용도지역별 수. */
export function attrsSummary(bundle) {
  const feats = bundle?.features ?? [];
  const byZone = new Map();
  let withAttrs = 0;
  for (const f of feats) {
    const a = f.properties?.attrs;
    if (!a) continue;
    withAttrs++;
    const c = zoneCategory(a.use_zone_1) ?? "none";
    byZone.set(c, (byZone.get(c) ?? 0) + 1);
  }
  return { total: feats.length, withAttrs, byZone, sources: bundle?.attrs_sources ?? [] };
}

/** 폴리곤 목록 [[outer, hole...], ...] 로 정규화. */
export function polygonsOf(feature) {
  const g = feature.geometry;
  return g.type === "Polygon" ? [g.coordinates] : g.coordinates;
}

function ringArea(ring) {
  let s = 0;
  for (let i = 0; i < ring.length - 1; i++) s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
  return s / 2;
}

/** 라벨 위치: 가장 큰 조각의 외곽 링 면적 중심. 오목한 필지에서는 밖으로 나갈 수 있어 bbox 중심으로 보정하지 않는다(라벨은 근처면 충분). */
export function labelPoint(feature) {
  let best = null, bestArea = -1;
  for (const poly of polygonsOf(feature)) {
    const ring = poly[0];
    const a = Math.abs(ringArea(ring));
    if (a > bestArea) { bestArea = a; best = ring; }
  }
  if (!best) return null;
  // 경위도 절대값(127 등)에 비해 필지가 아주 작아 상쇄 오차가 크다. 첫 꼭짓점 기준 상대 좌표로 계산한다.
  const [ox, oy] = best[0];
  const rel = best.map(([x, y]) => [x - ox, y - oy]);
  const a = ringArea(rel);
  if (Math.abs(a) < 1e-18) return best[0].slice();
  let cx = 0, cy = 0;
  for (let i = 0; i < rel.length - 1; i++) {
    const w = rel[i][0] * rel[i + 1][1] - rel[i + 1][0] * rel[i][1];
    cx += (rel[i][0] + rel[i + 1][0]) * w;
    cy += (rel[i][1] + rel[i + 1][1]) * w;
  }
  return [ox + cx / (6 * a), oy + cy / (6 * a)];
}

function inRing([x, y], ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** 점(경도, 위도)이 필지 안인지. 구멍 안이면 밖이다. */
export function pointInFeature(pt, feature) {
  const b = feature.properties?.bbox;
  if (b && (pt[0] < b[0] || pt[0] > b[2] || pt[1] < b[1] || pt[1] > b[3])) return false;
  for (const poly of polygonsOf(feature)) {
    if (!inRing(pt, poly[0])) continue;
    if (poly.slice(1).some((hole) => inRing(pt, hole))) continue;
    return true;
  }
  return false;
}

/** 위치점이 이 필지 안에 있는 물건들 (위치로만 찾는다). */
export function assetsInParcel(feature, assets) {
  return assets.filter((a) => Array.isArray(a.location_point) && a.location_point.length === 2 && pointInFeature(a.location_point, feature));
}

/** 이 필지의 물건: 정본 연결(asset_ids, 파생본에만 있음)과 위치점 포함을 합친다. 각 항목에 근거(linked / inside / both)를 붙인다.
 *  linksValid 가 false 면(번들을 넣은 뒤 시드가 바뀜) 정본 연결은 쓰지 않는다: 오래된 연결로 엉뚱한 물건에 관측이 붙지 않게 한다. */
export function parcelAssets(feature, assets, { linksValid = true } = {}) {
  const linked = new Set(linksValid ? (feature.properties?.asset_ids ?? []) : []);
  const inside = new Set(assetsInParcel(feature, assets).map((a) => a.asset_id));
  return assets.filter((a) => linked.has(a.asset_id) || inside.has(a.asset_id))
    .map((a) => ({ asset: a, basis: linked.has(a.asset_id) && inside.has(a.asset_id) ? "both" : linked.has(a.asset_id) ? "linked" : "inside" }));
}
export const BASIS_LABEL = { both: "정본 연결 · 위치점 포함", linked: "정본 연결", inside: "위치점 포함 (연결 미확정)" };

export function parcelTitle(props) {
  return `${props.emd_name ?? props.emd_code} ${props.label}`;
}

export function fmtArea(m2, reason) {
  return Number.isFinite(m2) ? `${m2.toFixed(1)} ㎡` : `미확인 (${reason ?? "사유 없음"})`;
}

// ---- 필지 찾기 (J5-027): 지번·PNU 검색과 위치로 찾기. 순수 함수, 외부 통신 없음. ----

/** 검색어 해석: "182-13", "산1-2", "종로5가 182-13", "가상동 1", 19자리 PNU. 못 읽으면 null. */
export function parseParcelQuery(q) {
  const s = String(q ?? "").trim().replace(/\s+/g, " ");
  if (!s) return null;
  if (/^\d{19}$/.test(s)) return { pnu: s };
  const core = /^(산)?\s*(\d{1,4})(?:\s*-\s*(\d{1,4}))?(?:\s*[가-힣]{1,2})?$/;
  let emd = null, m = core.exec(s);
  if (!m) {   // 앞에 동 이름이 있으면 첫 공백에서 나눈다 (예: "종로5가 182-13", "가상동 산1-2")
    const i = s.indexOf(" ");
    if (i < 0) return null;
    emd = s.slice(0, i);
    m = core.exec(s.slice(i + 1).trim());
    if (!m) return null;
  }
  return { emd, mountain: !!m[1], bon: Number(m[2]), bu: m[3] != null ? Number(m[3]) : null };
}

/** 검색어에 맞는 필지. 부번을 안 적으면 부번 0 을 먼저, 없으면 같은 본번 전체(부번 순). 동 이름은 앞부분 일치. 결과는 emd·bon·bu 순, 최대 limit. */
export function findParcels(features, q, { limit = 20 } = {}) {
  const query = parseParcelQuery(q);
  if (!query || !Array.isArray(features)) return { query, matches: [] };
  let matches;
  if (query.pnu) matches = features.filter((f) => f.id === query.pnu);
  else {
    const emdOk = (p) => !query.emd || String(p.emd_name ?? "").startsWith(query.emd) || String(p.emd_code ?? "").startsWith(query.emd);
    const base = features.filter((f) => { const p = f.properties ?? {}; return emdOk(p) && !!p.mountain === query.mountain && p.bon === query.bon; });
    if (query.bu != null) matches = base.filter((f) => f.properties.bu === query.bu);
    else {
      const exact = base.filter((f) => f.properties.bu === 0);
      matches = exact.length ? exact : base;
    }
  }
  const key = (f) => [f.properties?.emd_name ?? "", f.properties?.bon ?? 0, f.properties?.bu ?? 0];
  matches = [...matches].sort((a, b) => { const [ea, ba, ua] = key(a), [eb, bb, ub] = key(b); return ea.localeCompare(eb) || ba - bb || ua - ub; });
  return { query, matches: matches.slice(0, limit), total: matches.length };
}

/** 위치(경도, 위도)가 들어 있는 필지 (첫 것). 없으면 null. */
export function parcelAt(features, lonlat) {
  if (!Array.isArray(features) || !Array.isArray(lonlat)) return null;
  for (const f of features) if (pointInFeature(lonlat, f)) return f;
  return null;
}

/** 필지 안의 점 하나 (기기가 만든 물건의 위치점, J5-028). 라벨 위치가 안이면 그것, 아니면 bbox 격자에서 처음 찾은 안쪽 점, 없으면 null. */
export function interiorPoint(feature) {
  const lp = labelPoint(feature);
  if (lp && pointInFeature(lp, feature)) return [Number(lp[0].toFixed(7)), Number(lp[1].toFixed(7))];
  const b = feature.properties?.bbox;
  if (!isBbox(b)) return null;
  for (const n of [5, 11, 23]) {
    for (let i = 1; i < n; i++) for (let j = 1; j < n; j++) {
      const p = [b[0] + ((b[2] - b[0]) * i) / n, b[1] + ((b[3] - b[1]) * j) / n];
      if (pointInFeature(p, feature)) return [Number(p[0].toFixed(7)), Number(p[1].toFixed(7))];
    }
  }
  return null;
}
