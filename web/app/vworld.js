// 온라인 필지 조회 (J5-080, ADR-27). 지도를 누른 자리의 필지를 VWorld 데이터 API 에서 받아 경계·지번·지목·공시지가·용도지역을 보인다.
// VWorld 데이터 API 는 다른 출처 요청을 허용하지 않아(폰 실측 2026-10-14) 격리 프레임(vworld-frame.html, sandbox="allow-scripts")이
// JSONP 로 부르고 결과 값만 돌려준다. 이 모듈은 프레임과 주고받기(createVworldBridge)와 응답 해석(순수 함수)을 맡는다.
// 응답 필드 이름은 폰 실측으로 확인했다: features[0].properties 의 pnu·addr·jibun·bonbun·bubun·jiga·gosi_year·gosi_month, geometry 는 경위도 MultiPolygon.
// 용도지역(LT_C_UQ111)은 폰 실측(2026-10-14)으로 uname(용도지역 이름)·dyear·dnum(고시 연도·번호)·sido_name·sigg_name 을 확인했다. 한 자리에 결과가 여럿 올 수 있다.
// 화면 범위의 필지(J5-081)는 같은 연속지적 자료를 범위(BOX)로 부른다.

export const DATASETS = Object.freeze({ parcel: "LP_PA_CBND_BUBUN", zoning: "LT_C_UQ111" });
export const MAX_VERTICES = 20000;      // 한 필지 경계의 꼭짓점 상한 (넘으면 그리지 않는다)
export const MAX_RAW_PROPS = 12;        // 원문으로 보이는 속성 수 상한
export const MAX_BOX_DEG = 0.02;        // 범위 조회 한 변 상한 (vworld-frame.js 와 같다)
const KOREA = { lon: [124, 132], lat: [33, 39.5] };
const M2_PER_PYEONG = 3.305785;

const inKorea = (c) => Array.isArray(c) && c.length >= 2 && Number.isFinite(c[0]) && Number.isFinite(c[1]) &&
  c[0] > KOREA.lon[0] && c[0] < KOREA.lon[1] && c[1] > KOREA.lat[0] && c[1] < KOREA.lat[1];

/** 누른 자리가 조회할 수 있는 범위(한국)인가 */
export function canLookup(lonlat) {
  return inKorea(lonlat);
}

/** VWorld 응답의 상태와 첫 결과 목록. status: OK·NOT_FOUND·ERROR, 형식이 다르면 bad_response */
function featuresOf(doc) {
  const r = doc?.response;
  if (!r || typeof r !== "object") return { status: "bad_response", features: [] };
  if (r.status === "NOT_FOUND") return { status: "NOT_FOUND", features: [] };
  if (r.status !== "OK") return { status: "ERROR", error: typeof r.error?.text === "string" ? r.error.text.slice(0, 200) : null, features: [] };
  const fs = r.result?.featureCollection?.features;
  return Array.isArray(fs) ? { status: "OK", features: fs } : { status: "bad_response", features: [] };
}

/** 경위도 다각형 목록(MultiPolygon 좌표). 형식이 다르거나 범위 밖·꼭짓점이 너무 많으면 null */
export function polygonsOfGeometry(g) {
  if (!g || (g.type !== "Polygon" && g.type !== "MultiPolygon") || !Array.isArray(g.coordinates)) return null;
  const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;
  let n = 0;
  for (const poly of polys) {
    if (!Array.isArray(poly) || !poly.length) return null;
    for (const ring of poly) {
      if (!Array.isArray(ring) || ring.length < 4) return null;
      for (const c of ring) { if (!inKorea(c)) return null; n += 1; }
    }
  }
  return n > 0 && n <= MAX_VERTICES ? polys : null;
}

/** 다각형 목록의 경위도 범위 [서, 남, 동, 북] */
export function bboxOfPolygons(polys) {
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  for (const poly of polys) for (const c of poly[0]) { w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]); }
  return [w, s, e, n];
}

