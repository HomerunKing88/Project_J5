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
    await cdp.waitFor(`${txt("status-line")}.includes('앱 0.2.24')`);

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
    // 14. 접근성 속성: 입력마다 label, 아이콘 버튼 aria-label, 상태 메시지 live, 제목 순서
    const a11y = await cdp.eval(`(() => {
      const unlabeled = Array.from(document.querySelectorAll('input:not([type=hidden]), select, textarea')).filter(i => !(i.labels && i.labels.length) && !i.getAttribute('aria-label') && !i.getAttribute('aria-labelledby')).map(i => i.id || i.name);
      const iconNoLabel = Array.from(document.querySelectorAll('button.icon')).filter(b => !b.getAttribute('aria-label')).map(b => b.id);
      const live = ['observe-note', 'export-note', 'settings-note', 'seed-note', 'parcels-note', 'basemap-note', 'view-note', 'tiles-note', 'tiles-prefetch-note', 'map-note'].filter(id => !document.getElementById(id).getAttribute('role') && !document.getElementById(id).getAttribute('aria-live'));
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
