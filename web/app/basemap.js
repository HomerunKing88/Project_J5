// 배경 도형 번들(.j5basemap.json, ADR-16, J5-022) 검증과 도로명 라벨 배치. schemas/basemap_bundle.schema.json 과 같은 규칙의 핵심 부분.
// 배경은 지도의 보조 층이다. 정본이 아니고 물건·필지 연결이 없으며 도형과 도로명만 담는다. 외부 통신 없음.

export const BASEMAP_FORMAT = "1.0.0";
export const MAX_BASEMAP = 20000;
export const LAYERS = ["building", "road_area", "road"];
export const LAYER_LABEL = { building: "건물", road_area: "실폭도로", road: "도로 중심선" };
export const ROAD_LABEL_CHAR_PX = 10; // 도로명 글자 하나의 대략 폭(px)
export const ROAD_LABEL_PAD_PX = 24;  // 라벨이 선분보다 이만큼 짧아야 붙인다
const TOP_ALLOWED = new Set(["type", "j5basemap", "data_mode", "generated_at", "sources", "clip", "count", "counts", "bbox", "warnings", "features"]);
const ID_RE = /^(building|road_area|road):[0-9]{1,9}$/;

const isLonLat = (p) => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite) && p[0] >= -180 && p[0] <= 180 && p[1] >= -90 && p[1] <= 90;
const isRing = (r) => Array.isArray(r) && r.length >= 4 && r.every(isLonLat);
const isPolygon = (p) => Array.isArray(p) && p.length >= 1 && p.every(isRing);
const isLine = (l) => Array.isArray(l) && l.length >= 2 && l.every(isLonLat);
const isBbox = (b) => Array.isArray(b) && b.length === 4 && b.every(Number.isFinite) && b[0] <= b[2] && b[1] <= b[3];

/** 문제 목록. 빈 배열이면 유효. 앞 몇 건만 모으고 큰 파일에서 멈추지 않는다. */
export function validateBasemap(doc) {
  const errs = [];
  if (doc === null || typeof doc !== "object" || Array.isArray(doc)) return ["객체가 아님"];
  for (const k of Object.keys(doc)) if (!TOP_ALLOWED.has(k)) errs.push(`허용되지 않은 필드 ${k}`);
  if (doc.type !== "FeatureCollection") errs.push("type 은 FeatureCollection");
  if (doc.j5basemap !== BASEMAP_FORMAT) errs.push(`j5basemap 형식 버전은 ${BASEMAP_FORMAT} (파일: ${doc.j5basemap})`);
  if (!["synthetic", "real"].includes(doc.data_mode)) errs.push("data_mode 는 synthetic 또는 real");
  if (typeof doc.generated_at !== "string") errs.push("generated_at 누락");
  if (!Array.isArray(doc.sources) || !doc.sources.length || doc.sources.length > 3) errs.push("sources 는 1~3개");
  else {
    const seen = new Set();
    for (const s of doc.sources) {
      if (!s || typeof s !== "object") { errs.push("sources 항목 형식"); continue; }
      if (!LAYERS.includes(s.layer)) errs.push(`sources.layer ${s.layer}`);
      if (seen.has(s.layer)) errs.push(`같은 층이 두 번: ${s.layer}`);
      seen.add(s.layer);
      for (const k of ["name", "file", "shp_sha256", "dbf_sha256", "crs", "encoding", "record_count", "geometry_version", "license", "fields", "name_field"]) if (!(k in s)) errs.push(`sources.${k} 누락`);
      if (typeof s.geometry_version !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(s.geometry_version)) errs.push("sources.geometry_version 은 YYYY-MM-DD");
    }
  }
  if (!doc.clip || !isBbox(doc.clip.bbox)) errs.push("clip.bbox 누락");
  if (!doc.counts || typeof doc.counts !== "object" || !LAYERS.every((k) => Number.isInteger(doc.counts[k]) && doc.counts[k] >= 0)) errs.push("counts 형식");
  if (!Array.isArray(doc.features)) { errs.push("features 가 배열이 아님"); return errs; }
  if (doc.features.length > MAX_BASEMAP) errs.push(`배경 도형이 ${MAX_BASEMAP}개를 넘음 (${doc.features.length})`);
  if (doc.count !== doc.features.length) errs.push(`count(${doc.count})와 features 수(${doc.features.length})가 다름`);
  if (doc.bbox !== null && !isBbox(doc.bbox)) errs.push("bbox 형식");
  const tally = { building: 0, road_area: 0, road: 0 };
  const ids = new Set();
  for (let i = 0; i < doc.features.length && errs.length < 20; i++) {
    const f = doc.features[i];
    const at = `features[${i}]`;
    if (!f || typeof f !== "object" || f.type !== "Feature") { errs.push(`${at} Feature 가 아님`); continue; }
    if (!ID_RE.test(f.id)) errs.push(`${at} id 형식`);
    else if (ids.has(f.id)) errs.push(`${at} id 중복 ${f.id}`);
    ids.add(f.id);
    const p = f.properties;
    if (!p || typeof p !== "object") { errs.push(`${at} properties 누락`); continue; }
    for (const k of ["layer", "name", "bbox"]) if (!(k in p)) errs.push(`${at} properties.${k} 누락`);
    for (const k of Object.keys(p)) if (!["layer", "name", "bbox"].includes(k)) errs.push(`${at} properties.${k} 는 허용되지 않음 (도형·도로명만)`);
    if (!LAYERS.includes(p.layer)) { errs.push(`${at} layer`); continue; }
    if (f.id && !String(f.id).startsWith(p.layer + ":")) errs.push(`${at} id 와 layer 가 다름`);
    if (p.name !== null && (typeof p.name !== "string" || p.name.length > 60)) errs.push(`${at} name`);
    if (p.layer !== "road" && p.name !== null) errs.push(`${at} 도로 중심선만 이름을 가진다`);
    if (!isBbox(p.bbox)) errs.push(`${at} bbox`);
    const g = f.geometry;
    if (!g || typeof g !== "object") { errs.push(`${at} geometry 누락`); continue; }
    if (p.layer === "road") {
      if (g.type === "LineString" ? !isLine(g.coordinates) : g.type === "MultiLineString" ? !(Array.isArray(g.coordinates) && g.coordinates.length >= 1 && g.coordinates.every(isLine)) : true) errs.push(`${at} 도로 중심선은 LineString/MultiLineString`);
    } else if (g.type === "Polygon" ? !isPolygon(g.coordinates) : g.type === "MultiPolygon" ? !(Array.isArray(g.coordinates) && g.coordinates.length >= 1 && g.coordinates.every(isPolygon)) : true) errs.push(`${at} 건물·실폭도로는 Polygon/MultiPolygon`);
    tally[p.layer] += 1;
  }
  if (!errs.length && doc.counts) for (const k of LAYERS) if (doc.counts[k] !== tally[k]) errs.push(`counts.${k}(${doc.counts[k]})와 실제 수(${tally[k]})가 다름`);
  return errs;
}