/** 도형 면적(㎡). 각 다각형의 중심 위도로 경위도를 미터로 펴서 신발끈 공식으로 잰다(구멍은 뺀다). 수 ㎡ 오차가 있는 지도 계산값이며 공부면적이 아니다 */
export function polygonAreaM2(polys) {
  const R = 6371008.8;
  let total = 0;
  for (const poly of polys) {
    const lat0 = poly[0].reduce((a, c) => a + c[1], 0) / poly[0].length;
    const kx = (Math.PI / 180) * R * Math.cos((lat0 * Math.PI) / 180), ky = (Math.PI / 180) * R;
    poly.forEach((ring, i) => {
      let a = 0;
      for (let j = 0, k = ring.length - 1; j < ring.length; k = j++) a += ring[k][0] * kx * (ring[j][1] * ky) - ring[j][0] * kx * (ring[k][1] * ky);
      total += (i === 0 ? 1 : -1) * Math.abs(a / 2);
    });
  }
  return Math.max(0, total);
}

/** 지번 글 → { lot: "182-13" 또는 "산 1-2", jimok: 뒤에 붙은 지목 글자 또는 null }. 읽지 못하면 lot 에 원문 */
export function splitJibun(jibun) {
  const s = String(jibun ?? "").trim();
  const m = /^(산\s*)?(\d+(?:-\d+)?)\s*(\D.*)?$/.exec(s);
  if (!m) return { lot: s || null, jimok: null };
  return { lot: (m[1] ? "산 " : "") + m[2], jimok: m[3] ? m[3].trim() || null : null };
}

const num = (v) => {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v !== "string" || !/^\s*-?\d+(\.\d+)?\s*$/.test(v)) return null;
  return Number(v);
};
const str = (v, max = 200) => (typeof v === "string" && v.trim() ? v.trim().slice(0, max) : null);

/**
 * 연속지적(LP_PA_CBND_BUBUN) 응답 → 필지 하나.
 * { status: "OK", parcel: { pnu, addr, jibun, lot, jimok, jiga, gosi, areaM2, polygons, bbox, feature } } 또는 { status: NOT_FOUND·ERROR·bad_response, error }
 * feature 는 앱의 필지 함수(내부 점·필지 안 판정·대상 만들기)가 쓰는 모양이다.
 */
export function parseParcelResponse(doc) {
  const { status, features, error } = featuresOf(doc);
  if (status !== "OK") return { status, error: error ?? null };
  const f = features[0];
  const p = f?.properties ?? {};
  const pnu = typeof p.pnu === "string" && /^\d{19}$/.test(p.pnu) ? p.pnu : null;
  const polygons = polygonsOfGeometry(f?.geometry);
  if (!pnu || !polygons) return { status: "bad_response", error: !pnu ? "pnu" : "geometry" };
  const { lot, jimok } = splitJibun(p.jibun);
  const y = num(p.gosi_year), mo = num(p.gosi_month);
  const jiga = num(p.jiga);
  const addr = str(p.addr);
  const bbox = bboxOfPolygons(polygons);
  const areaM2 = polygonAreaM2(polygons);
  const label = lot ?? pnu;
  const feature = { type: "Feature", id: pnu, geometry: { type: "MultiPolygon", coordinates: polygons },
    properties: { label, emd_name: addr ? addr.replace(/\s*\S+$/, "").split(" ").slice(-1)[0] || null : null, emd_code: pnu.slice(0, 10), bbox, jimok, area_m2_geom: areaM2 } };
  return { status: "OK", parcel: { pnu, addr, jibun: str(p.jibun), lot, jimok, jiga: jiga !== null && jiga >= 0 ? jiga : null,
    gosi: y && mo ? `${y}-${String(mo).padStart(2, "0")}` : (y ? String(y) : null), areaM2, polygons, bbox, feature } };
}

