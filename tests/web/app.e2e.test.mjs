// 브라우저 e2e (J5-006). 헤드리스 Chromium 을 CDP 로 구동한다. 브라우저가 없으면 건너뛴다.
// 흐름: 설정 → 가상 시드 → 지도(마커·필지 경계·지번) → 관측 저장(사진 포함) → 재접속 후 목록·필지 유지 → 오프라인 재접속(서비스 워커) →
//       IndexedDB 내용을 패키지 폴더로 꺼내 `python -m j5 inspect` 가 ok 를 내는지 확인. 화면 전환(J5-020)은 하단 내비게이션 버튼으로 한다.
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { Cdp, MARKER_POS, ROOT, WEB, findChrome, png1x1, serve, waitForDownloads } from "./cdp.mjs";

const chrome = findChrome();

test("앱 e2e: 설정·시드·관측 저장·재접속·오프라인·j5 inspect", { skip: chrome ? false : "Chromium 없음" }, async () => {
  const tmp = mkdtempSync(join(tmpdir(), "j5-e2e-"));
  const srv = await serve(WEB);
  const base = `http://127.0.0.1:${srv.address().port}`;
  let cdp;
  try {
    // 브라우저 기동 실패도 finally 로 서버를 닫아야 한다. 안 닫으면 테스트 파일이 --test-timeout 까지 매달린다.
    cdp = await Cdp.launch(chrome, tmp);
    // 지도 모듈 로드 실패: map.js 요청을 막고 첫 접속 (서비스 워커가 아직 없을 때). 목록·설정은 그대로 동작하고 지도 절만 안내를 낸다.
    await cdp.send("Network.setBlockedURLs", { urls: ["*/app/map.js"] });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.2.0')");
    await cdp.waitFor("document.getElementById('map-note').textContent.includes('지도 표시 불가')");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li').length"), 1, "지도 모듈 없이도 목록 절이 그려진다");
    const blockedLogs = cdp.errors.splice(0);
    assert.ok(blockedLogs.every((e) => e.includes("ERR_BLOCKED_BY_CLIENT") || e.includes("Failed to load resource") || e.includes("map.js")), blockedLogs.join("; "));
    await cdp.send("Network.setBlockedURLs", { urls: [] });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.2.0')");
    await cdp.waitFor("document.getElementById('map-note').textContent === ''");
    // 설정
    await cdp.eval("document.getElementById('study-id').value = 'e2e-study'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor("document.getElementById('settings-note').textContent === '저장됨'");
    // 시드
    await cdp.eval("document.getElementById('load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 5");
    // 지도 (J5-005): 위치점 4개, 주소만 있는 물건 1개는 목록에서만. 마커 탭 → 대상 요약 → 기록 시작, 확대 → 좌표 변화, 전체 보기 → 복귀 (J5-020: 지도 화면으로 전환)
    await cdp.waitFor("document.querySelectorAll('#map-svg g.pt').length === 4");
    await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('view-map').hidden");
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /위치점 4개 표시 \(가상 4 · 실제 0\) · 위치점 없는 물건 1개/);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt.synthetic').length"), 4, "가상자료 마커 표시");
    assert.match(await cdp.eval("document.querySelector('#map-svg .layer-scale text').textContent"), /^\d+ m$/, "축척 막대");
    const pos0 = await cdp.eval(MARKER_POS);
    const svgBox = await cdp.eval("(b => [b.width, b.height])(document.getElementById('map-svg').getBoundingClientRect())");
    for (const [, x, y] of pos0) assert.ok(x > 0 && x < svgBox[0] && y > 0 && y < svgBox[1], `마커가 지도 안에 있다 ${x},${y}`);
    await cdp.clickRect('#map-svg g.pt[data-asset-id="7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a51"] .hit');
    await cdp.waitFor("!document.getElementById('map-selected').hidden && document.getElementById('map-selected-label').textContent === '가상 물건 2'");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt.sel').length"), 1);
    assert.equal(await cdp.eval("document.querySelector('#map-svg g.pt.sel').dataset.assetId"), "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a51");
    await cdp.eval("document.getElementById('map-selected-start').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.getElementById('target-label').textContent"), "가상 물건 2", "지도 선택 카드에서 관측 시작");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt.sel').length"), 1, "닫아도 지도의 선택은 남는다");
    await cdp.clickRect("#map-zoom-in");
    const pos1 = await cdp.eval(MARKER_POS);
    assert.ok(pos1.some((p, i) => Math.abs(p[1] - pos0[i][1]) > 1 || Math.abs(p[2] - pos0[i][2]) > 1), "확대 후 마커 좌표 변화");
    const orderX = (ps) => [...ps].sort((a, b) => a[1] - b[1]).map((p) => p[0]);
    assert.deepEqual(orderX(pos1), orderX(pos0), "확대해도 동서 순서 유지");
    await cdp.clickRect("#map-fit");
    const pos2 = await cdp.eval(MARKER_POS);
    for (let i = 0; i < pos0.length; i++) { assert.ok(Math.abs(pos2[i][1] - pos0[i][1]) < 0.5 && Math.abs(pos2[i][2] - pos0[i][2]) < 0.5, "전체 보기로 복귀"); }
    // 필지 (J5-013B-1, ADR-13): 가상 필지 6개 → 경계 6개·안내, 필지 탭 → 지번·PNU·도형면적·안의 물건 → 관측, 물건 없는 필지, 라벨은 확대 정도에 따라, 마커 탭은 그대로 물건
    await cdp.eval("document.getElementById('load-synthetic-parcels').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel').length === 6");
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 6개 \(synthetic\) · 가상 연속지적도 \(synthetic\) · 도형 기준일 2026-09-01 · EPSG:5186/);
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /위치점 4개 표시 .* · 필지 6개$/);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel.synthetic').length"), 6, "가상 필지는 점선");
    const VISIBLE_LABELS = "Array.from(document.querySelectorAll('#map-svg text.parcel-label')).filter(t => t.getAttribute('visibility') === 'visible').map(t => t.textContent)";
    const labels0 = await cdp.eval(VISIBLE_LABELS);
    assert.ok(labels0.includes("1"), `전체 보기에서 넓은 필지의 지번은 보인다: ${labels0}`);
    // 필지 1: 물건 1 이 서쪽 끝에 있어 필지 중심 탭은 마커(반지름 18px)가 아니라 필지에 닿는다
    await cdp.clickRect('#map-svg path.parcel[data-pnu="9999900100100010000"]');
    await cdp.waitFor("!document.getElementById('parcel-panel').hidden");
    assert.equal(await cdp.eval("document.getElementById('parcel-title').textContent"), "가상동 1");
    assert.equal(await cdp.eval("document.getElementById('parcel-pnu').textContent"), "9999900100100010000");
    assert.match(await cdp.eval("document.getElementById('parcel-meta').textContent"), /^도형면적 1764\.9 ㎡ \(공부면적 아님\) · 지목 대 · 가상 연속지적도 \(synthetic\) 2026-09-01$/);
    assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#parcel-assets li')).map(li => li.firstChild.textContent)"), ["가상 물건 1"], "필지 안의 물건");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel.sel').length"), 1);
    await cdp.eval("document.querySelector('#parcel-assets li button').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.getElementById('target-label').textContent"), "가상 물건 1", "필지 패널에서 관측 시작");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('sec-observe').hidden");
    // 물건 없는 필지 (구멍 있는 4-2): 안내만
    await cdp.clickRect('#map-svg path.parcel[data-pnu="9999900100100040002"]');
    await cdp.waitFor("document.getElementById('parcel-title').textContent === '가상동 4-2'");
    assert.match(await cdp.eval("document.getElementById('parcel-assets').textContent"), /연결되거나 위치점이 들어 있는 물건이 없다/);
    assert.ok((await cdp.eval(VISIBLE_LABELS)).includes("4-2"), "선택한 필지의 지번은 항상 보인다");
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    await cdp.waitFor("document.getElementById('parcel-panel').hidden && document.querySelectorAll('#map-svg path.parcel.sel').length === 0");
    // 라벨: 확대하면 늘거나 같고, 32배 축소하면 모두 숨는다. 경계 path 는 그대로 6개
    await cdp.clickRect("#map-zoom-in"); await cdp.clickRect("#map-zoom-in");
    const labels1 = await cdp.eval(VISIBLE_LABELS);
    assert.ok(labels1.length >= labels0.length, `확대 후 라벨 ${labels1} ≥ ${labels0}`);
    for (let i = 0; i < 7; i++) await cdp.clickRect("#map-zoom-out");
    assert.deepEqual(await cdp.eval(VISIBLE_LABELS), [], "축소하면 지번 숨김");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel').length"), 6);
    await cdp.clickRect("#map-fit");
    await cdp.clickRect('#map-svg g.pt[data-asset-id="7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a51"] .hit');
    await cdp.waitFor("!document.getElementById('map-selected').hidden && document.getElementById('map-selected-label').textContent === '가상 물건 2'");
    await cdp.eval("document.getElementById('map-selected-start').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.getElementById('target-label').textContent"), "가상 물건 2", "필지 위의 마커 탭은 물건 선택");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    // 잘못된 필지 파일은 거절되고 기존 필지가 남는다. 올바른 파일(기기 파일)은 출처 표시가 바뀐다
    const parcelsFixture = readFileSync(join(ROOT, "tests/fixtures/parcels/synthetic.j5parcels.json"), "utf8");
    const parcelsBad = join(tmp, "parcels-bad.json");
    writeFileSync(parcelsBad, JSON.stringify({ ...JSON.parse(parcelsFixture), count: 1 }));
    await cdp.setFiles("#parcels-file", [parcelsBad]);
    await cdp.waitFor("document.getElementById('parcels-note').textContent.includes('필지 파일 오류')");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel').length"), 6);
    // 파생본(parcels.geojson) 형식: 정본에서 연결한 물건(asset_ids)이 있으면 위치점 없는 물건도 필지 패널에 뜬다 (J5-013B-2)
    const linked = JSON.parse(parcelsFixture);
    const f42 = linked.features.find((f) => f.properties.label === "4-2");
    f42.properties.asset_ids = ["7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a52"];
    f42.properties.geometry_version = "2026-09-01";
    linked.study_id = "other-study"; linked.source_dataset_version = 3;
    const parcelsOther = join(tmp, "parcels-other.geojson");
    writeFileSync(parcelsOther, JSON.stringify(linked));
    await cdp.setFiles("#parcels-file", [parcelsOther]);
    await cdp.waitFor("document.getElementById('parcels-note').textContent.includes('study_id(other-study)가 설정(e2e-study)과 다르다')");
    linked.study_id = "e2e-study";
    const parcelsFile = join(tmp, "parcels.geojson");
    writeFileSync(parcelsFile, JSON.stringify(linked));
    await cdp.setFiles("#parcels-file", [parcelsFile]);
    await cdp.waitFor("document.getElementById('parcels-note').textContent.includes('file:parcels.geojson')");
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /정본 v3 · file:parcels\.geojson · 가져오기 .+ · 정본 연결 포함$/);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel').length"), 6);
    await cdp.clickRect('#map-svg path.parcel[data-pnu="9999900100100040002"]');
    await cdp.waitFor("!document.getElementById('parcel-panel').hidden && document.getElementById('parcel-title').textContent === '가상동 4-2'");
    assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#parcel-assets li')).map(li => [li.firstChild.textContent, li.querySelectorAll('.badge')[1].textContent])"), [["가상 물건 3", "정본 연결"]], "정본 연결(asset_ids)만으로도 위치점 없는 물건이 뜬다");
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    // 관측 (사진 1장, 태그 1개)
    const photo = join(tmp, "front.png");
    writeFileSync(photo, png1x1([0, 128, 255]));
    await cdp.eval("document.querySelectorAll('#asset-list li button')[0].click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    await cdp.eval("document.getElementById('status-change_observed').click(); document.getElementById('note').value = '1층 임대 광고 (e2e)'; 'ok'");
    await cdp.setFiles("#photos", [photo]);
    await cdp.waitFor("document.querySelectorAll('#photo-list .photo-item').length === 1 && document.querySelector('#photo-list .tags')");
    await cdp.eval("document.querySelector('#photo-list .tags input').click(); 'ok'");
    // 빠른 두 번 탭: 한 건만 저장돼야 한다
    await cdp.eval("const b = document.getElementById('save-observation'); b.click(); b.click(); 'ok'");
    await cdp.waitFor("document.getElementById('observe-note').textContent.startsWith('저장됨')");
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    // 저장 뒤 다음 행동 화면 (J5-020): 다음 대상 제안 → 목록으로 닫기
    await cdp.waitFor("!document.getElementById('observe-done').hidden && document.getElementById('observe-form').hidden");
    assert.match(await cdp.eval("document.getElementById('done-next').textContent"), /다음 대상 기록: 가상 물건/);
    await cdp.eval("document.getElementById('done-list').click(); 'ok'");
    await cdp.waitFor("document.getElementById('sec-observe').hidden && !document.getElementById('view-home').hidden");
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), 1, "두 번 탭에 한 건만 저장");
    // 미지원 사진(HEIC 시그니처)은 오류로 표시되고 저장이 막힌다
    const heic = join(tmp, "x.heic");
    writeFileSync(heic, Buffer.concat([Buffer.from([0, 0, 0, 0x18]), Buffer.from("ftypheic"), Buffer.alloc(20)]));
    await cdp.eval("document.querySelectorAll('#asset-list li button')[1].click(); 'ok'");
    await cdp.setFiles("#photos", [heic]);
    await cdp.waitFor("document.querySelector('#photo-list .bad')?.textContent.includes('HEIC')");
    await cdp.eval("document.getElementById('status-no_change').click(); document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('observe-note').textContent.includes('오류가 있는 사진')");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    // 시드 교체(기기 파일, 물건 2개): 이전 물건은 목록에서 빠지고 기록은 남는다
    const seedAll = JSON.parse(readFileSync(join(ROOT, "tests/fixtures/assets.seed.synthetic.json"), "utf8"));
    const seed2 = join(tmp, "seed2.json");
    writeFileSync(seed2, JSON.stringify(seedAll.slice(3)));
    await cdp.setFiles("#seed-file", [seed2]);
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 2");
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    assert.ok((await cdp.eval("document.getElementById('record-list').textContent")).includes("현재 목록에 없는 물건"));
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 2, "시드 교체 후 지도도 2개");
    // 시드가 바뀌면 번들의 정본 연결은 확인되지 않은 것으로 본다: 필지 4-2 의 "가상 물건 3" 연결을 보여 주지 않는다 (J5-013B-2 리뷰 반영)
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /시드가 바뀐 뒤라 표시하지 않음/);
    await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
    await cdp.clickRect('#map-svg path.parcel[data-pnu="9999900100100040002"]');
    await cdp.waitFor("!document.getElementById('parcel-panel').hidden && document.getElementById('parcel-title').textContent === '가상동 4-2'");
    assert.equal(await cdp.eval("document.querySelectorAll('#parcel-assets li button').length"), 0, "오래된 정본 연결로 관측을 시작하지 못한다");
    assert.match(await cdp.eval("document.getElementById('parcel-assets').textContent"), /정본 연결 1건은 시드가 바뀐 뒤 확인되지 않아/);
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    // 주소만 있는 시드: 지도에는 점이 없고 목록으로 선택한다
    const seedAddr = join(tmp, "seed-addr.json");
    writeFileSync(seedAddr, JSON.stringify([seedAll[2]]));
    await cdp.setFiles("#seed-file", [seedAddr]);
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 1");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 0);
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /위치점 있는 물건이 없다\. 목록에서 선택한다/);
    await cdp.setFiles("#seed-file", [seed2]);
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 2");
    // 잘못된 시드 파일은 거절되고 목록이 바뀌지 않는다
    const seedBad = join(tmp, "seed-bad.json");
    writeFileSync(seedBad, JSON.stringify([{ ...seedAll[0], extra: 1 }]));
    await cdp.setFiles("#seed-file", [seedBad]);
    await cdp.waitFor("document.getElementById('seed-note').textContent.includes('물건 목록 파일 오류')");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li button').length"), 2);
    await cdp.eval("document.getElementById('load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 5");
    // 재접속: 기록 유지
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    assert.ok((await cdp.eval("document.getElementById('status-line').textContent")).includes("관측 1"));
    await cdp.waitFor("document.querySelectorAll('#map-svg g.pt').length === 4");
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel').length === 6");
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 6개/, "재접속 후 필지 유지 (IndexedDB)");
    // 지도 실패 시 목록: SVG 요소 생성만 막아(저장소에서 createElementNS 는 map.js 만 쓴다) 지도가 못 뜨는 상황을 만든다.
    // 앱은 안내만 남기고 목록·기록은 그대로여야 하며 콘솔 오류를 내지 않아야 한다.
    const { identifier: stub } = await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      const orig = document.createElementNS.bind(document);
      document.createElementNS = (ns, name) => { if (String(ns).endsWith('/svg')) throw new Error('e2e: SVG 생성 불가'); return orig(ns, name); };` });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('map-note').textContent.includes('지도 표시 불가')");
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /지도 표시 불가: e2e: SVG 생성 불가\. 목록에서 선택한다/);
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 5");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 0);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel').length"), 0);
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 6개/, "지도 실패해도 필지 안내는 남는다");
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), 1, "지도 실패해도 기록 목록 유지");
    await cdp.eval("document.querySelectorAll('#asset-list li button')[2].click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.getElementById('target-label').textContent"), "가상 물건 3", "지도 없이 목록으로 선택");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    assert.deepEqual(cdp.errors, [], "지도 실패 경로에서 콘솔 오류 없음");
    await cdp.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: stub });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.querySelectorAll('#map-svg g.pt').length === 4");
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    // 필지 제거 → 경계 0개·안내, 다시 가상 필지
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel').length === 6");
    await cdp.eval("document.getElementById('clear-parcels').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel').length === 0");
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 없음/);
    assert.doesNotMatch(await cdp.eval("document.getElementById('map-note').textContent"), /필지/);
    await cdp.eval("document.getElementById('load-synthetic-parcels').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel').length === 6");
    // 이전 준비(J5-017D): 내보내지 않은 기록이 있으면 '내보내기 필요', origin·지속 저장 표시
    assert.match(await cdp.eval("document.getElementById('migrate-note').textContent"), /내보내기 필요: 1건/);
    assert.equal(await cdp.eval("document.getElementById('migrate-origin').textContent"), await cdp.eval("location.origin"));
    assert.match(await cdp.eval("document.getElementById('migrate-storage').textContent"), /지속 저장/);
    // 설정을 다른 study 로 바꾸면 그 기록은 '다른 study/모드' 로, 되돌리면 '현재 설정' 으로 즉시 바뀐다 (리뷰 반영)
    await cdp.eval("document.getElementById('study-id').value = 'e2e-other'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor("document.getElementById('migrate-note').textContent.includes('다른 study/모드 1건(e2e-study/synthetic)')");
    await cdp.eval("document.getElementById('study-id').value = 'e2e-study'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor("document.getElementById('migrate-note').textContent.includes('현재 설정 1건')");
    // 내보내기: 묶음 준비 → 파일 저장(다운로드) → j5 inspect ok → 저장 확인 → 내보냄 표시
    const dl = join(tmp, "dl"); mkdirSync(dl);
    await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dl, eventsEnabled: true });
    await cdp.eval("document.getElementById('nav-export').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('view-export').hidden");
    await cdp.eval("document.getElementById('export-prepare').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('export-save').disabled");
    assert.match(await cdp.eval("document.getElementById('export-summary').textContent"), /대상 1건 · 사진 1장/);
    assert.equal(await cdp.eval("document.querySelector('#export-steps .current').dataset.step"), "3", "파일을 만들면 3단계(파일 저장)");
    await cdp.clickSelector("#export-save");
    const zip1 = await waitForDownloads(dl, 1);
    assert.match(zip1, /^e2e-study-\d{8}-\d{6}-1of1-[0-9a-f]{8}\.j5field\.zip$/);
    const insp = spawnSync("python3", ["-m", "j5", "inspect", join(dl, zip1), "--seed", join(ROOT, "tests/fixtures/assets.seed.synthetic.json"), "--study-id", "e2e-study", "--json"], { cwd: ROOT, encoding: "utf8" });
    if (insp.error?.code !== "ENOENT") {
      assert.equal(insp.status, 0, insp.stdout + insp.stderr);
      const rep = JSON.parse(insp.stdout);
      assert.equal(rep.verdict, "ok", insp.stdout);
      assert.equal(rep.kind, "zip");
      assert.equal(rep.counts.events, 1);
      assert.equal(rep.counts.photos_referenced, 1);
    }
    // 저장 확인 전에는 아직 저장됨
    assert.ok((await cdp.eval("document.getElementById('record-list').textContent")).includes("저장됨"));
    await cdp.waitFor("!document.getElementById('export-confirm').disabled");
    await cdp.eval("document.getElementById('export-confirm').click(); 'ok'");
    await cdp.waitFor("document.getElementById('export-note').textContent.includes('모두 확인됨')");
    assert.ok((await cdp.eval("document.getElementById('record-list').textContent")).includes("내보냄"));
    assert.match(await cdp.eval("document.getElementById('export-history').textContent"), /저장 확인/);
    assert.match(await cdp.eval("document.getElementById('migrate-note').textContent"), /이전 준비 완료: 기록 1건/, "모두 내보내면 이전 준비 완료");
    // 다시 내보내기(내보낸 기록 포함): observations.jsonl 바이트가 같다
    await cdp.eval("document.getElementById('include-exported').click(); document.getElementById('export-prepare').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('export-save').disabled");
    await cdp.clickSelector("#export-save");
    const zip2 = await waitForDownloads(dl, 2);
    assert.notEqual(zip1, zip2);
    const cmp = spawnSync("python3", ["-c", `
