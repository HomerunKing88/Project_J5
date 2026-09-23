// 필지 번들(.j5parcels.json, ADR-13) 검증과 기하 유틸. schemas/parcels_bundle.schema.json 과 같은 규칙의 핵심 부분.
// PNU 는 필지 외부 ID 이고 asset_id 가 아니다. 도형면적(area_m2_geom)은 공부면적이 아니다. 외부 통신 없음.

export const PARCELS_FORMAT = "1.0.0";
export const MAX_PARCELS = 8000;
const PNU_RE = /^[0-9]{19}$/;
const TOP_ALLOWED = new Set(["type", "j5parcels", "data_mode", "generated_at", "source", "clip", "count", "bbox", "warnings", "features"]);
const PROP_REQUIRED = ["pnu", "label", "emd_code", "emd_name", "mountain", "bon", "bu", "jimok", "jibun_raw", "jibun_mismatch", "area_m2_geom", "bbox"];

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
    if (typeof p.area_m2_geom !== "number" || p.area_m2_geom < 0) errs.push(`${at} area_m2_geom`);
  }
  return errs;
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

/** 위치점이 이 필지 안에 있는 물건들. PNU↔asset_id 연결은 정본(R2) 몫이고 여기서는 위치로만 찾는다. */
export function assetsInParcel(feature, assets) {
  return assets.filter((a) => Array.isArray(a.location_point) && a.location_point.length === 2 && pointInFeature(a.location_point, feature));
}

export function parcelTitle(props) {
  return `${props.emd_name ?? props.emd_code} ${props.label}`;
}

export function fmtArea(m2) {
  return Number.isFinite(m2) ? `${m2.toFixed(1)} ㎡` : "-";
}
