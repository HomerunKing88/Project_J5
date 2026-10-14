// 온라인 필지 조회 e2e (J5-080, ADR-27). 지도의 빈 곳을 누르면 격리 프레임이 VWorld 를 JSONP 로 불러 필지 카드·경계를 보이고, 그 필지를 대상으로 기록한다.
// VWorld 응답은 CDP Fetch 로 흉내 낸다(격리 프레임이 다른 프로세스의 틀이어도 가로채도록 붙는다). 응답 구조는 폰 실측과 같고 값은 가상이다.
// 헤드리스 Chromium, 브라우저가 없으면 건너뛴다. 실제 VWorld·iPhone 확인이 아니다.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Cdp, WEB, findChrome, serve } from "./cdp.mjs";

const chrome = findChrome();
const KEY = "E2E-LOOKUP-KEY-0123456789";
const shown = (id) => `(() => { const e = document.getElementById('${id}'); return !!e && !e.hidden && e.getClientRects().length > 0; })()`;
const square = [[[127.0019, 37.5709], [127.0020, 37.5709], [127.0020, 37.5710], [127.0019, 37.5710], [127.0019, 37.5709]]];
const vworldDoc = (props, geometry) => ({ response: { service: { name: "data", version: "2.0", operation: "GetFeature" }, status: "OK", record: { total: "1", current: "1" },
  result: { featureCollection: { type: "FeatureCollection", features: [{ type: "Feature", geometry, properties: props }] } } } });