import sys, zipfile, hashlib, json
a, b = (zipfile.ZipFile(p) for p in sys.argv[1:3])
h = lambda z: hashlib.sha256(z.read("observations.jsonl")).hexdigest()
ma, mb = (json.loads(z.read("manifest.json")) for z in (a, b))
print(json.dumps({"same_obs": h(a) == h(b), "same_pkg": ma["package_id"] == mb["package_id"], "names": sorted(a.namelist()) == sorted(b.namelist())}))
`, join(dl, zip1), join(dl, zip2)], { encoding: "utf8" });
    if (cmp.error?.code !== "ENOENT") {
      assert.equal(cmp.status, 0, cmp.stderr);
      assert.deepEqual(JSON.parse(cmp.stdout), { same_obs: true, same_pkg: false, names: true });
    }
    // 두 번째 시도는 확인하지 않고 둔다 → 이력에 '저장 미확인' 이 남고 기록 상태는 그대로 내보냄
    // 오프라인 재접속: 정적 서버를 실제로 내린 뒤에도 서비스 워커 캐시로 앱이 뜨고 기록이 남는다 (127.0.0.1 은 보안 컨텍스트).
    // CDP 네트워크 에뮬레이션은 서비스 워커의 요청에 적용되지 않으므로 서버를 내린다.
    await cdp.waitFor("navigator.serviceWorker.ready.then(() => true)");
    await cdp.waitFor("caches.keys().then(k => k.length > 0)");
    srv.closeAllConnections();
    await new Promise((r) => srv.close(r));
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    // 캐시 목록에 없는 시드 파일 요청은 실패해야 한다 (서비스 워커가 데이터를 캐시하지 않음). 이 탐침이 남기는 로드 실패 로그는 예상된 것이라 걷어낸다.
    const errorsBefore = cdp.errors.length;
    assert.equal(await cdp.eval("fetch('data/assets.seed.synthetic.json', { cache: 'no-store' }).then(() => 'reachable').catch(() => 'blocked')"), "blocked");
    await new Promise((r) => setTimeout(r, 200));
    const probeLogs = cdp.errors.splice(errorsBefore);
    assert.ok(probeLogs.every((e) => e.includes("ERR_FAILED") || e.includes("Failed to load resource")), probeLogs.join("; "));
    assert.ok((await cdp.eval("document.getElementById('status-line').textContent")).includes("관측 1"));
    // IndexedDB 내용을 패키지 폴더로 꺼내 PC 검사기로 확인
    const dumpDb = () => cdp.eval(`(async () => {
      const db = await new Promise((res, rej) => { const r = indexedDB.open('j5'); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
      const all = (s) => new Promise((res, rej) => { const r = db.transaction(s).objectStore(s).getAll(); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
      const events = await all('events'); const photos = await all('photos');
      const b64 = async (blob) => { const buf = new Uint8Array(await blob.arrayBuffer()); let s = ''; for (const x of buf) s += String.fromCharCode(x); return btoa(s); };
      return { events: events.map(e => ({ line: Array.from(e.line), hash: e.event_hash, status: e.status, study_id: e.study_id, data_mode: e.data_mode, exported_in: e.exported_in })), photos: await Promise.all(photos.map(async p => ({ sha256: p.sha256, ext: p.ext, b64: await b64(p.blob) }))) };
    })()`);
    const dump = await dumpDb();
    assert.equal(dump.events.length, 1);
    assert.equal(dump.events[0].status, "exported", "저장 확인 후 내보냄");
    assert.equal(dump.events[0].exported_in.length, 1, "미확인 두 번째 시도는 exported_in 에 들어가지 않음");
    assert.equal(dump.events[0].study_id, "e2e-study", "기록에 study_id 고정");
    assert.equal(dump.events[0].data_mode, "synthetic", "기록에 data_mode 고정");
    // 덤프를 패키지 폴더로 써서 PC 검사기(j5 inspect)를 돌린다. 이벤트 순서는 기기 입력 시각순
    const inspectDump = (d, name, packageId) => {
      const pkg = join(tmp, name); mkdirSync(join(pkg, "photos"), { recursive: true });
      const lines = d.events.map((e) => ({ buf: Buffer.from(e.line), ev: JSON.parse(Buffer.from(e.line).toString("utf8")) }));
      lines.sort((a, b) => (a.ev.device_created_at < b.ev.device_created_at ? -1 : a.ev.device_created_at > b.ev.device_created_at ? 1 : 0));
      const obs = Buffer.concat(lines.map((l) => l.buf));
      writeFileSync(join(pkg, "observations.jsonl"), obs);
      const files = [{ path: "observations.jsonl", bytes: obs.length, sha256: createHash("sha256").update(obs).digest("hex") }];
      for (const p of d.photos) { const buf = Buffer.from(p.b64, "base64"); writeFileSync(join(pkg, "photos", `${p.sha256}.${p.ext}`), buf); files.push({ path: `photos/${p.sha256}.${p.ext}`, bytes: buf.length, sha256: createHash("sha256").update(buf).digest("hex") }); }
      writeFileSync(join(pkg, "manifest.json"), JSON.stringify({ format: "j5field", schema_version: "1.0.0", study_id: "e2e-study", package_id: packageId, created_at: "2026-09-22T09:00:00+09:00", data_mode: "synthetic", files }, null, 2) + "\n");
      const py = spawnSync("python3", ["-m", "j5", "inspect", pkg, "--seed", join(ROOT, "tests/fixtures/assets.seed.synthetic.json"), "--study-id", "e2e-study", "--json"], { cwd: ROOT, encoding: "utf8" });
      if (py.error?.code === "ENOENT") { console.log("python3 없음: inspect 단계 건너뜀"); return null; }
      assert.equal(py.status, 0, py.stdout + py.stderr);
      return JSON.parse(py.stdout);
    };
    assert.equal(createHash("sha256").update(Buffer.from(dump.events[0].line)).digest("hex"), dump.events[0].hash, "저장된 해시 = 행 바이트(LF 포함) 해시");
    const rep = inspectDump(dump, "pkg", "5e5e0000-0000-4000-8000-00000000e2e0");
    if (rep) {
      assert.equal(rep.verdict, "ok");
      assert.equal(rep.counts.events, 1);
      assert.equal(rep.counts.photos_referenced, 1);
    }
    assert.deepEqual(cdp.errors, [], "브라우저 콘솔 오류 없음");
    // 연차 비교 입력 (J5-015B): 같은 물건의 두 번째 관측에서 촬영 지점·방향·이전 사진(이 기기에 저장된 첫 사진)을 고른다.
    // 오프라인 상태 그대로다(저장은 IndexedDB). 이전 사진 목록은 기기의 기록에서 나온다
    const firstSha = createHash("sha256").update(readFileSync(photo)).digest("hex");
    const photo2 = join(tmp, "front-2026.png");
    writeFileSync(photo2, png1x1([255, 128, 0]));
    await cdp.eval("document.querySelectorAll('#asset-list li button')[0].click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    await cdp.setFiles("#photos", [photo2]);
    await cdp.waitFor("document.querySelectorAll('#photo-list .photo-item .series select.previous option').length === 2");
    assert.match(await cdp.eval("document.querySelectorAll('#photo-list select.previous option')[1].textContent"), new RegExp(`^\\d{4}-\\d{2}-\\d{2} 전면 · ${firstSha.slice(0, 8)}$`), "이전 사진 후보 = 첫 관측의 사진");
    await cdp.eval(`(() => {
      const vp = document.querySelector('#photo-list input.viewpoint'); vp.value = '전면-남측'; vp.dispatchEvent(new Event('input'));
      const hd = document.querySelector('#photo-list input.heading'); hd.value = '180'; hd.dispatchEvent(new Event('input'));
      const pv = document.querySelector('#photo-list select.previous'); pv.value = ${JSON.stringify(firstSha)}; pv.dispatchEvent(new Event('change'));
      document.querySelector('#photo-list .tags input').click();
      document.getElementById('status-no_change').click(); return 'ok'; })()`);
    await cdp.eval("document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('observe-note').textContent.startsWith('저장됨')");
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 2");
    const dump2 = await dumpDb();
    assert.equal(dump2.events.length, 2);
    const second = dump2.events.map((e) => JSON.parse(Buffer.from(e.line).toString("utf8"))).find((ev) => ev.attachment_refs[0]?.sha256 !== firstSha);
    assert.ok(second, "두 번째 이벤트");
    assert.deepEqual([second.attachment_refs[0].viewpoint_id, second.attachment_refs[0].heading_deg, second.attachment_refs[0].previous_photo_sha256], ["전면-남측", 180, firstSha]);
    const first = dump2.events.map((e) => JSON.parse(Buffer.from(e.line).toString("utf8"))).find((ev) => ev.attachment_refs[0]?.sha256 === firstSha);
    assert.deepEqual(Object.keys(first.attachment_refs[0]).sort(), ["bytes", "mime", "path", "sha256", "tags"], "첫 이벤트에는 선택 키가 없다 (바이트 불변)");
    const rep2 = inspectDump(dump2, "pkg2", "5e5e0000-0000-4000-8000-00000000e2e1");
    if (rep2) {
      assert.equal(rep2.verdict, "ok", JSON.stringify(rep2));
      assert.equal(rep2.counts.events, 2);
      assert.equal(rep2.counts.photos_referenced, 2);
    }
    assert.deepEqual(cdp.errors, [], "브라우저 콘솔 오류 없음");
  } finally {
    cdp?.close();
    try { srv.closeAllConnections(); srv.close(); } catch {}
  }
});