/** 용도지역 응답 → 결과마다 이름 그대로의 속성 목록 [[이름, 값], ...] (글·숫자 값만, 상한 있음). 필드 뜻은 짐작하지 않는다 */
export function parseZoningResponse(doc) {
  const { status, features, error } = featuresOf(doc);
  if (status !== "OK") return { status, error: error ?? null, items: [] };
  const items = features.slice(0, 5).map((f) => Object.entries(f?.properties ?? {})
    .filter(([k, v]) => /^[A-Za-z0-9_]{1,40}$/.test(k) && (typeof v === "string" ? v.trim() : Number.isFinite(v)))
    .slice(0, MAX_RAW_PROPS).map(([k, v]) => [k, String(v).trim().slice(0, 100)]));
  return { status: "OK", items: items.filter((it) => it.length) };
}

/** 용도지역 결과들 → 한 줄 요약 "일반상업지역 (2023 고시)". uname 이 있는 결과만, 같은 이름은 한 번. 없으면 null */
export function zoningSummary(items) {
  const seen = new Map();
  for (const props of items ?? []) {
    const o = Object.fromEntries(props);
    const name = typeof o.uname === "string" && o.uname.trim() ? o.uname.trim() : null;
    if (!name || seen.has(name)) continue;
    seen.set(name, /^\d{4}$/.test(o.dyear ?? "") ? `${name} (${o.dyear} 고시)` : name);
  }
  return seen.size ? [...seen.values()].join(", ") : null;
}

/**
 * 조회 결과 → 대상에 남길 기록 (J5-084). 이 폰의 대상(device_assets)에만 두고 내보내기에는 싣지 않는다.
 * 값은 조회 그때의 VWorld 값이며 없으면 null. area_m2 는 경계로 계산한 도형면적(공부면적 아님). feature 는 넣지 않는다.
 */
export function lookupSnapshot(p, zoning, lookedUpAt) {
  return { source: `VWorld ${DATASETS.parcel}, ${DATASETS.zoning}`, looked_up_at: lookedUpAt, pnu: p.pnu, addr: p.addr ?? null, lot: p.lot ?? null, jimok: p.jimok ?? null,
    jiga: p.jiga ?? null, gosi: p.gosi ?? null, area_m2: Number.isFinite(p.areaM2) ? p.areaM2 : null,
    zoning: zoning?.status === "OK" ? zoningSummary(zoning.items) : null, zoning_status: zoning?.status ?? "ERROR", zoning_error: zoning?.status === "OK" ? null : (zoning?.error ?? null),
    polygons: p.polygons, bbox: p.bbox };
}

/** 조회 기록 → 카드 줄 [[이름, 값], ...]. rawBelow: 용도지역 원문을 카드 아래에 보일 때 (조회 카드) */
export function lookupFacts(s, { rawBelow = false } = {}) {
  if (!s) return [];
  const facts = [
    ["지번", s.lot ?? "미확인"],
    ["지목", s.jimok ?? "미확인"],
    ["공시지가", s.jiga !== null ? `${fmtJiga(s.jiga)}${s.gosi ? ` (${s.gosi} 기준)` : ""}` : "미확인"],
    ["도형면적", s.area_m2 !== null ? `${fmtAreaPyeong(s.area_m2)} · 경계로 계산, 공부면적 아님` : "미확인"],
  ];
  if (s.zoning_status === "OK") facts.splice(2, 0, ["용도지역", s.zoning ?? (rawBelow ? "자료에 이름 없음 (아래 원문)" : "자료에 이름 없음")]);
  else facts.push(["용도지역", s.zoning_status === "NOT_FOUND" ? "이 자리에 자료 없음" : `조회 실패 (${s.zoning_error ?? s.zoning_status})`]);
  return facts;
}

