// VWorld JSONP 격리 프레임 (J5-078). vworld-frame.html 이 일반 스크립트로 읽는다(모듈 스크립트는 같은 출처 권한이 없는 프레임에서
// 다른 출처 요청 허용 머리글이 필요해 읽히지 않는다). 부모의 요청 값을 검사해 정해 둔 VWorld 데이터 API 주소만 만들고,
// 받은 응답은 JSON 으로 한 번 복사해(함수·객체 참조 없이) 부모에게 보낸다. 인증키는 요청 주소에만 쓰고 돌려보내지 않는다.
"use strict";
(function () {
  var VWORLD = "https://api.vworld.kr/req/data";
  var DATASETS = { LP_PA_CBND_BUBUN: true, LT_C_UQ111: true };
  var TIMEOUT_MS = 15000;
  var seq = 0;

  function reply(msg) {
    window.parent.postMessage(msg, "*");
  }

  function valid(m) {
    return m && m.type === "j5-vworld-req" && Number.isInteger(m.id) && typeof m.key === "string" && /^[A-Za-z0-9-]{8,64}$/.test(m.key) &&
      typeof m.domain === "string" && /^https?:\/\/[a-z0-9.-]+(:\d+)?$/.test(m.domain) && DATASETS[m.data] === true &&
      typeof m.lon === "number" && typeof m.lat === "number" && m.lon > 124 && m.lon < 132 && m.lat > 33 && m.lat < 39.5;
  }

  window.addEventListener("message", function (e) {
    if (e.source !== window.parent) return;
    var m = e.data;
    if (!m || m.type !== "j5-vworld-req") return;
    if (!valid(m)) return reply({ type: "j5-vworld-res", id: Number.isInteger(m.id) ? m.id : null, ok: false, error: "bad_request" });
    var cb = "__j5vw" + (++seq);
    var s = document.createElement("script");
    var done = false;
    var timer = null;
    function finish(msg) {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try { delete window[cb]; } catch (err) { window[cb] = undefined; }
      s.remove();
      msg.type = "j5-vworld-res";
      msg.id = m.id;
      reply(msg);
    }
    window[cb] = function (data) {
      var copy = null;
      try { copy = JSON.parse(JSON.stringify(data)); } catch (err) { return finish({ ok: false, error: "bad_json" }); }
      finish({ ok: true, data: copy });
    };
    s.onerror = function () { finish({ ok: false, error: "script_error" }); };
    timer = setTimeout(function () { finish({ ok: false, error: "timeout" }); }, TIMEOUT_MS);
    var q = new URLSearchParams({ service: "data", version: "2.0", request: "GetFeature", format: "json", errorformat: "json", size: "10", page: "1",
      data: m.data, geomFilter: "POINT(" + m.lon + " " + m.lat + ")", geometry: "true", attribute: "true", crs: "EPSG:4326", key: m.key, domain: m.domain, callback: cb });
    s.src = VWORLD + "?" + q.toString();
    document.head.appendChild(s);
  });

  reply({ type: "j5-vworld-ready" });
})();