/** 폴리곤 목록 [[outer, hole...], ...] 또는 선 목록 [[pt...], ...] 로 정규화. */
export function partsOf(feature) {
  const g = feature.geometry;
  if (g.type === "Polygon") return [g.coordinates];
  if (g.type === "LineString") return [g.coordinates];
  return g.coordinates;
}

/** 선 파트 목록(세계 좌표 {x,y} 배열들)에서 가장 긴 파트의 길이 절반 지점과 그 선분의 각도(도, -90~90 로 정규화). 길이 0 이면 null. */
export function lineMidpoint(parts) {
  let best = null;
  for (const part of parts) {
    let len = 0;
    const segs = [];
    for (let i = 1; i < part.length; i++) {
      const d = Math.hypot(part[i].x - part[i - 1].x, part[i].y - part[i - 1].y);
      segs.push(d);
      len += d;
    }
    if (len > 0 && (!best || len > best.length)) best = { part, segs, length: len };
  }
  if (!best) return null;
  let acc = 0;
  for (let i = 0; i < best.segs.length; i++) {
    const d = best.segs[i];
    if (acc + d >= best.length / 2) {
      const t = d > 0 ? (best.length / 2 - acc) / d : 0;
      const a = best.part[i], b = best.part[i + 1];
      let angle = (Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI;
      if (angle > 90) angle -= 180;
      if (angle < -90) angle += 180;
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, angle, length: best.length };
    }
    acc += d;
  }
  return null;
}

export const ROAD_LABEL_H_PX = 14; // 라벨 높이(px), 겹침 판정용

/** 회전한 라벨 사각형(중심 cx,cy, 폭 w, 높이 h, 각도 deg)의 화면 축 정렬 경계 상자. */
export function labelBox(cx, cy, w, h, angleDeg = 0) {
  const a = (angleDeg * Math.PI) / 180, c = Math.abs(Math.cos(a)), s = Math.abs(Math.sin(a));
  const hw = (w * c + h * s) / 2, hh = (w * s + h * c) / 2;
  return { x0: cx - hw, y0: cy - hh, x1: cx + hw, y1: cy + hh };
}

const boxesOverlap = (a, b) => !(a.x1 < b.x0 || b.x1 < a.x0 || a.y1 < b.y0 || b.y1 < a.y0);

/**
 * 도로명 라벨을 붙일 도로 id 집합. 같은 이름은 화면 안에서 가장 긴 것 하나에만 붙이고,
 * 화면 길이가 라벨 폭 + 여백보다 짧으면 붙이지 않으며, 다른 이름끼리도 라벨 사각형이 겹치면 긴 쪽만 남긴다.
 * roads: [{id, name, mid:{x,y,angle,length}}] (세계 좌표), view: {scale,tx,ty}.
 */
export function pickRoadLabels(roads, view, w, h, margin = 0) {
  const byName = new Map();
  for (const r of roads) {
    if (!r.name || !r.mid) continue;
    const px = r.mid.length * view.scale;
    const labelPx = Array.from(r.name).length * ROAD_LABEL_CHAR_PX;
    if (px < labelPx + ROAD_LABEL_PAD_PX) continue;
    const sx = r.mid.x * view.scale + view.tx, sy = r.mid.y * view.scale + view.ty;
    if (sx < -margin || sx > w + margin || sy < -margin || sy > h + margin) continue;
    const cur = byName.get(r.name);
    if (!cur || px > cur.px) byName.set(r.name, { id: r.id, px, box: labelBox(sx, sy, labelPx, ROAD_LABEL_H_PX, r.mid.angle ?? 0) });
  }
  const placed = [];
  const out = new Set();
  for (const c of [...byName.values()].sort((a, b) => b.px - a.px || (a.id < b.id ? -1 : 1))) {
    if (placed.some((b) => boxesOverlap(b, c.box))) continue;
    placed.push(c.box);
    out.add(c.id);
  }
  return out;
}