/** 범위(BOX)로 받은 연속지적 응답 → 필지 목록 [{ pnu, lot, polygons, bbox }] (형식이 맞지 않는 필지는 건너뛴다)와 전체 수.
 *  truncated: VWorld 의 전체 수가 받은 수보다 많아 일부만 받았는지 */
export function parseParcelsInBox(doc) {
  const { status, features, error } = featuresOf(doc);
  if (status === "NOT_FOUND") return { status: "OK", parcels: [], total: 0, truncated: false };
  if (status !== "OK") return { status, error: error ?? null, parcels: [], total: 0, truncated: false };
  const parcels = [];
  for (const f of features) {
    const p = f?.properties ?? {};
    if (typeof p.pnu !== "string" || !/^\d{19}$/.test(p.pnu)) continue;
    const polygons = polygonsOfGeometry(f?.geometry);
    if (!polygons) continue;
    parcels.push({ pnu: p.pnu, lot: splitJibun(p.jibun).lot, polygons, bbox: bboxOfPolygons(polygons) });
  }
  const total = num(doc?.response?.record?.total);
  return { status: "OK", parcels, total: total ?? parcels.length, truncated: total !== null && total > features.length };
}

/** 화면 범위의 필지(J5-081): 보이는 범위 → 받을 범위. 사방 25% 여유를 더하고, 격리 프레임의 한 변 상한(0.02°)에 맞춰 가운데 기준으로 줄인다 */
export function aroundQueryBox(vb, pad = 0.25, maxSpan = MAX_BOX_DEG) {
  const cx = (vb[0] + vb[2]) / 2, cy = (vb[1] + vb[3]) / 2;
  const hw = Math.min(((vb[2] - vb[0]) * (1 + 2 * pad)) / 2, maxSpan / 2 - 2e-6), hh = Math.min(((vb[3] - vb[1]) * (1 + 2 * pad)) / 2, maxSpan / 2 - 2e-6);   // 소수 6자리 반올림 뒤에도 한 변이 상한 안에 들게 (넘으면 격리 프레임이 거절한다)
  return [cx - hw, cy - hh, cx + hw, cy + hh].map((v) => Number(v.toFixed(6)));
}

