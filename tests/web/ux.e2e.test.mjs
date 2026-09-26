// 모바일 현장 앱 UX e2e (J5-020). 헤드리스 Chromium(CDP)으로 사용자 행동 순서를 따라간다. 브라우저가 없으면 건너뛴다.
// 흐름: 처음 실행 빈 상태 → 연습용 자료 → 대상 카드 → 관측 화면(포커스·필수값 누락·중복 사진·저장 실패·저장 성공·다음 행동) →
//       미내보냄 수 → 내보내기 4단계(저장 확인 전/후) → 새로고침 유지 → 해시 이동 → 오프라인 배너 → 지도 실패 시 목록 → 모바일 내비게이션·접근성 속성.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
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
    await cdp.waitFor(`${txt("status-line")}.includes('앱 0.2.0')`);

    // 1. 처음 실행: 오늘 화면, 빈 상태 안내와 두 가지 다음 행동, 기술 용어 없음
    assert.ok(await cdp.eval(visible("view-home")) && await cdp.eval("document.getElementById('view-map').hidden"), "첫 화면은 오늘");
    assert.equal(await cdp.eval("document.getElementById('nav-home').getAttribute('aria-current')"), "page");
    assert.ok(await cdp.eval(visible("home-empty")) && await cdp.eval("document.getElementById('home-cta').hidden"), "빈 상태 안내");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li.empty').length"), 1);
    assert.equal(await cdp.eval(txt("mode-badge")), "연습용 자료", "자료 종류는 항상 보인다");
    const homeText = await cdp.eval(txt("view-home"));
    for (const w of ["study_id", "asset_id", "origin", "synthetic", "j5 db", ".json"]) assert.ok(!homeText.includes(w), `오늘 화면에 내부 용어 없음: ${w}`);
    assert.ok(!(await cdp.eval("document.body.scrollWidth > window.innerWidth")), "가로 스크롤 없음 (빈 상태)");

    // 설정 저장 (작업 공간 이름) → 연습용 자료
    await cdp.eval("document.getElementById('study-id').value = 'ux-study'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor(`${txt("settings-note")} === '저장됨'`);
    await cdp.eval("document.getElementById('home-load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 5");
    // 2. 대상 카드: 이름·위치 요약·기록 없음 표시, 좌표 숫자 없음, 통계·주요 버튼
    assert.equal(await cdp.eval("document.querySelector('#asset-list li .title').textContent"), "가상 물건 1");
    assert.match(await cdp.eval("document.querySelector('#asset-list li .meta').textContent"), /이 기기에 기록 없음/);
    assert.doesNotMatch(await cdp.eval(txt("asset-list")), /126\.\d{3}/, "좌표 숫자를 기본 화면에 노출하지 않는다");
    assert.equal(await cdp.eval(txt("stat-targets")), "5");
    assert.ok(await cdp.eval("document.getElementById('home-empty').hidden") && await cdp.eval(visible("home-cta")));
    assert.equal(await cdp.eval(txt("start-observing")), "관측 시작");
    assert.ok(!(await cdp.eval("document.body.scrollWidth > window.innerWidth")), "가로 스크롤 없음 (목록)");
    const smallTargets = await cdp.eval("Array.from(document.querySelectorAll('#view-home button, .bottom-nav button')).filter(b => !b.hidden && b.getBoundingClientRect().height > 0 && b.getBoundingClientRect().height < 44).map(b => b.id || b.textContent)");
    assert.deepEqual(smallTargets, [], "터치 대상 44px 이상");

    // 3. 관측 시작: 첫 대상, 작업 화면에 포커스, dialog 속성
    await cdp.eval("document.getElementById('start-observing').click(); 'ok'");
    await cdp.waitFor(visible("sec-observe"));
    assert.equal(await cdp.eval(txt("target-label")), "가상 물건 1");
    assert.equal(await cdp.eval("document.activeElement.id"), "observe-title", "열리면 제목으로 포커스");
    assert.equal(await cdp.eval("document.getElementById('sec-observe').getAttribute('aria-modal')"), "true");
    // 4. 필수값 누락: 상태 없이 저장 → 오류, 기록 없음
    await cdp.eval("document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor(`${txt("observe-note")}.includes('관측 상태를 선택')`);
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
    await cdp.waitFor(`${txt("observe-note")}.includes('오류가 있는 사진')`);
    await cdp.eval("document.querySelectorAll('#photo-list .photo-item button')[1].click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#photo-list .photo-item').length === 1");
    // 6. 저장 실패(저장소 오류): '저장됨' 으로 표시하지 않고 기록도 남지 않는다
    await cdp.eval("window.__failSave = true; document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor(`${txt("observe-note")}.startsWith('저장 실패')`);
    assert.ok(await cdp.eval(visible("observe-form")) && await cdp.eval("document.getElementById('observe-done').hidden"), "실패하면 입력 화면 그대로");
    assert.equal(await cdp.eval(txt("stat-records")), "0");
    // 7. 저장 성공 → 완료 화면, 포커스, 통계·배지, 다음 대상 제안
    await cdp.eval("window.__failSave = false; document.getElementById('note').value = 'UX e2e'; document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor(`${txt("observe-note")}.startsWith('저장됨')`);
    await cdp.waitFor(visible("observe-done"));
    assert.equal(await cdp.eval("document.activeElement.id"), "done-title", "저장 뒤 완료 제목으로 포커스");
    assert.match(await cdp.eval(txt("done-text")), /아직 내보내지 않았습니다.*PC 반영 여부는 이 화면에서 알 수 없습니다/);
    assert.equal(await cdp.eval(txt("stat-records")), "1");
    assert.equal(await cdp.eval(txt("stat-unexported")), "1");
    assert.equal(await cdp.eval(txt("nav-export-badge")), "1");
    assert.equal(await cdp.eval(txt("done-next")), "다음 대상 기록: 가상 물건 2");
    // 8. 다음 대상 기록 → 새 입력 화면, Esc 로 닫기 → 목록의 카드에 마지막 기록 표시
    await cdp.eval("document.getElementById('done-next').click(); 'ok'");
    await cdp.waitFor(`${visible("observe-form")} && ${txt("target-label")} === '가상 물건 2'`);
    await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await cdp.waitFor("document.getElementById('sec-observe').hidden");
    assert.match(await cdp.eval("document.querySelector('#asset-list li .meta').textContent"), /변화 확인.*기록 1건 · 사진 1장.*안 내보냄 1건/);
    assert.match(await cdp.eval(txt("home-last")), /최근 저장: 오늘 .* 가상 물건 1 · 이 기기에 저장됨 \(아직 안 내보냄\)/);
    assert.equal(await cdp.eval(txt("start-observing")), "관측 계속하기");

    // 9. 내보내기 단계: 1 확인 → 2 만들기 → 3 저장 → 4 확인. 저장 확인 전에는 '저장됨' 그대로
    await go(cdp, "export");
    await cdp.waitFor(`${visible("view-export")} && ${txt("export-summary")}.includes('대상 1건 · 사진 1장')`);
    assert.equal(await cdp.eval("document.activeElement.id"), "export-title", "화면 전환 시 제목으로 포커스");
    assert.equal(await cdp.eval("document.querySelector('#export-steps .current').dataset.step"), "1");
    await cdp.eval("document.getElementById('export-prepare').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('export-save').disabled");
    assert.equal(await cdp.eval("document.querySelector('#export-steps .current').dataset.step"), "3");
    const dl = join(tmp, "dl"); mkdirSync(dl);
    await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dl, eventsEnabled: true });
    await cdp.clickSelector("#export-save");
    await waitForDownloads(dl, 1);
    await cdp.waitFor("!document.getElementById('export-confirm').disabled");
    assert.equal(await cdp.eval("document.querySelector('#export-steps .current').dataset.step"), "4");
    assert.match(await cdp.eval(txt("export-note")), /확인 전에는 아직 내보냄이 아니다/);
    assert.equal(await cdp.eval(txt("stat-unexported")), "1", "파일 저장만으로는 내보냄이 아니다");
    await cdp.eval("document.getElementById('export-confirm').click(); 'ok'");
    await cdp.waitFor(`${txt("export-note")}.includes('모두 확인됨')`);
    assert.equal(await cdp.eval(txt("stat-unexported")), "0");
    assert.ok(await cdp.eval("document.getElementById('nav-export-badge').hidden"));
    assert.equal(await cdp.eval("document.querySelector('#export-steps .current').dataset.step"), "1", "끝나면 다시 1단계");
    assert.match(await cdp.eval(txt("home-last")), /파일로 내보냄 \(PC 반영 여부는 PC 에서 확인\)/);
    await go(cdp, "records");
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li .badge.state-exported').length"), 1);

    // 10. 새로고침 뒤 유지 + 해시로 화면 이동 (같은 문서의 해시 이동이 아니라 실제 재방문이 되도록 쿼리를 바꾼다)
    await cdp.navigate(`${base}/index.html?reload=1#records`);
    await cdp.waitFor(`${txt("stat-records")} === '1'`);
    assert.ok(await cdp.eval(visible("view-records")) && await cdp.eval("document.getElementById('view-home').hidden"), "해시로 기록 화면");
    assert.equal(await cdp.eval(txt("stat-targets")), "5");
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), 1);
    assert.equal(await cdp.eval(txt("mode-badge")), "연습용 자료");

    // 11. 오프라인 배너
    await cdp.send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await cdp.waitFor(visible("offline-banner"));
    await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await cdp.waitFor("document.getElementById('offline-banner').hidden");

    // 12. 지도 실패(SVG 를 만들 수 없는 환경을 흉내): 지도 화면은 안내, 목록 흐름은 그대로. 서비스 워커가 map.js 를 캐시하므로 URL 차단 대신 스텁을 쓴다
    const { identifier: svgStub } = await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      const orig = document.createElementNS.bind(document);
      document.createElementNS = (ns, name) => { if (String(ns).endsWith('/svg')) throw new Error('e2e: SVG 생성 불가'); return orig(ns, name); };` });
    await cdp.navigate(`${base}/index.html?reload=2#map`);
    await cdp.waitFor(`${txt("map-note")}.includes('지도 표시 불가')`);
    assert.match(await cdp.eval(txt("map-empty")), /지도를 그릴 수 없습니다/);
    await cdp.eval("document.getElementById('map-go-list').click(); 'ok'");
    await cdp.waitFor(visible("view-home"));
    await cdp.eval("document.querySelectorAll('#asset-list li button')[2].click(); 'ok'");
    await cdp.waitFor(`${visible("sec-observe")} && ${txt("target-label")} === '가상 물건 3'`);
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('sec-observe').hidden");
    assert.deepEqual(cdp.errors, [], "지도 실패 경로에서 콘솔 오류 없음");
    await cdp.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: svgStub });

    // 13. 모바일 내비게이션: 하단 고정, 콘텐츠와 겹치지 않음, 각 화면에서 가로 스크롤 없음
    await cdp.navigate(`${base}/index.html?reload=3`);
    await cdp.waitFor(`${txt("stat-targets")} === '5'`);
    const nav = await cdp.eval("(r => ({ bottom: r.bottom, height: r.height, top: r.top }))(document.querySelector('.bottom-nav').getBoundingClientRect())");
    assert.ok(Math.abs(nav.bottom - 844) < 1 && nav.height >= 44, `하단 내비게이션 고정 ${JSON.stringify(nav)}`);
    for (const v of ["map", "records", "export", "settings", "home"]) {
      await go(cdp, v);
      await cdp.waitFor(visible(`view-${v}`));
      assert.equal(await cdp.eval(`document.getElementById('nav-${v}').getAttribute('aria-current')`), "page");
      assert.equal(await cdp.eval("document.activeElement.tagName"), "H2", `${v}: 제목으로 포커스`);
      assert.ok(!(await cdp.eval("document.documentElement.scrollWidth > window.innerWidth + 1")), `${v}: 가로 스크롤 없음`);
      const lastBottom = await cdp.eval(`(() => { window.scrollTo(0, document.body.scrollHeight); const s = document.getElementById('view-${v}'); return s.getBoundingClientRect().bottom; })()`);
      assert.ok(lastBottom <= nav.top + 1, `${v}: 끝까지 내려도 내비게이션에 가려지지 않음 (${lastBottom} vs ${nav.top})`);
    }
    // 14. 접근성 속성: 입력마다 label, 아이콘 버튼 aria-label, 상태 메시지 live, 제목 순서
    const a11y = await cdp.eval(`(() => {
      const unlabeled = Array.from(document.querySelectorAll('input:not([type=hidden]), select, textarea')).filter(i => !(i.labels && i.labels.length) && !i.getAttribute('aria-label') && !i.getAttribute('aria-labelledby')).map(i => i.id || i.name);
      const iconNoLabel = Array.from(document.querySelectorAll('button.icon')).filter(b => !b.getAttribute('aria-label')).map(b => b.id);
      const live = ['observe-note', 'export-note', 'settings-note', 'seed-note', 'parcels-note', 'map-note'].filter(id => !document.getElementById(id).getAttribute('role') && !document.getElementById(id).getAttribute('aria-live'));
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
  } finally {
    cdp?.close();
    await new Promise((r) => srv.close(r));
  }
});
