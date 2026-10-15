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
    let boxDelayMs = 0;
    cdp.on("Fetch.requestPaused", async (p, sessionId) => {
      const url = new URL(p.request.url);
      seen.push(p.request.url);
      const cb = url.searchParams.get("callback");
      let body = "", type = "image/png";
      if (cb) {
        type = "application/javascript";
        const data = url.searchParams.get("data");
        const isBox = (url.searchParams.get("geomFilter") ?? "").startsWith("BOX(");
        if (isBox && boxDelayMs) await new Promise((r) => setTimeout(r, boxDelayMs));
        const isSearch = url.pathname === "/req/search";
        const ring = (dx) => [[[127.0019 + dx, 37.5709], [127.0020 + dx, 37.5709], [127.0020 + dx, 37.5710], [127.0019 + dx, 37.5710], [127.0019 + dx, 37.5709]]];
        const doc = isSearch
          ? (url.searchParams.get("query") === "없는주소 1" ? { response: { status: "NOT_FOUND" } }
            : url.searchParams.get("category") !== "parcel" ? { response: { status: "NOT_FOUND" } }
            : { response: { status: "OK", result: { items: [{ id: "1111016300101930017", address: { parcel: "서울특별시 종로구 종로5가 193-17" }, point: { x: "127.00195", y: "37.57095" } }] } } })
          : isBox
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
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.2.43')");
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
    // 받는 중에 지도를 또 움직이면 끝난 뒤 새 범위를 한 번 더 받는다 (리뷰 반영 PR #128)
    boxDelayMs = 2500;
    const n0 = seen.filter((u) => u.includes("geomFilter=BOX")).length;
    for (let i = 0; i < 3 && seen.filter((u) => u.includes("geomFilter=BOX")).length === n0; i++) {
      await cdp.eval("document.getElementById('map-zoom-out').click(); 'ok'");
      await new Promise((r) => setTimeout(r, 900));
    }
    assert.equal(seen.filter((u) => u.includes("geomFilter=BOX")).length, n0 + 1, "축소해 받은 범위를 벗어나면 범위 조회");
    // 받는 중에 지도를 옆으로 크게 끈다(받을 범위 밖으로)
    // 오른쪽 확대·축소 버튼(x 310 이상)과 위 버튼 줄을 피해 지도 가운데에서 끈다
    await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: 270, y: 330, button: "left", clickCount: 1 });
    for (let x = 250; x >= 10; x -= 20) await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y: 330, button: "left", buttons: 1 });
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: 10, y: 330, button: "left", clickCount: 1 });
    await new Promise((r) => setTimeout(r, 4500));
    boxDelayMs = 0;
    for (let i = 0; i < 50 && seen.filter((u) => u.includes("geomFilter=BOX")).length < n0 + 2; i++) await new Promise((r) => setTimeout(r, 100));
    assert.equal(seen.filter((u) => u.includes("geomFilter=BOX")).length, n0 + 2, "받는 중에 움직인 화면은 끝난 뒤 다시 받는다");
    // 배경 지도를 끄면 경계도 지운다
    await cdp.eval("document.getElementById('nav-settings').click(); document.getElementById('tiles-provider').value = ''; document.getElementById('tiles-save').click(); document.getElementById('nav-map').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg path.around-parcel').length === 0", 8000);
    await cdp.eval(`document.getElementById('nav-settings').click(); document.getElementById('tiles-provider').value = 'vworld_base'; document.getElementById('tiles-key').value = ${JSON.stringify(KEY)}; document.getElementById('tiles-save').click(); document.getElementById('nav-map').click(); 'ok'`);

    // 5. 주소로 찾기 (J5-082): 가져온 필지가 없어도 VWorld 키가 있으면 찾기 칸이 보이고, 찾은 자리로 옮겨 그 필지를 조회한다
    await cdp.waitFor("document.getElementById('tiles-note').textContent.startsWith('저장했습니다: VWorld 기본 지도')");
    await cdp.waitFor("!document.getElementById('parcel-search-form').hidden", 8000);   // VWorld 기본 지도를 켜면 찾기 칸
    await cdp.eval("document.getElementById('lookup-close').click(); document.getElementById('parcel-search').value = '종로5가 193-17'; document.getElementById('parcel-search-form').requestSubmit(); 'ok'");
    const pointOf = (u) => new URL(u).searchParams.get("geomFilter");
    for (let i = 0; i < 100 && !seen.some((u) => pointOf(u) === "POINT(127.00195 37.57095)"); i++) await new Promise((r) => setTimeout(r, 100));
    assert.ok(seen.some((u) => pointOf(u) === "POINT(127.00195 37.57095)"), "찾은 자리로 필지 조회: " + seen.filter((u) => u.includes("POINT")).map(pointOf).join(" "));
    await cdp.waitFor("!document.getElementById('lookup-panel').hidden && !document.getElementById('lookup-body').hidden", 20000);
    const searchReqs = seen.filter((u) => u.includes("/req/search"));
    assert.ok(searchReqs.length === 1 && searchReqs[0].includes("category=parcel") && searchReqs[0].includes("callback=__j5vw") && searchReqs[0].includes("type=address"));
    // 없는 주소: 지번 주소·도로명 주소 둘 다 찾고 안내
    await cdp.eval("document.getElementById('parcel-search').value = '없는주소 1'; document.getElementById('parcel-search-form').requestSubmit(); 'ok'");
    await cdp.waitFor("document.getElementById('parcel-search-note').textContent.includes('찾지 못했습니다')", 20000);
    assert.equal(seen.filter((u) => u.includes("/req/search") && u.includes(encodeURIComponent("없는주소").slice(0, 9))).length >= 1, true);

    // 6. 필지가 없는 자리
    parcelReply = "none";
    // 찾기로 지도가 3단계에서 만든 대상 쪽으로 옮겨졌으므로 그 점이 없는 자리를 누른다
    await tapMap(90, 230);
    assert.equal(await cdp.eval("document.getElementById('map-selected').hidden"), true, "대상 점이 아닌 빈 자리");
    await cdp.waitFor("document.getElementById('lookup-status').textContent.includes('필지가 없습니다')", 20000);
    assert.equal(await cdp.eval("document.getElementById('lookup-body').hidden"), true);

    // 6b. 3단계에서 만든 대상을 고르면 만들 때 본 조회 값·경계가 다시 보인다 (J5-084). 이 폰(IndexedDB)에만 두고 내보내기에는 싣지 않는다. 고르면 지도가 대상 쪽으로 움직이므로 확대 시험(4단계) 뒤에 둔다
    await cdp.eval("[...document.querySelectorAll('#asset-list li')].find((li) => li.textContent.includes('182-13')).querySelector('.row-main').click(); 'ok'");
    await cdp.waitFor(`${shown("map-selected")} && ${shown("map-selected-lookup")}`);
    const saved = await cdp.eval("document.getElementById('map-selected-lookup').textContent");
    for (const w of ["서울특별시 종로구 종로5가 182-13", "1111013800101820013", "182-13", "1,235만 원/㎡", "2025-01 기준", "일반상업지역 (2020 고시)", "공부면적 아님", "VWorld LP_PA_CBND_BUBUN", "조회한 값", "PC 로는 보내지 않습니다"]) assert.ok(saved.includes(w), `대상 카드에 ${w}: ${saved}`);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg .layer-lookup path.lookup-parcel').length"), 1, "저장한 경계를 다시 그린다");
    const readStore = (name) => cdp.eval(`new Promise((res) => { const q = indexedDB.open('j5'); q.onsuccess = () => { const g = q.result.transaction('${name}').objectStore('${name}').getAll(); g.onsuccess = () => { res(g.result); q.result.close(); }; }; })`);
    const devices = await readStore("device_assets");
    assert.equal(devices.length, 1);
    assert.ok(!("lookup" in devices[0]), "대상 자체에는 조회 기록을 두지 않는다 (내보내기 항목과 분리)");
    const stored = await readStore("asset_lookups");
    assert.equal(stored.length, 1);
    assert.equal(stored[0].asset_id, devices[0].asset_id);
    assert.equal(stored[0].pnu, "1111013800101820013");
    assert.equal(stored[0].jiga, 12345000);
    assert.ok(Array.isArray(stored[0].polygons) && stored[0].polygons.length && /^\d{4}-\d{2}-\d{2}T/.test(stored[0].looked_up_at));
    assert.ok(!JSON.stringify(stored[0]).includes(KEY), "저장한 기록에 키 없음");
    await cdp.eval("document.getElementById('map-selected-close').click(); 'ok'");
    await cdp.waitFor("document.getElementById('map-selected').hidden");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg .layer-lookup path').length"), 0, "선택을 풀면 경계도 지운다");

    // 7. PC 정본이 같은 대상을 돌려주면(파생본 시드) 기기 사본은 지워지지만 조회 기록은 남는다 (리뷰 반영 PR #129). 마지막에 두어 앞 단계의 화면 상태와 섞이지 않게 한다
    const kept = await cdp.eval(`(async () => {
      const { Store } = await import('./app/db.js');
      const st = await Store.open();
      const [dev] = await st.listDeviceAssets();
      await st.replaceProjection({ assets: [{ ...dev, origin: undefined }], view: { manifest: {}, records: [] }, source: 'projection_test' });
      const out = { devices: (await st.listDeviceAssets()).length, lookup: (await st.listAssetLookups()).get(dev.asset_id)?.pnu ?? null };
      st.db.close();
      return out;
    })()`);
    assert.deepEqual(kept, { devices: 0, lookup: "1111013800101820013" });
  } finally {
    cdp?.close();
    srv.close();
  }
});