/** 찾기 글 → VWorld 주소 검색어. 앞뒤 공백·겹친 공백을 줄이고, 검색에 쓸 수 없는 글자(꺾쇠·따옴표·제어문자)는 빼며 80자로 자른다. 빈 글이면 null */
export function searchQuery(q) {
  const s = String(q ?? "").replace(/[\u0000-\u001f<>"'`\\]/g, " ").replace(/\s+/g, " ").trim().slice(0, 80);
  return s || null;
}

/**
 * 주소 검색 응답 → [{ title, lonlat }] (J5-082). 결과 목록은 response.result.items, 자리는 point.x·point.y(경위도), 이름은 address.parcel·address.road 를 읽는다.
 * 이 필드 이름은 아직 폰 실측 전이다. 다르면 결과를 읽지 못해 빈 목록이 되고(자리를 짐작하지 않는다), 찾은 자리의 필지 정보는 실측한 데이터 API 로 다시 조회한다.
 */
export function parseSearchResponse(doc) {
  const r = doc?.response;
  if (!r || typeof r !== "object") return { status: "bad_response", items: [] };
  if (r.status === "NOT_FOUND") return { status: "OK", items: [] };
  if (r.status !== "OK") return { status: "ERROR", error: typeof r.error?.text === "string" ? r.error.text.slice(0, 200) : null, items: [] };
  const raw = Array.isArray(r.result?.items) ? r.result.items : [];
  const items = [];
  for (const it of raw.slice(0, 10)) {
    const lon = num(it?.point?.x), lat = num(it?.point?.y);
    if (!inKorea([lon, lat])) continue;
    const title = str(it?.address?.parcel) ?? str(it?.address?.road) ?? str(it?.title) ?? null;
    items.push({ title: title ?? `${lat.toFixed(5)}, ${lon.toFixed(5)}`, lonlat: [lon, lat] });
  }
  return { status: "OK", items };
}

/** 원/㎡ → "1,234만 원/㎡ · 4,079만 원/평" 처럼 */
export function fmtJiga(wonPerM2) {
  if (!Number.isFinite(wonPerM2)) return null;
  const man = (v) => (v >= 10000 ? `${Math.round(v / 10000).toLocaleString("ko-KR")}만 원` : `${Math.round(v).toLocaleString("ko-KR")} 원`);
  return `${man(wonPerM2)}/㎡ · ${man(wonPerM2 * M2_PER_PYEONG)}/평`;
}

/** ㎡ → "1,234.5㎡ (373.4평)" */
export function fmtAreaPyeong(m2) {
  if (!Number.isFinite(m2)) return null;
  return `${m2.toLocaleString("ko-KR", { maximumFractionDigits: 1 })}㎡ (${(m2 / M2_PER_PYEONG).toLocaleString("ko-KR", { maximumFractionDigits: 1 })}평)`;
}

/**
 * 격리 프레임과 주고받기. 프레임 하나를 숨겨 두고 요청마다 번호를 붙여 보내며, 그 프레임에서 온 응답만 받는다.
 * request(data, [lon, lat] 또는 { box }, key, domain, { size }) → VWorld 응답 JSON (실패하면 Error: timeout·script_error·bad_request 등)
 */
export function createVworldBridge(doc = document, { src = "vworld-frame.html", timeoutMs = 20000, win = window } = {}) {
  let frame = null, ready = null, seq = 0;
  const pending = new Map();
  const onMsg = (e) => {
    if (!frame || e.source !== frame.contentWindow) return;
    const m = e.data || {};
    if (m.type === "j5-vworld-ready") { ready?.resolve(); return; }
    if (m.type !== "j5-vworld-res" || !pending.has(m.id)) return;
    const p = pending.get(m.id);
    pending.delete(m.id);
    clearTimeout(p.timer);
    if (m.ok) p.resolve(m.data); else p.reject(new Error(String(m.error || "error")));
  };
  const ensure = () => {
    if (ready) return ready.promise;
    let resolve, reject;
    const promise = new Promise((ok, no) => { resolve = ok; reject = no; });
    ready = { promise, resolve };
    frame = doc.createElement("iframe");
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("aria-hidden", "true");
    frame.tabIndex = -1;
    frame.hidden = true;
    frame.src = src;
    win.addEventListener("message", onMsg);
    doc.body.append(frame);
    const t = setTimeout(() => { reject(new Error("frame_timeout")); destroy(); }, timeoutMs);
    promise.then(() => clearTimeout(t), () => {});
    return promise;
  };
  const send = async (body) => {
    await ensure();
    const id = ++seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error("timeout")); }, timeoutMs);
      pending.set(id, { resolve, reject, timer });
      frame.contentWindow.postMessage({ type: "j5-vworld-req", id, ...body }, "*");
    });
  };
  const destroy = () => {
    win.removeEventListener("message", onMsg);
    frame?.remove();
    frame = null;
    ready = null;
    for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("closed")); }
    pending.clear();
  };
  return {
    /** where: [경도, 위도](누른 자리) 또는 { box: [서, 남, 동, 북] }(화면 범위, 한 변 0.02° 이하). opts.size: 받을 수(1~1000) */
    async request(data, where, key, domain, opts = {}) {
      const geo = Array.isArray(where) ? { lon: where[0], lat: where[1] } : { box: where.box };
      return send({ key, domain, data, ...geo, ...(opts.size ? { size: opts.size } : {}) });
    },
    /** 주소·지번 찾기 (J5-082). category: parcel(지번 주소)·road(도로명 주소) */
    async search(query, key, domain, category = "parcel") {
      return send({ api: "search", key, domain, query, category });
    },
    destroy,
  };
}
