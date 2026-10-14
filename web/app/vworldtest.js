// VWorld 직접 연결 시험 (J5-078). 기기 시험 페이지(devtest.html)에서만 쓴다.
// 폰(국내 회선)에서 VWorld 를 바로 부를 수 있는지(브라우저의 다른 출처 요청 허용, CORS)와 배경 타일이 그려지는지 본다.
// 인증키는 입력칸에만 두고 저장·전송하지 않는다(VWorld 요청 주소에만 싣는다). 결과에는 키를 지운 주소만 남긴다.
// 폰 실측(2026-10-14): 배경 타일은 그려지고 데이터 API 의 다른 출처 요청은 막혔다. 그래서 JSONP(격리 프레임)와 경계 그림(WMS)도 본다.

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

/** 시험 지점이 든 z17 타일 범위의 연속지적 경계 그림(WMS GetMap, 웹 메르카토르). 그림은 다른 출처 허용 없이도 그려진다 */
export function cadastralWmsUrl(key, domain) {
  const R = 6378137, n = 2 ** 17, x = 111775, y = 50755, size = (2 * Math.PI * R) / n, minx = -Math.PI * R + x * size, maxy = Math.PI * R - y * size;
  const q = new URLSearchParams({ SERVICE: "WMS", REQUEST: "GetMap", VERSION: "1.3.0", LAYERS: "lp_pa_cbnd_bubun", STYLES: "lp_pa_cbnd_bubun", CRS: "EPSG:3857",
    BBOX: [minx, maxy - size, minx + size, maxy].map((v) => v.toFixed(2)).join(","), WIDTH: "256", HEIGHT: "256", FORMAT: "image/png", TRANSPARENT: "TRUE", KEY: key, DOMAIN: domain });
  return `${VWORLD_ORIGIN}/req/wms?${q}`;
}

/** 격리 프레임(vworld-frame.html, sandbox="allow-scripts")에서 JSONP 로 필지 조회를 한 번 부른다. 프레임이 준비 신호를 보내면 요청을 보내고,
 *  그 프레임에서 온 응답만 받는다. 결과에는 응답 필드 이름 경로와 앞부분(키 없음)만 남긴다 */
export function runJsonpProbe(key, domain, doc = document, timeoutMs = 20000) {
  return new Promise((resolve) => {
    const t0 = performance.now();
    const frame = doc.createElement("iframe");
    frame.setAttribute("sandbox", "allow-scripts");
    frame.hidden = true;
    frame.src = "vworld-frame.html";
    let finished = false;
    const end = (r) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      window.removeEventListener("message", onMsg);
      frame.remove();
      resolve({ ...r, ms: Math.round(performance.now() - t0) });
    };
    const onMsg = (e) => {
      if (e.source !== frame.contentWindow) return;
      const m = e.data || {};
      if (m.type === "j5-vworld-ready") {
        const [lon, lat] = TEST_POINT;
        frame.contentWindow.postMessage({ type: "j5-vworld-req", id: 1, key, domain, data: "LP_PA_CBND_BUBUN", lon, lat }, "*");
      } else if (m.type === "j5-vworld-res" && m.id === 1) {
        end(m.ok ? { outcome: "ok", fields: fieldPaths(m.data), preview: redactKey(JSON.stringify(m.data), key).slice(0, 1500) } : { outcome: "error", error: String(m.error) });
      }
    };
    const timer = setTimeout(() => end({ outcome: "timeout" }), timeoutMs);
    window.addEventListener("message", onMsg);
    doc.body.append(frame);
  });
}