test("온라인 필지 조회: 키 없으면 안내, 키가 있으면 지도를 눌러 필지 카드·경계, 그 필지로 기록", { skip: chrome ? false : "Chromium 없음" }, async () => {
  const tmp = mkdtempSync(join(tmpdir(), "j5-lookup-"));
  const srv = await serve(WEB);
  const base = `http://127.0.0.1:${srv.address().port}`;
  let cdp;
  try {
    cdp = await Cdp.launch(chrome, tmp);
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
    const seen = [];
    let parcelReply = "ok";
    cdp.on("Fetch.requestPaused", async (p, sessionId) => {
      const url = new URL(p.request.url);
      seen.push(p.request.url);
      const cb = url.searchParams.get("callback");
      let body = "", type = "image/png";
      if (cb) {
        type = "application/javascript";
        const data = url.searchParams.get("data");
        const isBox = (url.searchParams.get("geomFilter") ?? "").startsWith("BOX(");
        const ring = (dx) => [[[127.0019 + dx, 37.5709], [127.0020 + dx, 37.5709], [127.0020 + dx, 37.5710], [127.0019 + dx, 37.5710], [127.0019 + dx, 37.5709]]];
        const doc = isBox
          ? { response: { status: "OK", record: { total: "3", current: "3" }, result: { featureCollection: { features: [0, 1, 2].map((i) => ({ type: "Feature",
              geometry: { type: "MultiPolygon", coordinates: [ring(i * 0.0001)] }, properties: { pnu: `111101630010193001${i}`, jibun: `193-1${i}대` } })) } } } }
          : data === "LP_PA_CBND_BUBUN"
          ? (parcelReply === "none" ? { response: { status: "NOT_FOUND" } }
            : vworldDoc({ gosi_year: "2025", pnu: "1111013800101820013", jibun: "182-13 대", bonbun: "0182", bubun: "0013", addr: "서울특별시 종로구 종로5가 182-13", gosi_month: "01", jiga: "12345000" }, { type: "MultiPolygon", coordinates: [square] }))
          : vworldDoc({ uname: "일반상업지역", dyear: "2020" }, { type: "MultiPolygon", coordinates: [square] });
        body = `${cb}(${JSON.stringify(doc)});`;
      }
      await cdp.send("Fetch.fulfillRequest", { requestId: p.requestId, responseCode: 200, body: Buffer.from(body).toString("base64"), responseHeaders: [{ name: "content-type", value: type }] }, 20000, sessionId);
    });
    const vworldOnly = { patterns: [{ urlPattern: "https://api.vworld.kr/*" }] };
    await cdp.send("Fetch.enable", vworldOnly);
    cdp.on("Target.attachedToTarget", async ({ sessionId }) => {
      await cdp.send("Fetch.enable", vworldOnly, 20000, sessionId).catch(() => {});
      await cdp.send("Runtime.runIfWaitingForDebugger", {}, 20000, sessionId).catch(() => {});
    });
    await cdp.send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });

    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.2.41')");
    await cdp.eval("document.getElementById('study-id').value = 'lookup-study'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor("document.getElementById('settings-note').textContent === '저장했습니다'");
    const tapMap = async (x = 195, y = 300) => {
      await cdp.eval("document.getElementById('field-sheet').dataset.state === 'open' && document.getElementById('sheet-toggle').click(); 'ok'");
      await cdp.waitFor("document.getElementById('field-sheet').dataset.state === 'peek'");
      for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) await cdp.send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
    };

    // 1. 키가 없으면 조회하지 않고 안내
    await tapMap();
    await cdp.waitFor(shown("lookup-panel"));
    assert.ok((await cdp.eval("document.getElementById('lookup-status').textContent")).includes("VWorld 인증키가 필요합니다"));
    assert.equal(seen.filter((u) => u.includes("/req/data")).length, 0, "키 없으면 VWorld 를 부르지 않는다");

    // 2. 배경 지도에 VWorld 키를 저장하고 누르면 필지 카드·경계
    await cdp.eval(`document.getElementById('nav-settings').click(); document.getElementById('tiles-provider').value = 'vworld_base'; document.getElementById('tiles-key').value = ${JSON.stringify(KEY)}; document.getElementById('tiles-save').click(); 'ok'`);
    await cdp.waitFor("document.getElementById('tiles-note').textContent.startsWith('저장했습니다: VWorld 기본 지도')");
    await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
    await tapMap();
    await cdp.waitFor("!document.getElementById('lookup-body').hidden", 20000);
    assert.equal(await cdp.eval("document.getElementById('lookup-title').textContent"), "서울특별시 종로구 종로5가 182-13");
    assert.equal(await cdp.eval("document.getElementById('lookup-pnu').textContent"), "1111013800101820013");
    const facts = await cdp.eval("document.getElementById('lookup-facts').innerText");
    for (const w of ["182-13", "대", "1,235만 원/㎡", "4,081만 원/평", "2025-01 기준", "도형면적", "공부면적 아님", "일반상업지역 (2020 고시)"]) assert.ok(facts.includes(w), `카드에 ${w}: ${facts}`);
    assert.equal(await cdp.eval("document.getElementById('lookup-zoning').hidden"), false, "용도지역 원문 칸");
    assert.ok((await cdp.eval("document.getElementById('lookup-zoning-list').textContent")).includes("일반상업지역"));
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg .layer-lookup path.lookup-parcel').length"), 1, "조회한 필지 경계");
    const dataReqs = seen.filter((u) => u.includes("/req/data"));
    assert.ok(dataReqs.some((u) => u.includes("data=LP_PA_CBND_BUBUN")) && dataReqs.some((u) => u.includes("data=LT_C_UQ111")));
    assert.ok(dataReqs.every((u) => u.includes("callback=__j5vw") && u.includes(`domain=${encodeURIComponent(base)}`)));
    assert.equal(await cdp.eval("document.querySelectorAll('iframe[sandbox=\"allow-scripts\"]').length"), 1, "격리 프레임 하나를 둔다");
    assert.ok(!(await cdp.eval("document.body.innerText")).includes(KEY), "화면에 키 없음");

    // 3. 이 필지를 대상으로 기록 → 이 폰의 대상, 기록 화면
    await cdp.eval("document.getElementById('lookup-new-asset').click(); 'ok'");
    await cdp.waitFor(shown("sec-observe"));
    assert.ok((await cdp.eval("document.getElementById('sec-observe').innerText")).includes("종로5가 182-13"));
    assert.equal(await cdp.eval("document.getElementById('lookup-panel').hidden"), true);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg .layer-lookup path').length"), 0, "카드를 닫으면 경계도 지운다");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('sec-observe').hidden");

    // 4. 확대 17단계 이상이면 화면 범위의 필지 경계 (J5-081): 범위(BOX) 조회 한 번, 경계 3개
    assert.equal(seen.filter((u) => u.includes("geomFilter=BOX")).length, 0, "확대 전에는 범위 조회를 하지 않는다");
    for (let i = 0; i < 8 && !(await cdp.eval("document.querySelectorAll('#map-svg path.around-parcel').length")); i++) {
      await cdp.eval("document.getElementById('map-zoom-in').click(); 'ok'");
      await new Promise((r) => setTimeout(r, 900));
    }
    await cdp.waitFor("document.querySelectorAll('#map-svg path.around-parcel').length === 3", 15000);
    const boxReqs = seen.filter((u) => u.includes("geomFilter=BOX"));
    assert.ok(boxReqs.length >= 1 && boxReqs.every((u) => u.includes("size=1000") && u.includes("data=LP_PA_CBND_BUBUN")));
    // 같은 곳에서 조금 움직여도(받은 범위 안) 다시 부르지 않는다
    const before = boxReqs.length;
    await cdp.eval("document.getElementById('map-zoom-in').click(); 'ok'");
    await new Promise((r) => setTimeout(r, 1200));
    assert.equal(seen.filter((u) => u.includes("geomFilter=BOX")).length, before, "받은 범위 안이면 다시 부르지 않는다");
    // 배경 지도를 끄면 경계도 지운다
    await cdp.eval("document.getElementById('nav-settings').click(); document.getElementById('tiles-provider').value = ''; document.getElementById('tiles-save').click(); document.getElementById('nav-map').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg path.around-parcel').length === 0", 8000);
    await cdp.eval(`document.getElementById('nav-settings').click(); document.getElementById('tiles-provider').value = 'vworld_base'; document.getElementById('tiles-key').value = ${JSON.stringify(KEY)}; document.getElementById('tiles-save').click(); document.getElementById('nav-map').click(); 'ok'`);

    // 5. 필지가 없는 자리
    parcelReply = "none";
    await tapMap(195, 260);
    await cdp.waitFor("document.getElementById('lookup-status').textContent.includes('필지가 없습니다')", 20000);
    assert.equal(await cdp.eval("document.getElementById('lookup-body').hidden"), true);
  } finally {
    cdp?.close();
    srv.close();
  }
});
