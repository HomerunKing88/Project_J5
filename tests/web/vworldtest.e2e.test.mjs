// 기기 시험 페이지의 VWorld 직접 연결 시험 e2e (J5-078, 리뷰 반영 PR #125). 헤드리스 Chromium(CDP), 브라우저가 없으면 건너뛴다.
// 앱의 서비스 워커가 설치된 뒤(배포 주소의 보통 흐름) 기기 시험 페이지에서 VWorld 를 부르면, 요청이 서비스 워커를 거치지 않고 페이지에서 바로 나가야 한다:
// 서비스 워커가 no-cors 로 바꿔 보내면 다른 출처 허용 여부를 잴 수 없고, 실제 키가 든 타일 주소가 타일 캐시에 남는다.
// VWorld 응답은 CDP Fetch 로 흉내 낸다(페이지가 낸 요청만 가로챈다). 가상 응답 시험이며 실제 VWorld·iPhone 확인은 아니다.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Cdp, WEB, findChrome, png1x1, serve } from "./cdp.mjs";

const chrome = findChrome();
const KEY = "E2EVWORLDKEY-0123456789abcdef";

test("VWorld 직접 연결 시험: 서비스 워커가 있어도 페이지에서 바로 부르고, 키가 든 주소를 타일 캐시에 남기지 않는다", { skip: chrome ? false : "Chromium 없음" }, async () => {
  const tmp = mkdtempSync(join(tmpdir(), "j5-vwtest-"));
  const srv = await serve(WEB);
  const base = `http://127.0.0.1:${srv.address().port}`;
  let cdp;
  try {
    cdp = await Cdp.launch(chrome, tmp);
    // 앱을 한 번 열어 서비스 워커를 설치한다
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.2.38')");
    await cdp.waitFor("navigator.serviceWorker.ready.then(() => !!navigator.serviceWorker.controller)", 15000);

    const seen = [];
    cdp.on("Fetch.requestPaused", async (p) => {
      seen.push(p.request.url);
      const isTile = p.request.url.includes("/req/wmts/");
      const body = isTile ? png1x1([10, 20, 30]) : Buffer.from(JSON.stringify({ response: { status: "OK", result: { featureCollection: { features: [{ properties: { pnu: "1111013800101820013", jibun: "182-13대" } }] } } } }));
      await cdp.send("Fetch.fulfillRequest", { requestId: p.requestId, responseCode: 200, body: body.toString("base64"),
        responseHeaders: [{ name: "content-type", value: isTile ? "image/png" : "application/json" }, { name: "access-control-allow-origin", value: "*" }] });
    });
    await cdp.send("Fetch.enable", { patterns: [{ urlPattern: "https://api.vworld.kr/*" }] });

    await cdp.navigate(`${base}/devtest.html`);
    assert.equal(await cdp.eval("!!navigator.serviceWorker.controller"), true, "서비스 워커가 맡은 기기 시험 페이지");
    await cdp.eval(`document.getElementById('vw-key').value = ${JSON.stringify(KEY)}; document.getElementById('vw-run').click(); 'ok'`);
    await cdp.waitFor("!document.getElementById('vw-note').textContent.includes('시험 중') && document.getElementById('vw-note').textContent !== ''", 15000);
    const r = JSON.parse(await cdp.eval("document.getElementById('results').textContent"));
    const v = r.vworld[0];
    assert.equal(v.parcel.outcome, "ok", `필지 조회가 페이지에서 바로 나가 다른 출처 허용 응답을 받는다: ${JSON.stringify(v.parcel).slice(0, 300)}`);
    assert.ok(v.parcel.fields.includes("response.result.featureCollection.features[0].properties.pnu"), "응답 필드 이름 경로를 남긴다");
    assert.equal(v.tile.outcome, "ok");
    assert.ok(seen.some((u) => u.includes("/req/data?")) && seen.some((u) => u.includes("/req/wmts/")), "두 요청 모두 페이지에서 나갔다");
    assert.ok(!JSON.stringify(r).includes(KEY), "결과 JSON 에 키 없음");
    const cached = await cdp.eval("caches.has('j5-tiles-v1').then((h) => h ? caches.open('j5-tiles-v1').then((c) => c.keys()).then((ks) => ks.map((k) => k.url)) : [])");
    assert.ok(!cached.some((u) => u.includes(KEY) || u.includes(encodeURIComponent(KEY))), `타일 캐시에 키가 든 주소 없음: ${cached.join(",")}`);
  } finally {
    cdp?.close();
    srv.close();
  }
});
