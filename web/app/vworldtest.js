// VWorld 직접 연결 시험 (J5-078). 기기 시험 페이지(devtest.html)에서만 쓴다.
// 폰(국내 회선)에서 VWorld 를 바로 부를 수 있는지(브라우저의 다른 출처 요청 허용, CORS)와 배경 타일이 그려지는지 본다.
// 인증키는 입력칸에만 두고 저장·전송하지 않는다(VWorld 요청 주소에만 싣는다). 결과에는 키를 지운 주소만 남긴다.

export const VWORLD_ORIGIN = "https://api.vworld.kr";
/** 시험 지점: 종로5가역 부근 [경도, 위도] (앱의 FIELD_HOME 과 같다) */
export const TEST_POINT = Object.freeze([127.0019, 37.5709]);

/** 지점의 필지 조회 주소(연속지적 LP_PA_CBND_BUBUN, 2.0 데이터 API). domain 은 키에 등록한 서비스 주소 */
export function parcelProbeUrl(key, domain) {
  const [lon, lat] = TEST_POINT;
  const q = new URLSearchParams({ service: "data", version: "2.0", request: "GetFeature", format: "json", errorformat: "json", size: "1", page: "1", data: "LP_PA_CBND_BUBUN",
    geomFilter: `POINT(${lon} ${lat})`, geometry: "false", attribute: "true", crs: "EPSG:4326", key, domain });
  return `${VWORLD_ORIGIN}/req/data?${q}`;
}

/** 시험 지점이 든 z17 배경 타일 주소 (WMTS 경로는 {z}/{y}/{x}) */
export function tileProbeUrl(key) {
  return `${VWORLD_ORIGIN}/req/wmts/1.0.0/${encodeURIComponent(key)}/Base/17/50755/111775.png`;
}

export function redactKey(text, key) {
  let s = String(text ?? "");
  if (key && key.length >= 8) s = s.split(key).join("<KEY>").split(encodeURIComponent(key)).join("<KEY>");
  return s.replace(/([?&]key=)[^&"'\s]+/gi, "$1<KEY>");
}

/** 응답 JSON 의 필드 이름 경로 목록 (값 없이 이름만, 배열은 첫 항목만 본다) */
export function fieldPaths(obj, prefix = "", out = []) {
  if (Array.isArray(obj)) return obj.length ? fieldPaths(obj[0], prefix + "[0]", out) : out;
  if (!obj || typeof obj !== "object") return out;
  for (const [k, v] of Object.entries(obj)) {
    const p = prefix ? `${prefix}.${k}` : k;
    out.push(p);
    fieldPaths(v, p, out);
  }
  return out;
}

/** 필지 조회를 한 번 부른다. 다른 출처 요청이 막히면 브라우저가 TypeError 를 내므로 그것을 cors_blocked 로 적는다 */
export async function runParcelProbe(key, domain) {
  const url = parcelProbeUrl(key, domain);
  const t0 = performance.now();
  try {
    const res = await fetch(url, { mode: "cors", credentials: "omit", referrerPolicy: "origin" });
    const text = await res.text();
    let body = null;
    try { body = JSON.parse(text); } catch { /* JSON 아님 */ }
    return { outcome: res.ok ? "ok" : "http_error", status: res.status, ms: Math.round(performance.now() - t0), url: redactKey(url, key),
      fields: body ? fieldPaths(body) : null, preview: redactKey(text, key).slice(0, 1500) };
  } catch (e) {
    return { outcome: "cors_blocked_or_network", status: null, ms: Math.round(performance.now() - t0), url: redactKey(url, key), error: redactKey(String(e?.message || e), key) };
  }
}
