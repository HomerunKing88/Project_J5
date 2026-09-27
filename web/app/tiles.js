// 배경 지도 타일 (J5-024, ADR-18). 자체 SVG 지도(ADR-12)의 맨 아래 층에 Web Mercator XYZ 타일을 <image> 로 그린다.
// 타일은 사용자가 설정에서 켠 제공자(공식 타일 API, 키는 이 기기에만 저장)에서만 받고, 호스트 허용목록 밖으로는 요청하지 않는다.
// 한 번 받은 타일은 서비스 워커가 기기에 저장해(캐시 j5-tiles-v1) 오프라인에서도 보인다. 여기는 순수 계산과 URL 만 다루고 DOM·통신은 없다.

export const TILE_SIZE = 256;
export const TILE_CACHE = "j5-tiles-v1";
/** 요청을 허용하는 타일 호스트. 같은 출처(상대 경로 템플릿)는 항상 허용된다. sw.js 의 목록과 같아야 한다. */
export const TILE_HOSTS = Object.freeze(["api.vworld.kr"]);
export const PREFETCH_MAX_TILES = 3000;   // 미리 받기 한 번의 상한 (256px PNG 약 20KB × 3,000 ≈ 60MB)
export const PREFETCH_ZOOMS = Object.freeze({ min: 14, max: 18 });

/** 인증키 자리표. 타일 URL 에는 이 자리표만 두고 실제 키는 서비스 워커가 요청 직전에 IndexedDB 에서 읽어 바꾼다 (캐시 키·화면·요소 속성에 키가 남지 않게, 리뷰 반영). */
export const KEY_PLACEHOLDER = "{key}";

/**
 * 제공자 미리 설정. verified: 공식 문서로 형식·약관을 확인한 날짜, 아니면 null. enabled 가 false 면 선택할 수 없다 (리뷰 반영: 공식 문서·실제 응답·캐시 이용조건을 확인하기 전에는
 * 미검증 주소로 수천 건을 요청·저장하지 않는다. 확인이 끝나면 verified 에 날짜를 적고 enabled 를 true 로 바꾼다). 그 전에는 사용자가 제공자 문서의 형식을 "직접 입력" 에 넣는다.
 */
export const PROVIDERS = Object.freeze({
  vworld_base: { name: "VWorld 기본 지도 (국토교통부)", host: "api.vworld.kr", template: "/req/wmts/1.0.0/{key}/Base/{z}/{y}/{x}.png", minZoom: 6, maxZoom: 19, needsKey: true, attribution: "배경: VWorld (국토교통부)", verified: null, enabled: false },
  vworld_satellite: { name: "VWorld 항공사진", host: "api.vworld.kr", template: "/req/wmts/1.0.0/{key}/Satellite/{z}/{y}/{x}.jpeg", minZoom: 6, maxZoom: 19, needsKey: true, attribution: "배경: VWorld (국토교통부)", verified: null, enabled: false },
  vworld_hybrid: { name: "VWorld 항공사진 + 라벨", host: "api.vworld.kr", template: "/req/wmts/1.0.0/{key}/Hybrid/{z}/{y}/{x}.png", minZoom: 6, maxZoom: 19, needsKey: true, attribution: "배경: VWorld (국토교통부)", verified: null, enabled: false },
  custom: { name: "직접 입력 (허용 호스트 또는 같은 출처)", host: null, template: "", minZoom: 0, maxZoom: 22, needsKey: false, attribution: "배경: 사용자 지정 타일", verified: null, enabled: true },
});

/** 설정 → 템플릿 URL(키는 자리표 그대로). 잘못되면 {errors:[...]}. */
export function resolveTileConfig({ provider, key = "", template = "", attribution = "" } = {}) {
  const p = PROVIDERS[provider];
  if (!p) return { errors: ["제공자를 고른다"] };
  if (!p.enabled) return { errors: [`${p.name} 은 주소 형식·이용조건을 공식 문서로 확인하기 전이라 아직 고를 수 없다. 제공자 문서의 타일 주소를 "직접 입력" 에 넣는다 (허용 호스트: ${TILE_HOSTS.join(", ")})`] };
  let url;
  if (provider === "custom") {
    url = String(template ?? "").trim();
    if (!url) return { errors: ["타일 주소 템플릿을 넣는다 (예: ./tiles/{z}/{x}/{y}.png, 인증키 자리는 {key})"] };
  } else {
    url = "https://" + p.host + p.template;
  }
  const needsKey = p.needsKey || url.includes(KEY_PLACEHOLDER);
  if (needsKey && !String(key ?? "").trim()) return { errors: ["이 주소는 인증키가 필요하다 (제공자 사이트에서 발급, 이 기기에만 저장)"] };
  const errs = validateTemplate(url);
  if (errs.length) return { errors: errs };
  return { url, minZoom: p.minZoom, maxZoom: p.maxZoom, attribution: provider === "custom" && attribution ? attribution : p.attribution, provider, needsKey };
}

