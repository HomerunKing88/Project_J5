// 연습용 모드 e2e (J5-077). 연습용 자료(가상 대상·필지·윤곽·거래)는 ?demo=1 로 켠 폰에서만 넣을 수 있고, 연습용 모드를 끄면 앱에 든 연습용 자료를 걷어 낸다.
// 헤드리스 Chromium(CDP), 브라우저가 없으면 건너뛴다. 가상 서버 시험이며 실제 iPhone 확인은 아니다.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Cdp, WEB, findChrome, serve } from "./cdp.mjs";

const chrome = findChrome();
const appUp = "document.getElementById('status-line').textContent.includes('앱 0.2.37')";
const shown = (id) => `(() => { const e = document.getElementById('${id}'); return !!e && !e.hidden && getComputedStyle(e).display !== 'none' && e.getClientRects().length > 0; })()`;

test("연습용 모드: 기본은 실제 자료, ?demo=1 에서만 연습용 자료, 끄면 걷어 냄", { skip: chrome ? false : "Chromium 없음" }, async () => {
  const tmp = mkdtempSync(join(tmpdir(), "j5-demo-"));
  const srv = await serve(WEB);
  const base = `http://127.0.0.1:${srv.address().port}`;
  let cdp;
  try {
    cdp = await Cdp.launch(chrome, tmp);
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });

    // 1. 처음 실행(연습용 모드 아님): 배지 없음, 연습용 버튼·안내 없음, 자료 종류 실제, 연습용 종류는 고를 수 없음
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor(appUp);
    await cdp.waitFor("document.getElementById('field-sheet').dataset.state === 'open'");
    assert.equal(await cdp.eval("document.getElementById('mode-badge').hidden"), true, "실제 자료면 배지를 감춘다");
    assert.equal(await cdp.eval(shown("home-load-synthetic")), false, "연습용 자료 버튼 없음");
    assert.ok(await cdp.eval("document.getElementById('home-view-file').closest('label').getClientRects().length > 0"), "PC 자료 파일 가져오기는 보인다");
    assert.ok(!(await cdp.eval("document.getElementById('field-sheet').innerText")).includes("연습용"), "현장 시트에 연습용 글 없음");
    assert.equal(await cdp.eval("document.getElementById('data-mode').value"), "private_real");
    assert.equal(await cdp.eval("document.querySelector('#data-mode option[value=synthetic]').disabled"), true);
    // 기본값을 저장해 둔다: 비어 있으면 PC 자료 파일을 처음 넣을 때 파일의 자료 종류(연습용 포함)를 받는다(리뷰 반영 PR #124)
    const storedMode = "new Promise((ok, no) => { const r = indexedDB.open('j5'); r.onerror = no; r.onsuccess = () => { const g = r.result.transaction('meta').objectStore('meta').get('data_mode'); g.onsuccess = () => { ok(g.result ?? null); r.result.close(); }; g.onerror = no; }; })";
    assert.equal(await cdp.eval(storedMode), "private_real");
    for (const id of ["load-synthetic", "load-synthetic-parcels", "load-synthetic-basemap"]) {
      assert.equal(await cdp.eval(`getComputedStyle(document.getElementById('${id}')).display`), "none", `${id} 감춤`);
    }

    // 2. ?demo=1: 자료 종류를 연습용으로 저장 → 연습용 자료로 둘러보기 → 대상·필지·윤곽·거래, 배지 연습용
    await cdp.navigate(`${base}/index.html?demo=1`);
    await cdp.waitFor(appUp);
    // 이미 실제로 저장된 폰은 연습용 모드를 켜도 실제 그대로이고, 설정에서 연습용을 고를 수 있다
    assert.equal(await cdp.eval("document.getElementById('data-mode').value"), "private_real");
    assert.equal(await cdp.eval("document.querySelector('#data-mode option[value=synthetic]').disabled"), false);
    await cdp.eval("document.getElementById('study-id').value = 'demo-study'; document.getElementById('data-mode').value = 'synthetic'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor("document.getElementById('settings-note').textContent === '저장했습니다'");
    assert.equal(await cdp.eval("document.getElementById('mode-badge').textContent"), "연습용 자료");
    assert.equal(await cdp.eval("document.getElementById('mode-badge').hidden"), false);
    await cdp.eval("document.getElementById('home-load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.getElementById('seed-status').textContent.startsWith('대상 5곳')");
    await cdp.waitFor("!document.getElementById('sample-item').hidden");
    // 다시 열어도(주소에 demo 없음) 연습용 모드가 유지된다
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor(appUp);
    assert.ok((await cdp.eval("document.getElementById('seed-status').textContent")).startsWith("대상 5곳"), "연습용 모드 유지 중에는 연습용 자료가 그대로");

    // 3. ?demo=0: 연습용 대상·필지·윤곽·거래를 걷어 내고, 연습용 기록이 없으므로 자료 종류를 실제로 바꾼다. 작업 공간 이름은 그대로
    await cdp.navigate(`${base}/index.html?demo=0`);
    await cdp.waitFor(appUp);
    await cdp.waitFor("document.getElementById('seed-status').textContent === '가져온 대상 목록이 없습니다.'");
    assert.equal(await cdp.eval("document.getElementById('sample-item').hidden"), true, "지울 연습용 자료가 남지 않는다");
    assert.equal(await cdp.eval("document.getElementById('mode-badge').hidden"), true);
    assert.equal(await cdp.eval("document.getElementById('data-mode').value"), "private_real");
    assert.equal(await cdp.eval("document.getElementById('study-id').value"), "demo-study");
    assert.equal(await cdp.eval(shown("home-empty")), true, "빈 상태 시작하기");
    assert.deepEqual(cdp.errors.filter((e) => !e.includes("favicon")), []);
  } finally {
    await cdp?.close();
    srv.close();
  }
});
