// Cloudflare Worker (J5-078, ADR-27 준비). 정적 앱(web/)은 그대로 자산으로 내주고, /api/ 아래만 이 코드가 받는다.
// 지금은 VWorld 연결 시험 하나만 둔다: Cloudflare 에서 VWorld 를 부를 수 있는지(해외 접속 차단 여부)와 실제 응답 필드를 확인한다.
// 인증키는 Worker 비밀값 VWORLD_API_KEY 에만 있고 응답·로그에 싣지 않는다. 시험 주소는 고정 질의만 하며 확인이 끝나면 지운다.

const VWORLD = "https://api.vworld.kr";
/** 시험 지점: 종로5가역 부근 [경도, 위도] (앱의 FIELD_HOME 과 같다) */
const PROBE_POINT = [127.0019, 37.5709];
const BODY_PREVIEW = 6000;
const PROBE_CACHE_S = 300;   // 같은 시험을 5분 안에 다시 부르면 저장한 결과를 준다(VWorld 호출 수를 아낀다)

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/api/vworld/probe") {
      if (request.method !== "GET") return json({ error: "GET 만 받는다" }, 405);
      return probe(request, env, ctx);
    }
    if (url.pathname.startsWith("/api/")) return json({ error: "없는 주소" }, 404);
    return env.ASSETS.fetch(request);
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 1), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff" } });
}

/** 글에서 인증키를 지운다. 키가 짧거나 비어 있으면 그대로 */
export function redact(text, key) {
  let s = String(text ?? "");
  if (key && key.length >= 8) s = s.split(key).join("<KEY>");
  return s.replace(/([?&]key=)[^&"'\s]+/gi, "$1<KEY>");
}

/** VWorld 한 번 부르기. key 는 지우기에 늘 쓰고, sendKey 가 거짓이면(타일처럼 경로에 키가 든 주소) 질의에 싣지 않는다 */
async function call(name, path, params, { key, domain = null, sendKey = true }) {
  const q = new URLSearchParams({ ...params, ...(sendKey ? { key } : {}), ...(domain ? { domain } : {}) });
  const target = `${VWORLD}${path}${q.size ? "?" + q : ""}`;
  const t0 = Date.now();
  try {
    const res = await fetch(target, { headers: { accept: "application/json, image/*;q=0.8, */*;q=0.5" }, redirect: "manual" });
    const type = res.headers.get("content-type") || "";
    const buf = new Uint8Array(await res.arrayBuffer());
    const isText = /json|xml|text|javascript/i.test(type) || buf.length < 2000;
    const body = isText ? new TextDecoder().decode(buf) : null;
    return { name, url: redact(target, key), status: res.status, ms: Date.now() - t0, content_type: type, bytes: buf.length,
      access_control_allow_origin: res.headers.get("access-control-allow-origin"), body: body === null ? null : redact(body, key).slice(0, BODY_PREVIEW), _json: tryJson(body) };
  } catch (e) {
    return { name, url: redact(target, key), status: null, ms: Date.now() - t0, error: redact(String(e?.message || e), key) };
  }
}

function tryJson(s) {
  try { return s ? JSON.parse(s) : null; } catch { return null; }
}

/** 응답에서 첫 필지의 PNU 를 찾는다(필드 이름은 응답으로 확인 중이라 pnu 대소문자를 모두 본다) */
export function findPnu(obj) {
  const stack = [obj];
  while (stack.length) {
    const v = stack.pop();
    if (!v || typeof v !== "object") continue;
    for (const [k, x] of Object.entries(v)) {
      if (/^pnu$/i.test(k) && typeof x === "string" && /^\d{19}$/.test(x)) return x;
      if (x && typeof x === "object") stack.push(x);
    }
  }
  return null;
}

async function probe(request, env, ctx) {
  const key = env.VWORLD_API_KEY;
  if (!key) return json({ error: "Worker 비밀값 VWORLD_API_KEY 가 없다" }, 500);
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const cacheKey = new Request(new URL("/api/vworld/probe", request.url).toString());
  if (cache) {
    const hit = await cache.match(cacheKey);
    if (hit) return hit;
  }
  const origin = new URL(request.url).origin;
  const domain = env.VWORLD_DOMAIN || origin;
  const [lon, lat] = PROBE_POINT;
  const feature = (data, extra = {}) => ({ service: "data", version: "2.0", request: "GetFeature", format: "json", errorformat: "json", size: "10", page: "1", data,
    geomFilter: `POINT(${lon} ${lat})`, geometry: "true", attribute: "true", crs: "EPSG:4326", ...extra });
  const steps = [];
  steps.push(await call("parcel_at_point", "/req/data", feature("LP_PA_CBND_BUBUN"), { key, domain }));
  const pnu = findPnu(steps[0]._json);
  steps.push(await call("zoning_at_point", "/req/data", feature("LT_C_UQ111"), { key, domain }));
  if (pnu) {
    const ned = { pnu, format: "json", numOfRows: "10", pageNo: "1" };
    steps.push(await call("land_characteristics", "/ned/data/getLandCharacteristics", { ...ned, stdrYear: String(new Date().getUTCFullYear() - 1) }, { key, domain }));
    steps.push(await call("land_register", "/ned/data/ladfrlList", ned, { key, domain }));
    steps.push(await call("land_price", "/ned/data/getIndvdLandPriceAttr", { ...ned, stdrYear: String(new Date().getUTCFullYear() - 1) }, { key, domain }));
    steps.push(await call("land_use_plan", "/ned/data/getLandUseAttr", { ...ned, numOfRows: "50" }, { key, domain }));
  }
  // 배경 타일(WMTS, 경로가 {z}/{y}/{x}): 시험 지점이 든 z17 타일
  steps.push(await call("tile_base", `/req/wmts/1.0.0/${key}/Base/17/50755/111775.png`, {}, { key, sendKey: false }));
  const out = { note: "VWorld 연결 시험 (J5-078). 인증키는 지웠다. 시험이 끝나면 이 주소를 지운다.", checked_at: new Date().toISOString(), domain_param: domain, colo: request.cf?.colo ?? null,
    country: request.cf?.country ?? null, pnu, steps: steps.map(({ _json, ...s }) => s) };
  const res = json(out);
  res.headers.set("cache-control", `public, max-age=${PROBE_CACHE_S}`);
  if (cache && ctx?.waitUntil) ctx.waitUntil(cache.put(cacheKey, res.clone()));
  res.headers.set("cache-control", "no-store");
  return res;
}