/** 템플릿 검사: {z}{x}{y} 필수, 같은 출처 상대 경로이거나 https 의 허용 호스트, 경로 탈출·제어문자 없음. */
export function validateTemplate(url) {
  const errs = [];
  const s = String(url ?? "");
  for (const t of ["{z}", "{x}", "{y}"]) if (!s.includes(t)) errs.push(`템플릿에 ${t} 가 없다`);
  if (/[\s\u0000-\u001f\u007f]/.test(s)) errs.push("템플릿에 공백·제어문자가 있다");
  if (s.split("/").includes("..")) errs.push("템플릿에 상위 경로(..)를 쓸 수 없다");
  if (/^https:\/\//i.test(s)) {
    const host = s.slice(8).split(/[/?#]/)[0].toLowerCase();
    if (!TILE_HOSTS.includes(host)) errs.push(`허용되지 않은 호스트 ${host} (허용: ${TILE_HOSTS.join(", ")}, 또는 ./ 로 시작하는 같은 출처 경로)`);
  } else if (!s.startsWith("./") && !s.startsWith("/")) {
    errs.push("템플릿은 https:// 허용 호스트 또는 ./ 로 시작하는 같은 출처 경로여야 한다");
  } else if (s.startsWith("//")) {
    errs.push("// 로 시작하는 주소는 쓸 수 없다");
  }
  return errs;
}

/** 키를 가린 템플릿 (화면 표시용). URL 에 키가 들어 있지 않으면 그대로. */
export function maskUrl(url, key) {
  if (!key) return url;
  return url.split(encodeURIComponent(key)).join("****").split(key).join("****");
}

/** 타일 URL. {key} 자리표는 남겨 둔다 (서비스 워커가 바꾼다). */
export function tileUrl(template, z, x, y) {
  return template.replace("{z}", String(z)).replace("{x}", String(x)).replace("{y}", String(y));
}

/** 화면 축척에 맞는 타일 확대 단계. 타일 하나가 화면에서 256~512px 이 되게 하고, 고해상도 화면은 한 단계 더 세밀하게. */
export function tileZoom(scale, { minZoom = 0, maxZoom = 22, dpr = 1 } = {}) {
  let z = Math.floor(Math.log2(scale / TILE_SIZE) + 1e-9);
  if (dpr >= 2) z += 1;
  return Math.max(minZoom, Math.min(maxZoom, z));
}

/** 화면(w×h)을 덮는 타일 목록 [{z,x,y,key}]. margin 은 여분 타일 수. 세계 좌표 0~1 밖(y)은 제외, x 는 감싸지 않고 제외. */
export function tilesFor(view, w, h, z, margin = 0) {
  const n = 2 ** z;
  const px = view.scale / n;                                  // 타일 하나의 화면 크기
  const x0 = Math.floor((0 - view.tx) / px) - margin, x1 = Math.floor((w - view.tx) / px) + margin;
  const y0 = Math.floor((0 - view.ty) / px) - margin, y1 = Math.floor((h - view.ty) / px) + margin;
  const out = [];
  for (let y = Math.max(0, y0); y <= Math.min(n - 1, y1); y++) {
    for (let x = Math.max(0, x0); x <= Math.min(n - 1, x1); x++) out.push({ z, x, y, key: `${z}/${x}/${y}` });
  }
  return out;
}

/** 타일의 화면 위치·크기. */
export function tileRect(tile, view) {
  const n = 2 ** tile.z;
  const px = view.scale / n;
  return { x: tile.x * px + view.tx, y: tile.y * px + view.ty, size: px };
}

const lon2x = (lon, z) => Math.floor(((lon + 180) / 360) * 2 ** z);
const lat2y = (lat, z) => { const la = (lat * Math.PI) / 180; return Math.floor(((1 - Math.log(Math.tan(la) + 1 / Math.cos(la)) / Math.PI) / 2) * 2 ** z); };

/** 경위도 bbox 를 z 범위로 덮는 타일 목록과 수. cap 을 넘으면 {tooMany:true, count} 만 돌려준다. */
export function prefetchPlan(bbox, { minZoom = PREFETCH_ZOOMS.min, maxZoom = PREFETCH_ZOOMS.max, cap = PREFETCH_MAX_TILES } = {}) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(Number.isFinite)) return { count: 0, tiles: [], tooMany: false };
  const tiles = [];
  let count = 0;
  for (let z = minZoom; z <= maxZoom; z++) {
    const xa = lon2x(bbox[0], z), xb = lon2x(bbox[2], z);
    const ya = lat2y(bbox[3], z), yb = lat2y(bbox[1], z);
    count += (xb - xa + 1) * (yb - ya + 1);
    if (count > cap) return { count, tiles: [], tooMany: true, cap };
    for (let y = ya; y <= yb; y++) for (let x = xa; x <= xb; x++) tiles.push({ z, x, y, key: `${z}/${x}/${y}` });
  }
  return { count, tiles, tooMany: false, cap };
}

/** bbox 를 m 단위로 넓힌다 (미리 받기 여유). */
export function padBbox(bbox, meters) {
  const lat = (bbox[1] + bbox[3]) / 2;
  const dlat = meters / 111320, dlon = meters / (111320 * Math.max(Math.cos((lat * Math.PI) / 180), 1e-6));
  return [bbox[0] - dlon, bbox[1] - dlat, bbox[2] + dlon, bbox[3] + dlat];
}
