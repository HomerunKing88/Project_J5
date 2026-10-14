// 모바일 현장 앱 UX e2e (J5-020, 화면 구조 J5-054). 헤드리스 Chromium(CDP)으로 사용자 행동 순서를 따라간다. 브라우저가 없으면 건너뛴다.
// 흐름: 처음 실행 빈 상태 → 연습용 자료 → 대상 카드 → 관측 화면(포커스·필수값 누락·중복 사진·저장 실패·저장 성공·다음 행동) →
//       미내보냄 수 → 내보내기 4단계(저장 확인 전/후) → 새로고침 유지 → 해시 이동 → 오프라인 배너 → 지도 실패 시 목록 → 모바일 내비게이션·접근성 속성.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Cdp, WEB, findChrome, png1x1, serve, waitForDownloads } from "./cdp.mjs";

const chrome = findChrome();
const go = (cdp, view) => cdp.eval(`document.getElementById('nav-${view}').click(); 'ok'`);
const visible = (id) => `!document.getElementById('${id}').hidden`;
const txt = (id) => `document.getElementById('${id}').textContent`;

test("현장 앱 UX: 빈 상태 → 대상 → 관측 → 내보내기 → 유지·오프라인·접근성", { skip: chrome ? false : "Chromium 없음" }, async () => {
  const tmp = mkdtempSync(join(tmpdir(), "j5-ux-"));
  const srv = await serve(WEB);
  const base = `http://127.0.0.1:${srv.address().port}`;
  let cdp;
  try {
    cdp = await Cdp.launch(chrome, tmp);
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
    // 저장 실패를 흉내 내는 훅: window.__failSave 가 켜져 있으면 events 스토어 쓰기가 실패한다 (용량 부족 등)
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      const origAdd = IDBObjectStore.prototype.add;
      IDBObjectStore.prototype.add = function (v, k) { if (this.name === 'events' && window.__failSave) throw new DOMException('저장 공간 부족 (e2e)', 'QuotaExceededError'); return origAdd.call(this, v, k); };` });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor(`${txt("status-line")}.includes('앱 0.2.36')`);

    // 1. 처음 실행: 현장 화면, 시트가 펼쳐져 시작하기(PC 자료 파일·연습용 자료)가 보인다, 기술 용어 없음
    assert.ok(await cdp.eval(visible("view-map")) && await cdp.eval("document.getElementById('view-records').hidden"), "첫 화면은 현장");
    assert.equal(await cdp.eval("document.getElementById('nav-map').getAttribute('aria-current')"), "page");
    await cdp.waitFor("document.getElementById('field-sheet').dataset.state === 'open'");   // 자료가 없으면 시트를 펼쳐 시작하기를 보인다
    assert.equal(await cdp.eval("document.getElementById('sheet-toggle').getAttribute('aria-expanded')"), "true");
    assert.ok(await cdp.eval(visible("home-empty")) && await cdp.eval("document.getElementById('start-observing').hidden"), "빈 상태 안내");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li.empty').length"), 1);
    assert.equal(await cdp.eval(txt("mode-badge")), "연습용 자료", "자료 종류는 항상 보인다");
    const homeText = await cdp.eval("document.getElementById('field-sheet').innerText");
    for (const w of ["study_id", "asset_id", "origin", "synthetic", "j5 db", ".json", "물건", "정본"]) assert.ok(!homeText.includes(w), `현장 시트에 내부 용어 없음: ${w}`);
    assert.ok(!(await cdp.eval("document.body.scrollWidth > window.innerWidth")), "가로 스크롤 없음 (빈 상태)");

    // 설정 저장 (작업 공간 이름) → 연습용 자료
    await cdp.eval("document.getElementById('study-id').value = 'ux-study'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor(`${txt("settings-note")} === '저장했습니다'`);
    await cdp.eval("document.getElementById('home-load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li .row-act').length === 5");
    // 시작하기의 연습용 자료는 대상·필지·건물 윤곽을 함께 넣고, 시트를 접어 지도를 보인다
    await cdp.waitFor("document.getElementById('field-sheet').dataset.state === 'peek' && document.querySelectorAll('#map-svg path.parcel').length === 6");
    assert.match(await cdp.eval(txt("home-note")), /대상 5곳을 가져왔습니다/);
    assert.equal(await cdp.eval("document.getElementById('sheet-toggle').getAttribute('aria-expanded')"), "false");
    // 지도는 접힌 시트 위의 보이는 영역에 맞춘다: 위치점이 모두 시트 위에 있다
    const sheetTop = await cdp.eval("document.getElementById('field-sheet').getBoundingClientRect().top");
    const ptBottoms = await cdp.eval("Array.from(document.querySelectorAll('#map-svg g.pt .dot')).map(d => d.getBoundingClientRect().bottom)");
    assert.ok(ptBottoms.length === 4 && ptBottoms.every((b) => b < sheetTop), `위치점이 시트에 가려지지 않음 ${JSON.stringify({ sheetTop, ptBottoms })}`);
    await cdp.eval("document.getElementById('sheet-toggle').click(); 'ok'");
    await cdp.waitFor("document.getElementById('field-sheet').dataset.state === 'open'");
    // 2. 대상 줄: 이름·위치 요약·기록 없음 표시, 좌표 숫자 없음, 통계·주요 버튼
    assert.equal(await cdp.eval("document.querySelector('#asset-list li .title').textContent"), "가상 물건 1");
    assert.match(await cdp.eval("document.querySelector('#asset-list li .meta').textContent"), /아직 기록 없음/);
    assert.equal(await cdp.eval("document.querySelector('#asset-list li .row-act').getAttribute('aria-label')"), "가상 물건 1 기록하기");
    assert.doesNotMatch(await cdp.eval(txt("asset-list")), /126\.\d{3}/, "좌표 숫자를 기본 화면에 노출하지 않는다");
    assert.equal(await cdp.eval(txt("stat-targets")), "5");
    assert.ok(await cdp.eval("document.getElementById('home-empty').hidden") && await cdp.eval(visible("start-observing")));
    assert.equal(await cdp.eval(txt("start-observing")), "기록 시작");
    assert.ok(!(await cdp.eval("document.body.scrollWidth > window.innerWidth")), "가로 스크롤 없음 (목록)");
    const smallTargets = await cdp.eval("Array.from(document.querySelectorAll('#view-map button, .bottom-nav button')).filter(b => !b.hidden && b.getBoundingClientRect().height > 0 && b.getBoundingClientRect().height < 44 && b.id !== 'sheet-toggle').map(b => b.id || b.textContent)");
    assert.deepEqual(smallTargets, [], "터치 대상 44px 이상 (시트 손잡이는 끌기 띠라 제외)");
    // 줄을 누르면 지도에서 고르고 시트 위에 대상 카드가 열린다. 카드가 열리면 시트 머리의 '기록 시작' 은 숨긴다 (기록하기는 하나만)
    await cdp.eval("document.querySelectorAll('#asset-list li .row-main')[1].click(); 'ok'");
    await cdp.waitFor(`${visible("map-selected")} && ${txt("map-selected-label")} === '가상 물건 2'`);
    assert.ok(await cdp.eval("document.getElementById('start-observing').hidden"));
    assert.equal(await cdp.eval("document.activeElement.id"), "map-selected-start");
    await cdp.eval("document.getElementById('map-selected-close').click(); 'ok'");
    await cdp.waitFor("document.getElementById('map-selected').hidden");
    assert.ok(await cdp.eval(visible("start-observing")));
    assert.equal(await cdp.eval("document.activeElement.id"), "sheet-toggle", "카드를 닫으면 시트 손잡이로 포커스");

    // 3. 관측 시작: 첫 대상, 작업 화면에 포커스, dialog 속성
    await cdp.eval("const b = document.getElementById('start-observing'); b.focus(); b.click(); 'ok'");
    await cdp.waitFor(visible("sec-observe"));
    assert.equal(await cdp.eval(txt("target-label")), "가상 물건 1");
    assert.equal(await cdp.eval("document.activeElement.id"), "observe-title", "열리면 제목으로 포커스");
    assert.equal(await cdp.eval("document.getElementById('sec-observe').getAttribute('aria-modal')"), "true");
    // 4. 필수값 누락: 상태 없이 저장 → 오류, 기록 없음
    await cdp.eval("document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor(`${txt("observe-note")}.includes('달라짐·그대로·확인 어려움 가운데 하나를 고릅니다')`);
    assert.equal(await cdp.eval("document.getElementById('observe-note').className"), "bad");
    assert.equal(await cdp.eval(txt("stat-records")), "0");
    // 5. 같은 사진 두 번: 두 번째 항목은 오류로 표시되고 저장이 막힌다
    const photo = join(tmp, "front.png");
    writeFileSync(photo, png1x1([0, 128, 255]));
    await cdp.setFiles("#photos", [photo]);
    await cdp.waitFor("document.querySelectorAll('#photo-list .photo-item').length === 1");
    await cdp.setFiles("#photos", [photo]);
    await cdp.waitFor("document.querySelectorAll('#photo-list .photo-item').length === 2");
    assert.match(await cdp.eval("document.querySelectorAll('#photo-list .photo-item .bad')[0].textContent"), /같은 사진이 이미 선택됨/);
    await cdp.eval("document.getElementById('status-change_observed').click(); document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor(`${txt("observe-note")}.includes('빨간 글이 붙은 사진을 뺍니다')`);
    await cdp.eval("document.querySelectorAll('#photo-list .photo-item button')[1].click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#photo-list .photo-item').length === 1");
    // 6. 저장 실패(저장소 오류): '저장됨' 으로 표시하지 않고 기록도 남지 않는다
    await cdp.eval("window.__failSave = true; document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor(`${txt("observe-note")}.startsWith('저장하지 못했습니다')`);
    assert.match(await cdp.eval(txt("observe-note")), /기록은 남지 않았습니다/);
    assert.ok(await cdp.eval(visible("observe-form")) && await cdp.eval("document.getElementById('observe-done').hidden"), "실패하면 입력 화면 그대로");
    assert.equal(await cdp.eval(txt("stat-records")), "0");
    // 7. 저장 성공 → 완료 화면, 포커스, 통계·배지, 다음 대상 제안
    await cdp.eval("window.__failSave = false; document.getElementById('note').value = 'UX e2e'; document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor(`${txt("observe-note")}.startsWith('이 폰에 저장했습니다')`);
    await cdp.waitFor(visible("observe-done"));
    assert.equal(await cdp.eval("document.activeElement.id"), "done-title", "저장 뒤 완료 제목으로 포커스");
    assert.equal(await cdp.eval(txt("done-text")), "가상 물건 1, 달라짐, 사진 1장. 이 폰에 저장했고 아직 PC 로 보내지 않았습니다.");
    assert.equal(await cdp.eval(txt("stat-records")), "1");
    assert.equal(await cdp.eval(txt("stat-unexported")), "1");
    assert.ok(await cdp.eval("document.getElementById('stat-unexported').parentElement.classList.contains('has')"), "보낼 기록이 있으면 형광펜");
    assert.equal(await cdp.eval(txt("nav-records-badge")), "1", "기록 탭 배지 = 안 보낸 기록 수");
    assert.equal(await cdp.eval(txt("done-next")), "다음: 가상 물건 2");
    // 8. 다음 대상 기록 → 새 입력 화면, Esc 로 닫기 → 목록의 카드에 마지막 기록 표시
    await cdp.eval("document.getElementById('done-next').click(); 'ok'");
    await cdp.waitFor(`${visible("observe-form")} && ${txt("target-label")} === '가상 물건 2'`);
    await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await cdp.waitFor("document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.activeElement.id"), "start-observing", "이어서 열었다 닫아도 처음 연 버튼으로 포커스가 돌아온다");
    assert.deepEqual(await cdp.eval("Array.from(document.querySelector('#asset-list li .meta').children).map(c => c.textContent)").then((a) => [a[0], a[2], a[3], a[4]]),
      ["달라짐", "기록 1건", "사진 1장", "안 보냄 1"]);
    assert.ok(await cdp.eval("!!document.querySelector('#asset-list li .meta .unsent')"), "안 보낸 기록은 형광펜 표시");
    assert.match(await cdp.eval(txt("home-last")), /^최근 기록 오늘 .*, 가상 물건 1\. 아직 PC 로 안 보냈습니다\.$/);
    assert.equal(await cdp.eval(txt("start-observing")), "이어서 기록");

    // 9. PC 로 보내기 (기록 화면): 1 확인 → 2 만들기 → 3 저장 → 4 확인. 지금 단계의 버튼만 보이고, 저장 확인 전에는 안 보낸 기록 그대로
    await go(cdp, "records");
    await cdp.waitFor(`${visible("view-records")} && ${txt("export-summary")}.startsWith('보낼 기록 1건, 사진 1장')`);
    assert.equal(await cdp.eval("document.activeElement.id"), "records-title", "화면 전환 시 제목으로 포커스");
    assert.equal(await cdp.eval("document.querySelector('#export-steps .current').dataset.step"), "1");
    assert.deepEqual(await cdp.eval("['export-prepare', 'export-save', 'export-confirm'].map(id => !document.getElementById(id).hidden)"), [true, false, false]);
    await cdp.eval("document.getElementById('export-prepare').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('export-save').disabled");
    assert.equal(await cdp.eval("document.querySelector('#export-steps .current').dataset.step"), "3");
    assert.deepEqual(await cdp.eval("['export-prepare', 'export-save', 'export-confirm'].map(id => !document.getElementById(id).hidden)"), [false, true, false]);
    const dl = join(tmp, "dl"); mkdirSync(dl);
    await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dl, eventsEnabled: true });
    await cdp.clickSelector("#export-save");
    await waitForDownloads(dl, 1);
    await cdp.waitFor("!document.getElementById('export-confirm').disabled");
    assert.equal(await cdp.eval("document.querySelector('#export-steps .current').dataset.step"), "4");
    assert.match(await cdp.eval(txt("export-note")), /누르기 전에는 보낸 기록으로 치지 않습니다/);
    assert.deepEqual(await cdp.eval("['export-prepare', 'export-save', 'export-confirm'].map(id => !document.getElementById(id).hidden)"), [false, true, true], "저장 확인과 다시 저장");
    assert.equal(await cdp.eval(txt("export-save")), "다시 저장");
    assert.equal(await cdp.eval(txt("stat-unexported")), "1", "파일 저장만으로는 보낸 기록이 아니다");
    await cdp.eval("document.getElementById('export-confirm').click(); 'ok'");
    await cdp.waitFor(`${txt("export-note")}.startsWith('보냈습니다')`);
    assert.match(await cdp.eval(txt("export-note")), /반영과 백업은 PC 에서 확인합니다/, "보내기 성공을 PC 반영으로 표시하지 않는다");
    assert.equal(await cdp.eval(txt("stat-unexported")), "0");
    assert.ok(await cdp.eval("document.getElementById('nav-records-badge').hidden"));
    assert.equal(await cdp.eval("document.querySelector('#export-steps .current').dataset.step"), "1", "끝나면 다시 1단계");
    assert.match(await cdp.eval(txt("home-last")), /PC 로 보냈습니다\.$/);
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li .badge.state-exported').length"), 1);
    assert.equal(await cdp.eval("document.querySelector('#record-list li .badge.state-exported').textContent"), "보냄");

    // 10. 새로고침 뒤 유지 + 해시로 화면 이동 (같은 문서의 해시 이동이 아니라 실제 재방문이 되도록 쿼리를 바꾼다)
    await cdp.navigate(`${base}/index.html?reload=1#records`);
    await cdp.waitFor(`${txt("stat-records")} === '1'`);
    assert.ok(await cdp.eval(visible("view-records")) && await cdp.eval("document.getElementById('view-map').hidden"), "해시로 기록 화면");
    assert.equal(await cdp.eval(txt("stat-targets")), "5");
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), 1);
    assert.equal(await cdp.eval(txt("mode-badge")), "연습용 자료");

    // 11. 오프라인 배너
    await cdp.send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await cdp.waitFor(visible("offline-banner"));
    // 배너가 여러 줄이 돼도(좁은 화면·큰 글자) 상단 바 아래 화면이 배너에 덮이지 않는다 (리뷰 반영 PR #101): 기록 화면과 현장 화면 모두
    const appbarBottom = () => // 상단 바의 아래 괘선 1px 은 화면과 겹친다
      cdp.eval("document.querySelector('.appbar').getBoundingClientRect().bottom");
    await cdp.eval("document.getElementById('offline-banner').textContent += ' 글자를 크게 키운 폰처럼 여러 줄이 되는 경우를 흉내 냅니다. 화면이 배너 아래로 내려가야 합니다.'; 'ok'");
    await cdp.waitFor("document.getElementById('offline-banner').getBoundingClientRect().height > 60");
    await cdp.waitFor(`document.getElementById('records-title').getBoundingClientRect().top >= document.querySelector('.appbar').getBoundingClientRect().bottom - 1.5`);
    await go(cdp, "map");
    await cdp.waitFor(visible("view-map"));
    assert.ok(await cdp.eval("document.getElementById('view-map').getBoundingClientRect().top") >= (await appbarBottom()) - 1.5, "현장 지도가 여러 줄 배너 아래에서 시작한다");
    await go(cdp, "records");
    await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await cdp.waitFor("document.getElementById('offline-banner').hidden");

    // 12. 지도 실패(SVG 를 만들 수 없는 환경을 흉내): 지도 화면은 안내, 목록 흐름은 그대로. 서비스 워커가 map.js 를 캐시하므로 URL 차단 대신 스텁을 쓴다
    const { identifier: svgStub } = await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      const orig = document.createElementNS.bind(document);
      document.createElementNS = (ns, name) => { if (String(ns).endsWith('/svg')) throw new Error('e2e: SVG 생성 불가'); return orig(ns, name); };` });
    await cdp.navigate(`${base}/index.html?reload=2#map`);
    await cdp.waitFor(`${txt("map-note")}.includes('지도를 그리지 못했습니다')`);
    assert.match(await cdp.eval(txt("map-empty")), /지도를 그릴 수 없습니다/);
    await cdp.waitFor("document.getElementById('field-sheet').dataset.state === 'open'");   // 지도가 실패하면 목록을 펼친다
    // 지도 실패 안내는 목록보다 먼저 뜬다 (initMap 이 목록 그리기 전). 목록이 다 그려질 때까지 기다린다 (부하가 크면 앞서 누르던 경합)
    await cdp.waitFor("document.querySelectorAll('#asset-list li .row-act').length === 5");
    await cdp.eval("document.querySelectorAll('#asset-list li .row-act')[2].click(); 'ok'");
    await cdp.waitFor(`${visible("sec-observe")} && ${txt("target-label")} === '가상 물건 3'`);
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.activeElement.id"), "map-title", "되돌릴 요소가 없으면 현재 화면 제목으로 (숨은 대화상자에 포커스가 남지 않음)");
    assert.deepEqual(cdp.errors, [], "지도 실패 경로에서 콘솔 오류 없음");
    await cdp.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: svgStub });

    // 13. 모바일 내비게이션: 하단 고정, 콘텐츠와 겹치지 않음, 각 화면에서 가로 스크롤 없음
    await cdp.navigate(`${base}/index.html?reload=3`);
    await cdp.waitFor(`${txt("stat-targets")} === '5'`);
    const nav = await cdp.eval("(r => ({ bottom: r.bottom, height: r.height, top: r.top }))(document.querySelector('.bottom-nav').getBoundingClientRect())");
    assert.ok(Math.abs(nav.bottom - 844) < 1 && nav.height >= 44, `하단 내비게이션 고정 ${JSON.stringify(nav)}`);
    for (const v of ["records", "settings", "map"]) {
      await go(cdp, v);
      await cdp.waitFor(visible(`view-${v}`));
      assert.equal(await cdp.eval(`document.getElementById('nav-${v}').getAttribute('aria-current')`), "page");
      assert.equal(await cdp.eval("document.activeElement.tagName"), "H2", `${v}: 제목으로 포커스`);
      assert.ok(!(await cdp.eval("document.documentElement.scrollWidth > window.innerWidth + 1")), `${v}: 가로 스크롤 없음`);
      const lastBottom = await cdp.eval(`(() => { window.scrollTo(0, document.body.scrollHeight); const s = document.getElementById('view-${v}'); return s.getBoundingClientRect().bottom; })()`);
      assert.ok(lastBottom <= nav.top + 1, `${v}: 끝까지 내려도 내비게이션에 가려지지 않음 (${lastBottom} vs ${nav.top})`);
    }
    // 13b. 지도 선택 뒤 물건 목록을 교체하면 선택 카드가 닫힌다 (옛 물건으로 기록하지 않는다)
    // 예전 해시(#home, #export)는 합친 화면으로 연다 (J5-054)
    await cdp.navigate(`${base}/index.html?reload=4#export`);
    await cdp.waitFor(`${visible("view-records")} && ${txt("stat-targets")} === '5'`);
    assert.equal(await cdp.eval("location.hash"), "#records");
    await cdp.navigate(`${base}/index.html?reload=5#home`);
    await cdp.waitFor(`${visible("view-map")} && ${txt("stat-targets")} === '5'`);
    assert.equal(await cdp.eval("document.getElementById('field-sheet').dataset.state"), "peek", "자료가 있으면 시트는 접혀 시작");
    await cdp.waitFor("document.querySelectorAll('#map-svg g.pt').length === 4");
    await cdp.clickRect('#map-svg g.pt[data-asset-id="7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a51"] .hit');
    await cdp.waitFor(visible("map-selected"));
    const seed2 = join(tmp, "seed2.json");
    writeFileSync(seed2, JSON.stringify(JSON.parse(readFileSync(join(WEB, "data/assets.seed.synthetic.json"), "utf8")).slice(3)));
    await cdp.setFiles("#seed-file", [seed2]);
    await cdp.waitFor("document.querySelectorAll('#asset-list li .row-act').length === 2");
    assert.ok(await cdp.eval("document.getElementById('map-selected').hidden"), "사라진 물건의 선택 카드는 닫힌다");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt.sel').length"), 0);
    // 13c. 연습용 자료 지우기 (J5-061): 앱에 든 연습용 필지·윤곽·대상만 지우고, 파일에서 가져온 대상 목록과 저장한 기록은 그대로
    await go(cdp, "settings");
    await cdp.waitFor(visible("sample-item"));
    const recordsBefore = await cdp.eval("document.querySelectorAll('#record-list li').length");
    assert.ok(recordsBefore > 0, "지우기 전에 저장한 기록이 있다");
    await cdp.eval("document.getElementById('clear-sample').click(); 'ok'");
    await cdp.waitFor(`${txt("sample-note")}.includes('지웠습니다')`);
    assert.ok((await cdp.eval(txt("sample-note"))).includes("필지·건물·도로 윤곽") && !(await cdp.eval(txt("sample-note"))).includes("대상 목록"), "파일에서 가져온 대상 목록은 지우지 않는다");
    assert.ok(await cdp.eval("document.getElementById('sample-item').hidden"), "지울 연습용 자료가 없으면 감춘다");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li .row-act').length"), 2);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel').length"), 0, "연습용 필지가 지도에서 사라진다");
    await cdp.eval("document.getElementById('load-synthetic').click(); 'ok'");
    await cdp.waitFor(`${visible("sample-item")} && ${txt("stat-targets")} === '5'`);
    await cdp.eval("document.getElementById('clear-sample').click(); 'ok'");
    await cdp.waitFor(`${txt("sample-note")}.includes('대상 목록')`);
    assert.ok((await cdp.eval(txt("sample-note"))).includes("작업 공간에서 자료 종류를 실제 자료로"), "연습용 모드면 실제 자료로 바꾸는 곳을 알린다");
    assert.equal(await cdp.eval(txt("stat-targets")), "0");
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), recordsBefore, "저장한 기록은 지우지 않는다");
    // 13d. 예전 판이 저장한 인증키 든 주소 (리뷰 반영 PR #108): 다시 열면 그 주소와 그 주소로 받은 타일을 지우고 안내한다. 다른 타일은 남긴다 (가상 UUID)
    const fakeKey = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D";
    await cdp.eval(`(async () => {
      const db = await new Promise((res, rej) => { const q = indexedDB.open('j5'); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
      await new Promise((res) => { const tx = db.transaction('meta', 'readwrite'); tx.objectStore('meta').put('custom', 'tiles_provider'); tx.objectStore('meta').put('https://api.vworld.kr/req/wmts/1.0.0/{${fakeKey}}/Base/{z}/{y}/{x}.png', 'tiles_template'); tx.oncomplete = res; });
      db.close();
      const c = await caches.open('j5-tiles-v1');
      await c.put('https://api.vworld.kr/req/wmts/1.0.0/%7B${fakeKey}%7D/Base/15/1/2.png', new Response('x'));
      await c.put(location.origin + '/tiles/15/1/2.png', new Response('y'));
      return 'ok';
    })()`);
    await cdp.navigate(`${base}/index.html?reload=key#settings`);
    await cdp.waitFor(`${txt("tiles-note")}.includes('인증키가 들어 있어')`);
    assert.ok((await cdp.eval(txt("tiles-note"))).includes("타일 1장"), "키가 든 주소로 받은 타일만 지운다");
    assert.ok(!(await cdp.eval("document.body.innerText")).includes(fakeKey), "화면 어디에도 키가 없다");
    assert.equal(await cdp.eval("document.getElementById('tiles-template').value"), "");
    assert.equal(await cdp.eval("document.getElementById('tiles-provider').value"), "");
    assert.deepEqual(await cdp.eval("caches.open('j5-tiles-v1').then((c) => c.keys()).then((ks) => ks.map((r) => new URL(r.url).pathname))"), ["/tiles/15/1/2.png"]);
    assert.equal(await cdp.eval(`new Promise((res) => { const q = indexedDB.open('j5'); q.onsuccess = () => { const g = q.result.transaction('meta').objectStore('meta').get('tiles_template'); g.onsuccess = () => { res(g.result ?? null); q.result.close(); }; }; })`), null);
    await cdp.eval("caches.delete('j5-tiles-v1').then(() => 'ok')");
    // 13e. 자리 고르기 (J5-062): 지도 가운데 십자를 필지 위에 맞추면 그 필지 카드, 필지 밖이면 필지 번호 없는 이 폰의 대상을 만들고 바로 기록
    await cdp.eval("document.getElementById('load-synthetic-parcels').click(); 'ok'");
    await go(cdp, "map");
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel').length === 6");
    const PICK_AT = `(() => { const s = document.getElementById('map-svg').getBoundingClientRect(); const m = /translate\\(([-\\d.]+),([-\\d.]+)\\)/.exec(document.querySelector('#map-svg .layer-pick').getAttribute('transform') || ''); return m ? { x: s.left + Number(m[1]), y: s.top + Number(m[2]) } : null; })()`;
    const drag = async (from, to) => {
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: from.x, y: from.y });
      await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: from.x, y: from.y, button: "left", clickCount: 1 });
      for (let i = 1; i <= 8; i++) await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: from.x + ((to.x - from.x) * i) / 8, y: from.y + ((to.y - from.y) * i) / 8, button: "left", buttons: 1 });
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: to.x, y: to.y, button: "left", clickCount: 1 });
    };
    await cdp.eval("document.getElementById('map-pick').click(); 'ok'");
    await cdp.waitFor(`${visible("pick-bar")} && document.getElementById('map-pick').getAttribute('aria-pressed') === 'true' && document.querySelector('#map-svg .layer-pick').getAttribute('visibility') === 'visible'`);
    assert.equal(await cdp.eval("document.getElementById('field-sheet').dataset.state"), "peek", "고르는 동안 시트는 접힌다");
    let cross = await cdp.eval(PICK_AT);
    const pTarget = await cdp.eval("(() => { const p = document.querySelector('#map-svg path.parcel'); const b = p.getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2, pnu: p.dataset.pnu }; })()");
    const pickBarBottom = await cdp.eval("document.getElementById('pick-bar').getBoundingClientRect().bottom");
    assert.ok(cross.y > pickBarBottom, "십자는 막대에 가리지 않는다");
    // 누르는 곳은 비어 있는 지도(십자 자리)에서 시작한다(필지가 위 막대 아래에 있을 수 있다). 끌기는 평행 이동이라 같은 거리만큼 끌면 필지가 십자로 온다
    await drag(cross, { x: cross.x + (cross.x - pTarget.x), y: cross.y + (cross.y - pTarget.y) });
    await cdp.eval("document.getElementById('pick-ok').click(); 'ok'");
    await cdp.waitFor(`${visible("parcel-panel")} && ${visible("pick-bar")} === false`);
    assert.equal(await cdp.eval("document.querySelector('#map-svg .layer-pick').getAttribute('visibility')"), "hidden");
    assert.equal(await cdp.eval("document.getElementById('parcel-pnu').textContent"), pTarget.pnu, "필지 안이면 그 필지 카드");
    await cdp.eval("document.getElementById('map-pick').click(); 'ok'");
    await cdp.waitFor(`${visible("pick-bar")} && document.getElementById('parcel-panel').hidden`);
    // 고르는 중에는 필지·점을 눌러도 카드가 열리지 않고 고르기가 이어진다 (리뷰 반영 PR #109)
    const tapSel = await cdp.eval(`(() => { const top = document.getElementById('pick-bar').getBoundingClientRect().bottom + 30, bottom = document.getElementById('field-sheet').getBoundingClientRect().top - 30;
      const s = document.getElementById('map-svg').getBoundingClientRect(); const m = /translate\\(([-\\d.]+),([-\\d.]+)\\)/.exec(document.querySelector('#map-svg .layer-pick').getAttribute('transform')); const cx = s.left + Number(m[1]), cy = s.top + Number(m[2]);
      const p = Array.from(document.querySelectorAll('#map-svg path.parcel')).find((el) => { const b = el.getBoundingClientRect(); const x = b.left + b.width / 2, y = b.top + b.height / 2; return y > top && y < bottom && b.width > 8 && Math.hypot(x - cx, y - cy) > 60; });
      return p ? '#map-svg path.parcel[data-pnu="' + p.dataset.pnu + '"]' : null; })()`);
    assert.ok(tapSel, "누를 수 있는 필지가 보인다");
    await cdp.clickRect(tapSel);
    await new Promise((r) => setTimeout(r, 300));
    assert.ok(await cdp.eval("document.getElementById('parcel-panel').hidden && !document.getElementById('pick-bar').hidden"), "고르는 중 누르기는 무시");
    cross = await cdp.eval(PICK_AT);
    for (let i = 0; i < 3; i++) await drag({ x: cross.x, y: cross.y + 150 }, { x: cross.x, y: cross.y - 150 });   // 필지 묶음에서 멀리
    await cdp.eval("document.getElementById('pick-name').value = '  세운상가 옆 3층  '; document.getElementById('pick-ok').click(); 'ok'");
    await cdp.waitFor(`${visible("sec-observe")} && ${txt("target-label")} === '세운상가 옆 3층'`);
    const picked = await cdp.eval(`new Promise((res) => { const q = indexedDB.open('j5'); q.onsuccess = () => { const g = q.result.transaction('device_assets').objectStore('device_assets').getAll(); g.onsuccess = () => { res(g.result.filter((a) => a.label === '세운상가 옆 3층')); q.result.close(); }; }; })`);
    assert.equal(picked.length, 1);
    assert.equal(picked[0].pnu, null, "필지 밖이면 필지 번호를 채우지 않는다 (PC 에서 확인)");
    assert.equal(picked[0].origin, "device");
    const [plon, plat] = picked[0].location_point;
    assert.ok(plon > 126 && plon < 128 && plat > 37 && plat < 38, "위치점은 지도 가운데의 경도·위도");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('sec-observe').hidden");
    // 13f. 지도의 실거래 (J5-063): 연습용 거래를 넣으면 실거래가 켜져 필지·번지대 위에 종류+금액 꼬리표. 값·기간 바꾸기, 번지대 카드(필지 미확정), 필지 꼬리표 → 필지 카드
    // 기간은 오늘 날짜로 잡히므로 기대값도 같은 함수로 계산한다 (가상 거래의 날짜는 고정이라 해가 지나면 5년·3년 안의 수가 바뀐다)
    const { placeDeals: pdeals, dealsSince: dsince, dealSince: dfrom } = await import("../../web/app/view.js");
    const { localDate: today } = await import("../../web/app/time.js");
    const sampleTx = JSON.parse(readFileSync(join(WEB, "data/transactions.synthetic.json"), "utf8")).transactions;
    const sampleParcels = JSON.parse(readFileSync(join(WEB, "data/parcels.synthetic.j5parcels.json"), "utf8")).features;
    const expectDeals = (years) => { const txs = dsince(sampleTx, dfrom(years, today())); const r = pdeals(txs, sampleParcels);
      return { n: txs.length, labels: r.parcels.length + r.groups.length, text: `거래 ${txs.length}건` + (r.groups.length ? `, 번지대 ${r.groups.length}곳` : "") + (r.outside ? `, 지도 밖 ${r.outside}건` : "") }; };
    assert.ok(await cdp.eval("document.getElementById('map-deals').hidden"), "거래 자료가 없으면 실거래 버튼을 감춘다");
    await cdp.eval("document.getElementById('home-load-synthetic').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('map-deals').hidden && document.getElementById('map-deals').getAttribute('aria-pressed') === 'false'", 15000);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.deal').length"), 0, "연습용 거래를 넣어도 저절로 켜지지 않는다");
    await cdp.eval("document.getElementById('map-deals').click(); 'ok'");
    await cdp.waitFor(`document.getElementById('map-deals').getAttribute('aria-pressed') === 'true' && document.querySelectorAll('#map-svg g.deal').length === ${expectDeals(5).labels}`);
    await cdp.clickRect("#map-fit");   // 앞 단계에서 지도를 멀리 끌어 두었다
    assert.ok(await cdp.eval(visible("deal-legend")));
    assert.equal(await cdp.eval(txt("deal-count")), `총액, ${expectDeals(5).text}`, "기본 기간 5년");
    assert.match(await cdp.eval("document.querySelector('#map-svg g.deal[data-deal-id^=\"p:9999900100100020000\"]').textContent"), /업무24\.8억$/);
    await cdp.eval("document.querySelector('[data-deal-mode=\"land\"]').click(); 'ok'");
    await cdp.waitFor(`${txt("deal-count")}.startsWith('대지 평당')`);
    assert.match(await cdp.eval("document.querySelector('#map-svg g.deal[data-deal-id^=\"p:9999900100100020000\"] .deal-value').textContent"), /^1\.9억\/평$|만\/평$/, "대지 평당 단가");
    await cdp.eval("const s = document.getElementById('deal-years'); s.value = '0'; s.dispatchEvent(new Event('change')); 'ok'");
    await cdp.waitFor(`${txt("deal-count")} === '대지 평당, 거래 11건, 번지대 1곳, 지도 밖 1건'`);   // 전체 기간은 날짜와 무관
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.deal.masked').length"), 1, "번지대는 점선 꼬리표 하나");
    const groupSel = '#map-svg g.deal.masked .deal-bg';
    // 앞 단계의 먼 대상(세운상가 옆 3층) 때문에 필지 묶음이 작게 모여 꼬리표가 겹친다. 필지 묶음 위에서 휠로 확대해 벌린다
    for (let i = 0; i < 4 && await cdp.eval("document.querySelector('#map-svg g.deal.masked').getAttribute('visibility') !== 'visible'"); i++) {
      const c = await cdp.eval("(() => { const b = document.querySelector('#map-svg path.parcel[data-pnu=\"9999900100100010001\"]').getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; })()");
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseWheel", x: c.x, y: c.y, deltaX: 0, deltaY: -120 });
      await new Promise((r) => setTimeout(r, 150));
    }
    assert.equal(await cdp.eval("document.querySelector('#map-svg g.deal.masked').getAttribute('visibility')"), "visible");
    await cdp.clickRect(groupSel);
    await cdp.waitFor(`${visible("deal-panel")} && ${txt("deal-title")} === '가상동 1-*'`);
    assert.equal(await cdp.eval("document.querySelectorAll('#deal-list li').length"), 2);
    assert.match(await cdp.eval(txt("deal-note")), /필지 2곳 가운데 어느 필지의 거래인지 확정하지 않습니다/);
    assert.ok(await cdp.eval("document.getElementById('parcel-panel').hidden"), "카드는 하나만");
    // 펼친 시트·위 덮개 아래의 꼬리표는 보이지 않는다 (리뷰 반영 PR #110)
    assert.equal(await cdp.eval(`(() => { const top = document.getElementById('deal-legend').getBoundingClientRect().bottom, bottom = document.getElementById('field-sheet').getBoundingClientRect().top;
      return Array.from(document.querySelectorAll('#map-svg g.deal')).filter((g) => g.getAttribute('visibility') === 'visible').filter((g) => { const b = g.querySelector('.deal-bg').getBoundingClientRect(); return b.top < top - 1 || b.bottom > bottom + 1; }).length; })()`), 0);
    // 지도를 끌어 보이던 꼬리표 하나를 펼친 시트 경계에 걸치게 보내면 감춘다 (리뷰 반영 PR #110, 걸침은 PR #113: 일부가 가려지면 누르기 어렵다)
    const under = await cdp.eval(`(() => { const sheetTop = document.getElementById('field-sheet').getBoundingClientRect().top; const top = document.getElementById('deal-legend').getBoundingClientRect().bottom;
      const g = Array.from(document.querySelectorAll('#map-svg g.deal')).find((x) => x.getAttribute('visibility') === 'visible');
      const b = g.querySelector('.deal-bg').getBoundingClientRect(); return { id: g.dataset.dealId, dy: sheetTop - b.top - 12, x: 12, y: top + 12 }; })()`);
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: under.x, y: under.y });
    await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: under.x, y: under.y, button: "left", clickCount: 1 });
    for (let i = 1; i <= 8; i++) await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: under.x, y: under.y + (under.dy * i) / 8, button: "left", buttons: 1 });
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: under.x, y: under.y + under.dy, button: "left", clickCount: 1 });
    assert.equal(await cdp.eval(`document.querySelector('#map-svg g.deal[data-deal-id="${under.id}"]').getAttribute('visibility')`), "hidden", "시트 경계에 걸친 꼬리표는 감춘다");
    // 기간을 바꿔 그 번지대 거래가 없어지면 열린 카드를 닫는다 (리뷰 반영 PR #110: 1년 안에는 1-* 거래가 없다)
    await cdp.eval("(() => { const y = document.getElementById('deal-years'); y.value = '1'; y.dispatchEvent(new Event('change')); return 'ok'; })()");
    await cdp.waitFor("document.getElementById('deal-panel').hidden");
    await cdp.eval("(() => { const y = document.getElementById('deal-years'); y.value = '0'; y.dispatchEvent(new Event('change')); return 'ok'; })()");
    await cdp.waitFor(`${txt("deal-count")} === '대지 평당, 거래 11건, 번지대 1곳, 지도 밖 1건'`);   // 전체 기간은 날짜와 무관
    if (await cdp.eval("document.getElementById('field-sheet').dataset.state === 'open'")) await cdp.eval("document.getElementById('sheet-toggle').click(); 'ok'");
    await cdp.waitFor("document.getElementById('field-sheet').dataset.state === 'peek'");
    await cdp.clickRect("#map-fit");
    await new Promise((r) => setTimeout(r, 400));
    // 가로로 지도 왼쪽 끝에 걸친 꼬리표도 감춘다 (리뷰 반영 PR #113: 전체가 덮이지 않은 영역에 들어야 보인다)
    const edge = await cdp.eval(`(() => { const top = document.getElementById('deal-legend').getBoundingClientRect().bottom;
      const gs = Array.from(document.querySelectorAll('#map-svg g.deal')).filter((x) => x.getAttribute('visibility') === 'visible');
      const g = gs.sort((a, b) => a.querySelector('.deal-bg').getBoundingClientRect().left - b.querySelector('.deal-bg').getBoundingClientRect().left)[0];
      if (!g) return null; const b = g.querySelector('.deal-bg').getBoundingClientRect(); return { id: g.dataset.dealId, dx: -(b.left + b.width / 2), x: 250, y: top + 12 }; })()`);
    assert.ok(edge && edge.x + edge.dx >= 0, JSON.stringify(edge));
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: edge.x, y: edge.y });
    await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: edge.x, y: edge.y, button: "left", clickCount: 1 });
    for (let i = 1; i <= 8; i++) await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: edge.x + (edge.dx * i) / 8, y: edge.y, button: "left", buttons: 1 });
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: edge.x + edge.dx, y: edge.y, button: "left", clickCount: 1 });
    assert.equal(await cdp.eval(`document.querySelector('#map-svg g.deal[data-deal-id="${edge.id}"]').getAttribute('visibility')`), "hidden", "왼쪽 끝에 반쯤 걸친 꼬리표는 감춘다");
    await cdp.clickRect("#map-fit");
    await new Promise((r) => setTimeout(r, 400));
    // 펼친 시트·위 덮개에 가리지 않은 필지 꼬리표를 누른다
    const exactSel = await cdp.eval(`(() => { const top = document.getElementById('deal-legend').getBoundingClientRect().bottom, bottom = document.getElementById('field-sheet').getBoundingClientRect().top;
      const g = Array.from(document.querySelectorAll('#map-svg g.deal:not(.masked)')).find((x) => { const b = x.querySelector('.deal-bg').getBoundingClientRect(); return x.getAttribute('visibility') === 'visible' && b.top > top && b.bottom < bottom; });
      return g ? '#map-svg g.deal[data-deal-id="' + g.dataset.dealId + '"] .deal-bg' : null; })()`);
    assert.ok(exactSel);
    await cdp.clickRect(exactSel);
    await cdp.waitFor(`${visible("parcel-panel")} && ${visible("parcel-deals")}`);
    assert.ok(await cdp.eval("document.getElementById('deal-panel').hidden"));
    // 주변 사례 (J5-064): 같은 동의 거래 수·평당 단가, 연습용 필지에는 공시지가가 없어 참고 추정을 내지 않는다. 기간은 지도 실거래와 같다
    assert.ok(await cdp.eval(visible("parcel-comps")));
    assert.equal(await cdp.eval("document.querySelector('#comps-facts li .v').textContent"), "11건", "전체 기간의 가상동 거래 11건 (토지·단독·다가구 포함)");
    assert.match(await cdp.eval("document.querySelectorAll('#comps-facts li')[1].textContent"), /^대지 평당중간 [0-9,.억만]+\/평 \([0-9,.억만]+\/평~[0-9,.억만]+\/평, 10건\)$/, "지분 거래 1건은 단가에서 뺀다 (토지 거래는 토지 면적으로 든다)");
    assert.match(await cdp.eval(txt("comps-reference")), /견줄 수 있는 사례가 0건이라 참고값을 내지 않습니다/);
    assert.match(await cdp.eval("document.querySelectorAll('#comps-facts li')[2].textContent"), /^연면적 평당중간 [0-9,.억만]+\/평 \([0-9,.억만]+\/평~[0-9,.억만]+\/평, 9건\)$/, "연면적 평당도 범위 (토지 거래는 빠진다)");
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-list li').length"), 5);
    // 주변 사례 추이 (J5-067): 대지 평당이 있는 사례마다 점 하나, 연습용 필지에는 공시지가가 없어 선이 없다. 점을 누르면 값을 적고, 같은 값이 표에도 있다
    assert.ok(await cdp.eval(visible("comps-chart-wrap")));
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-chart g.chart-pt').length"), 10, "대지 평당 10건과 같은 수");
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-chart .chart-line path').length"), 0);
    assert.match(await cdp.eval(txt("comps-chart-title")), /^같은 동 거래 10건의 대지 평당 단가와 이 필지 공시지가의 평당 환산, 공시지가 자료 없음$/);
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-chart-table tbody tr').length"), 10);
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-chart [style]').length"), 0, "SVG 에 style 속성 없음 (CSP)");
    await cdp.clickRect("#comps-chart g.chart-pt:last-of-type .hit");
    await cdp.waitFor("document.querySelector('#comps-chart g.chart-pt.on') !== null");
    assert.match(await cdp.eval(txt("comps-chart-note")), /^\d{4}-\d{2}-\d{2} · \S+ · 대지 평당 [0-9,.억만]+\/평 · 가상동 /, "누른 점의 날짜·종류·대지 평당·지번");
    // 같은 동 연도별 시세 (J5-071): 2019년(가장 이른 거래)부터 올해까지 막대 하나씩, 연습용 거래는 해마다 대지 평당 사례가 3건 미만이라 변동을 적지 않고 막대는 테두리만
    const thisYear = Number(today().slice(0, 4)), nYears = thisYear - 2019 + 1;
    assert.ok(await cdp.eval(visible("comps-years-wrap")));
    assert.equal(await cdp.eval(txt("comps-years-scope")), `가상동, 2019~${thisYear}년 (${thisYear}년은 오늘까지)`);
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-years-chart g.chart-bar').length"), nYears, "거래가 없는 해도 칸이 있다");
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-years-chart g.chart-bar.few').length"), nYears, "대지 평당 사례 3건 미만인 해는 테두리만");
    assert.match(await cdp.eval(txt("comps-trend-summary")), /^대지 평당 사례가 3건 이상인 해가 0개라 시작·최근 변동을 적지 않습니다/);
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-years-table tbody tr').length"), nYears);
    assert.deepEqual(JSON.parse(await cdp.eval("JSON.stringify(Array.from(document.querySelectorAll('#comps-years-table tbody tr:last-child td'), (x) => x.textContent))")).map((v, k) => (k === 2 ? v.replace(/^[0-9,.억만]+\/평/, "_") : v)),
      ["2019", "1건", "_ (1건)", "수집 12/12개월"], "오래된 해가 표의 끝");
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-years-chart [style]').length"), 0, "SVG 에 style 속성 없음 (CSP)");
    await cdp.clickRect("#comps-years-chart g.chart-bar[data-year='2024'] .hit");
    await cdp.waitFor("document.querySelector('#comps-years-chart g.chart-bar.on') !== null");
    assert.match(await cdp.eval(txt("comps-years-note")), /^2024년 · 거래 3건 · 대지 평당 중간 [0-9,.억만]+\/평 \(2건, [0-9,.억만]+\/평~[0-9,.억만]+\/평\) · 수집 12\/12개월$/, "지분 거래는 거래 수에 들고 단가에서 빠진다");
    await cdp.eval("(() => { const m = document.getElementById('comps-trend-mode'); m.value = 'count'; m.dispatchEvent(new Event('change')); return 'ok'; })()");
    await cdp.waitFor(`${txt("comps-years-key")} === '그해 거래 수'`);
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-years-chart g.chart-bar.few').length"), thisYear - 2025, "거래 수는 덜 받은 해·올해만 테두리 (연습용 거래는 2025년까지 받았다)");
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-years-chart g.chart-bar rect.bar').length"), 7, "2019~2025년은 해마다 거래가 있고 받지 않은 해에는 막대가 없다");
    assert.deepEqual(JSON.parse(await cdp.eval("JSON.stringify(Array.from(document.querySelectorAll('#comps-years-table tbody tr:first-child td'), (x) => x.textContent))")).slice(1),
      ["미수집", "사례 없음", "미수집"], "리뷰 반영(PR #118): 받지 않은 올해는 거래 0건이 아니라 미수집");
    await cdp.clickRect(`#comps-years-chart g.chart-bar[data-year='${thisYear}'] .hit`);
    await cdp.waitFor(`${txt("comps-years-note")}.startsWith('${thisYear}년 (오늘까지) · 거래 미수집 · ')`);
    await cdp.eval("(() => { const m = document.getElementById('comps-trend-mode'); m.value = 'price'; m.dispatchEvent(new Event('change')); return 'ok'; })()");
    await cdp.waitFor(`${txt("comps-years-key")} === '그해 대지 평당 중간값'`);
    await cdp.eval("(() => { const y = document.getElementById('comps-years'); y.value = '3'; y.dispatchEvent(new Event('change')); return 'ok'; })()");
    await cdp.waitFor(`document.querySelector('#comps-facts li .v').textContent === '${expectDeals(3).n}건' && document.getElementById('deal-years').value === '3'`);   // 지도 실거래 기간도 같이
    await cdp.eval("(() => { const y = document.getElementById('comps-years'); y.value = '0'; y.dispatchEvent(new Event('change')); return 'ok'; })()");
    await cdp.waitFor("document.querySelector('#comps-facts li .v').textContent === '11건'");
    // 거래 종류 거르기 (J5-066): 주변 사례에서 토지만 고르면 지도 실거래도 토지만 (같은 선택)
    assert.ok(await cdp.eval(visible("comps-kind")) && await cdp.eval(visible("deal-kind")), "세 종류가 든 거래 파일이면 종류 고르기가 보인다");
    await cdp.eval("(() => { const k = document.getElementById('comps-kind'); k.value = 'land'; k.dispatchEvent(new Event('change')); return 'ok'; })()");
    await cdp.waitFor("document.querySelector('#comps-facts li .v').textContent === '1건' && document.getElementById('deal-kind').value === 'land'");
    assert.match(await cdp.eval(txt("comps-scope")), /, 전체 기간, 토지$/);
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-facts li')[2].textContent"), "연면적 평당면적이 있는 거래 없음", "토지 거래에는 연면적이 없다");
    assert.equal(await cdp.eval(txt("deal-count")), "대지 평당, 토지, 거래 1건");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.deal').length"), 1, "토지 매매가 있는 필지 4-2 하나");
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-chart g.chart-pt').length"), 1, "그래프도 같은 종류 (J5-067)");
    assert.match(await cdp.eval(txt("comps-years-scope")), /, 토지$/, "연도별 시세도 같은 종류 (J5-071)");
    assert.equal(await cdp.eval("document.querySelectorAll('#comps-years-chart g.chart-bar').length"), thisYear - 2020 + 1, "토지 거래는 2020년 1건");
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    // 켬·값·기간·종류는 이 폰에 남는다. 저장(IndexedDB)은 비동기라 끝난 것을 확인한 뒤 다시 연다 (CI 에서 저장 전에 다시 열려 종류가 남지 않은 적이 있다, PR #115)
    await cdp.waitFor(`new Promise((res) => { const q = indexedDB.open('j5'); q.onsuccess = () => { const g = q.result.transaction('meta').objectStore('meta').get('deal_view');
      g.onsuccess = () => { res(g.result?.kind === 'land' && g.result?.years === 0); q.result.close(); }; }; })`);
    await cdp.navigate(`${base}/index.html?reload=deals`);
    await cdp.waitFor(`document.querySelectorAll('#map-svg g.deal').length === 1 && ${txt("deal-count")} === '대지 평당, 토지, 거래 1건' && document.getElementById('deal-kind').value === 'land'`);
    await cdp.eval("(() => { const k = document.getElementById('deal-kind'); k.value = 'all'; k.dispatchEvent(new Event('change')); return 'ok'; })()");
    await cdp.waitFor(`document.querySelectorAll('#map-svg g.deal').length === 5 && ${txt("deal-count")}.startsWith('대지 평당, 거래 11건')`);
    await cdp.eval("document.getElementById('map-deals').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg g.deal').length === 0 && document.getElementById('deal-legend').hidden");
    // 거래 파일은 있으나 거래가 0건(수집했고 거래 없음)이면 주변 사례를 감추지 않고 0건으로 보인다 (리뷰 반영 PR #111)
    await cdp.eval(`new Promise((res) => { const q = indexedDB.open('j5'); q.onsuccess = () => { const tx = q.result.transaction('view', 'readwrite'); const st = tx.objectStore('view');
      const g = st.get('active'); g.onsuccess = () => { const v = g.result; v.transactions = { ...v.transactions, count: 0, transactions: [] }; st.put(v, 'active'); }; tx.oncomplete = () => { q.result.close(); res('ok'); }; }; })`);
    await cdp.navigate(`${base}/index.html?reload=nodeals`);
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel').length === 6");
    assert.ok(await cdp.eval("document.getElementById('map-deals').hidden"), "거래 0건이면 지도 실거래 버튼은 감춘다");
    await cdp.eval("document.getElementById('parcel-search').value = '2'; document.getElementById('parcel-search-form').requestSubmit(); 'ok'");
    await cdp.waitFor(`${visible("parcel-panel")} && ${visible("parcel-comps")}`);
    assert.equal(await cdp.eval("document.querySelector('#comps-facts li .v').textContent"), "0건");
    assert.equal(await cdp.eval(txt("comps-reference")), "이 기간에 같은 동의 거래가 없습니다.");
    assert.ok(await cdp.eval("document.getElementById('comps-kind').hidden"), "종류가 둘 미만이면 종류 고르기를 감춘다");
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    // 연습용 자료 지우기에 거래도 들어간다
    await go(cdp, "settings");
    await cdp.eval("document.getElementById('clear-sample').click(); 'ok'");
    await cdp.waitFor(`${txt("sample-note")}.includes('거래')`);
    assert.ok(await cdp.eval("document.getElementById('map-deals').hidden"));
    assert.match(await cdp.eval(txt("view-status")), /아직 가져오지 않았습니다/);
    await go(cdp, "map");
    // 14. 접근성 속성: 입력마다 label, 아이콘 버튼 aria-label, 상태 메시지 live, 제목 순서
    const a11y = await cdp.eval(`(() => {
      const unlabeled = Array.from(document.querySelectorAll('input:not([type=hidden]), select, textarea')).filter(i => !(i.labels && i.labels.length) && !i.getAttribute('aria-label') && !i.getAttribute('aria-labelledby')).map(i => i.id || i.name);
      const iconNoLabel = Array.from(document.querySelectorAll('button.icon')).filter(b => !b.getAttribute('aria-label')).map(b => b.id);
      const live = ['observe-note', 'export-note', 'settings-note', 'seed-note', 'parcels-note', 'basemap-note', 'view-note', 'tiles-note', 'tiles-prefetch-note', 'map-note', 'sample-note', 'pick-note', 'deal-count', 'comps-reference'].filter(id => !document.getElementById(id).getAttribute('role') && !document.getElementById(id).getAttribute('aria-live'));
      const heads = Array.from(document.querySelectorAll('h1, h2, h3')).map(h => Number(h.tagName[1]));
      let bad = false; for (let i = 1; i < heads.length; i++) if (heads[i] - heads[i - 1] > 1) bad = true;
      return { unlabeled, iconNoLabel, live, headingJump: bad, navLabel: document.querySelector('nav').getAttribute('aria-label') };
    })()`);
    assert.deepEqual(a11y.unlabeled, [], "레이블 없는 입력");
    assert.deepEqual(a11y.iconNoLabel, [], "aria-label 없는 아이콘 버튼");
    assert.deepEqual(a11y.live, [], "live 영역 아닌 상태 메시지");
    assert.equal(a11y.headingJump, false, "제목 단계 건너뜀 없음");
    assert.equal(a11y.navLabel, "주 메뉴");
    assert.deepEqual(cdp.errors, [], "콘솔 오류 없음");
    // 15. 브라우저 저장소를 열 수 없으면(IndexedDB 차단 흉내) 오류 안내가 화면 맨 위에 보이고 현장 지도가 그 위를 덮지 않는다 (리뷰 반영 PR #101)
    const { identifier: idbStub } = await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: `IDBFactory.prototype.open = function () { throw new DOMException('e2e: 저장소 차단', 'SecurityError'); };` });
    await cdp.navigate(`${base}/index.html?reload=6`);
    await cdp.waitFor(visible("fatal"));
    assert.match(await cdp.eval(txt("fatal")), /기록을 저장할 수 없습니다/);
    assert.equal(await cdp.eval("(r => document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)?.closest('#fatal')?.id ?? null)(document.getElementById('fatal').getBoundingClientRect())"), "fatal", "오류 안내가 맨 위에 보인다");
    assert.ok(await cdp.eval("document.getElementById('view-map').hidden && document.querySelector('.bottom-nav').hidden"));
    await cdp.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: idbStub });
  } finally {
    cdp?.close();
    await new Promise((r) => srv.close(r));
  }
});
