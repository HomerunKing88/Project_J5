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
  // 배경 타일 (J5-024): 같은 출처의 가짜 타일 폴더 ./tiles/{z}/{x}/{y}.png (1×1 PNG). 요청 수로 서비스 워커 저장을 확인한다.
  const tileReqs = [];
  // 템플릿 ./tiles/{key}/{z}/{x}/{y}.png: 서비스 워커가 자리표를 IndexedDB 의 키(e2e-tile-key)로 바꿔 요청해야 서버가 응답한다
  const srv = await serve(WEB, (path) => {
    const m = /^\/tiles\/([^/]+)\/(\d+)\/(\d+)\/(\d+)\.png$/.exec(path);
    if (!m) return null;
    if (m[1] !== "e2e-tile-key") return { status: 401, type: "text/plain", body: "bad key" };
    tileReqs.push(m.slice(2).join("/"));
    return { type: "image/png", body: png1x1([200, 220, 240]) };
  });
  const base = `http://127.0.0.1:${srv.address().port}`;
  let cdp;
  try {
    // 브라우저 기동 실패도 finally 로 서버를 닫아야 한다. 안 닫으면 테스트 파일이 --test-timeout 까지 매달린다.
    cdp = await Cdp.launch(chrome, tmp);
    // 지도 모듈 로드 실패: map.js 요청을 막고 첫 접속 (서비스 워커가 아직 없을 때). 목록·설정은 그대로 동작하고 지도 절만 안내를 낸다.
    await cdp.send("Network.setBlockedURLs", { urls: ["*/app/map.js"] });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.2.14')");
    await cdp.waitFor("document.getElementById('map-note').textContent.includes('지도 표시 불가')");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li').length"), 1, "지도 모듈 없이도 목록 절이 그려진다");
    const blockedLogs = cdp.errors.splice(0);
    assert.ok(blockedLogs.every((e) => e.includes("ERR_BLOCKED_BY_CLIENT") || e.includes("Failed to load resource") || e.includes("map.js")), blockedLogs.join("; "));
    await cdp.send("Network.setBlockedURLs", { urls: [] });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.2.14')");
    await cdp.waitFor("document.getElementById('map-note').textContent === ''");
    // 설정
    await cdp.eval("document.getElementById('study-id').value = 'e2e-study'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor("document.getElementById('settings-note').textContent === '저장됨'");
    // 시드
    await cdp.eval("document.getElementById('load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 5");
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
    // J5-021: 선택 카드의 외부 지도 링크는 좌표만 담고 새 창·noopener·no-referrer 로 연다. 누르기 전에는 요청이 없다.
    const ext = await cdp.eval("Array.from(document.querySelectorAll('#map-selected-ext a')).map(a => [a.textContent, a.href, a.target, a.rel, a.referrerPolicy])");
    assert.equal(ext.length, 4, "외부 지도 링크 4개");
    for (const [name, href, target, rel, rp] of ext) {
      assert.ok(href.startsWith("https://") && href.includes("37.57") && href.includes("126.99"), `${name}: ${href}`);
      assert.ok(!decodeURIComponent(href).includes("가상 물건"), "물건 이름을 보내지 않는다");
      assert.deepEqual([target, rel, rp], ["_blank", "noopener noreferrer external", "no-referrer"], name);
    }
    assert.equal(await cdp.eval("document.getElementById('map-selected-ext').hidden"), false);
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
    assert.equal(await cdp.eval("document.querySelectorAll('#parcel-ext a').length"), 4, "필지 패널에도 외부 지도 링크 (bbox 가운데)");
    assert.ok((await cdp.eval("document.querySelector('#parcel-ext a').href")).includes("maps.apple.com/?ll="));
    assert.match(await cdp.eval("document.getElementById('parcel-meta').textContent"), /^도형면적 1764\.9 ㎡ \(공부면적 아님\) · 지목 대 · 가상 연속지적도 \(synthetic\) 2026-09-01$/);
    assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#parcel-assets li')).map(li => li.firstChild.textContent)"), ["가상 물건 1"], "필지 안의 물건");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel.sel').length"), 1);
    await cdp.eval("document.querySelector('#parcel-assets li button').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.getElementById('target-label').textContent"), "가상 물건 1", "필지 패널에서 관측 시작");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    // 배경 도형 (J5-022, ADR-16): 연습용 배경 → 층별 path·도로명 라벨, 탭 대상 아님(필지 탭은 그대로), 안내 문구, 지우기
    await cdp.eval("document.getElementById('load-synthetic-basemap').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg .layer-base path').length === 11");
    assert.deepEqual(await cdp.eval("['bm-building','bm-road-area','bm-road'].map(c => document.querySelectorAll('#map-svg path.' + c).length)"), [6, 2, 3]);
    assert.match(await cdp.eval("document.getElementById('basemap-note').textContent"), /^배경 도형 11개 \(건물 6 · 실폭도로 2 · 도로 중심선 3\) · 연습용 · 도형 기준일 2026-09-01 · 이용허락 가상자료 \(이용허락 해당 없음\) · 가져오기 .+$/);
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /· 필지 6개 · 배경 11개$/);
    assert.equal(await cdp.eval("document.querySelector('#map-svg .layer-base').compareDocumentPosition(document.querySelector('#map-svg .layer-parcels')) & Node.DOCUMENT_POSITION_FOLLOWING"), 4, "배경 층은 필지 층보다 앞(아래)에 있다");
    await cdp.clickRect("#map-zoom-in");
    await cdp.clickRect("#map-zoom-in");
    const roadLabels = await cdp.eval("Array.from(document.querySelectorAll('#map-svg text.bm-road-label')).filter(t => t.getAttribute('visibility') === 'visible').map(t => t.textContent).sort()");
    assert.ok(roadLabels.includes("가상로"), `확대하면 긴 도로의 이름이 보인다: ${roadLabels}`);
    assert.equal(await cdp.eval("getComputedStyle(document.querySelector('#map-svg path.bm-building')).pointerEvents"), "none", "배경은 탭 대상이 아니다");
    await cdp.clickRect("#map-fit");
    await cdp.clickRect('#map-svg path.parcel[data-pnu="9999900100100010000"]');
    await cdp.waitFor("!document.getElementById('parcel-panel').hidden && document.getElementById('parcel-title').textContent === '가상동 1'");
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    await cdp.waitFor("document.getElementById('parcel-panel').hidden");
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
    // 폰 범위 경고(J5-039)는 글 그대로, 다른 경고는 건수로
    linked.warnings = ["폰 범위: 정본 필지 12개 중 법정동 9999900100 과 물건이 연결된 필지 6개만 실었다", "원본 좌표계가 섞여 있다: 가상"];
    const parcelsFile = join(tmp, "parcels.geojson");
    writeFileSync(parcelsFile, JSON.stringify(linked));
    await cdp.setFiles("#parcels-file", [parcelsFile]);
    await cdp.waitFor("document.getElementById('parcels-note').textContent.includes('file:parcels.geojson')");
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /정본 v3 · file:parcels\.geojson · 가져오기 .+ · 폰 범위: 정본 필지 12개 중 법정동 9999900100 과 물건이 연결된 필지 6개만 실었다 · 경고 1건 \(변환 로그 참조\) · 정본 연결 포함$/);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel').length"), 6);
    await cdp.clickRect('#map-svg path.parcel[data-pnu="9999900100100040002"]');
    await cdp.waitFor("!document.getElementById('parcel-panel').hidden && document.getElementById('parcel-title').textContent === '가상동 4-2'");
    assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#parcel-assets li')).map(li => [li.firstChild.textContent, li.querySelectorAll('.badge')[1].textContent])"), [["가상 물건 3", "정본 연결"]], "정본 연결(asset_ids)만으로도 위치점 없는 물건이 뜬다");
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    // 필지 속성·용도지역 색 (J5-025): VWorld 가상 묶음에서 만든 번들을 넣으면 속성 수가 안내에 뜨고 "용도지역 색" 버튼이 켜진다. 색은 이 기기에 저장돼 재접속 뒤 유지.
    assert.equal(await cdp.eval("document.getElementById('map-zones').disabled"), true, "속성 없는 번들에서는 용도지역 색을 켤 수 없다");
    const vwFile = join(tmp, "parcels-vworld.j5parcels.json");
    writeFileSync(vwFile, readFileSync(join(ROOT, "tests/fixtures/parcels/synthetic_vworld.j5parcels.json")));
    await cdp.setFiles("#parcels-file", [vwFile]);
    await cdp.waitFor("document.getElementById('parcels-note').textContent.includes('file:parcels-vworld.j5parcels.json')");
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 속성 6개 \(토지특성공간정보, 토지이용계획공간정보, 토지소유공간정보\)/);
    await cdp.waitFor("!document.getElementById('map-zones').disabled");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel[class*=\"zone-\"]').length"), 0, "켜기 전에는 색 없음");
    await cdp.clickRect("#map-zones");
    await cdp.waitFor("document.getElementById('map-zones').getAttribute('aria-pressed') === 'true'");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel[class*=\"zone-\"]').length"), 6);
    assert.equal(await cdp.eval("document.querySelector('#map-svg path.parcel[data-pnu=\"9999900100100010000\"]').getAttribute('class')"), "parcel synthetic zone-com");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel.zone-res2').length"), 2);
    assert.equal(await cdp.eval("document.getElementById('zone-legend').hidden"), false);
    assert.match(await cdp.eval("document.getElementById('zone-legend').textContent"), /일반주거 2/);
    // 필지 찾기 (J5-027): 지번·PNU 검색은 불러온 필지 안에서만. 하나면 바로 열고 화면을 맞춘다(확대 유지), 여럿이면 목록, 없으면 안내
    assert.equal(await cdp.eval("document.getElementById('parcel-search-form').hidden"), false, "필지가 있으면 찾기 입력이 보인다");
    const search = async (q) => { await cdp.eval(`document.getElementById('parcel-search').value = ${JSON.stringify(q)}; document.getElementById('parcel-search-go').click(); 'ok'`); };
    const scaleOf = () => cdp.eval("(() => { const t = document.querySelector('#map-svg .layer-parcels').getAttribute('transform') || ''; const m = /scale\\(([^)]+)\\)/.exec(t); return m ? Number(m[1]) : 0; })()");
    const s0 = await scaleOf();
    await search("1-1");
    await cdp.waitFor("!document.getElementById('parcel-panel').hidden && document.getElementById('parcel-title').textContent === '가상동 1-1'");
    assert.match(await cdp.eval("document.getElementById('parcel-search-note').textContent"), /^가상동 1-1 필지로 이동$/);
    assert.ok((await scaleOf()) > s0, "필지 하나로 맞추면 확대된다");
    assert.equal(await cdp.eval("document.activeElement.id"), "parcel-history", "찾은 필지의 패널로 포커스");
    await search("9999900100100040002");
    await cdp.waitFor("document.getElementById('parcel-title').textContent === '가상동 4-2'");
    await search("산1-2");
    await cdp.waitFor("document.getElementById('parcel-title').textContent === '가상동 산1-2'");
    // 필지에서 물건 만들기 (J5-028): 물건이 없는 필지 → 버튼 → 이 기기 물건 생성·관측 화면. 취소해도 물건은 남고, 시드를 다시 넣어도 남으며, 기록이 없으면 지울 수 있다
    assert.equal(await cdp.eval("document.getElementById('parcel-new-asset').hidden"), false, "물건이 없는 필지에는 만들기 버튼");
    await cdp.eval("document.getElementById('parcel-new-asset').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden && document.getElementById('target-label').textContent === '가상동 산1-2'");
    assert.match(await cdp.eval("document.getElementById('parcel-search-note').textContent"), /가상동 산1-2 물건을 이 기기에 만들었습니다 \(PC 반영 전\)/);
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 6");
    const deviceLi = "Array.from(document.querySelectorAll('#asset-list li')).find(li => li.querySelector('.title').textContent === '가상동 산1-2')";
    assert.match(await cdp.eval(`${deviceLi}.textContent`), /이 기기에서 만듦 · PC 반영 전/);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 5, "위치점이 필지 안에 생긴다");
    await search("산1-2");
    await cdp.waitFor("document.getElementById('parcel-title').textContent === '가상동 산1-2'");
    assert.equal(await cdp.eval("document.getElementById('parcel-new-asset').hidden"), true, "물건이 생기면 버튼은 숨는다");
    assert.match(await cdp.eval("document.getElementById('parcel-assets').textContent"), /가상동 산1-2.*위치점 포함/);
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    // 안내 문구를 비운 뒤 다시 넣는다 (앞선 적재의 같은 문구를 보고 지나치지 않게). 문구는 목록을 다 그린 뒤 쓰인다
    await cdp.eval("document.getElementById('seed-note').textContent = ''; document.getElementById('load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.getElementById('seed-note').textContent.includes('물건 5개를 불러왔습니다')");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li > button').length"), 6, "시드를 다시 넣어도 기기 물건은 남는다");
    await cdp.eval(`${deviceLi}.querySelector('.meta button:last-child').click(); 'ok'`);
    await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 5");
    await cdp.waitFor("document.getElementById('seed-note').textContent.includes('을 지웠습니다')");
    await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
    await search("999-9");
    await cdp.waitFor("document.getElementById('parcel-search-note').textContent.includes('맞는 필지가 불러온 6개 안에 없습니다')");
    await search("abc");
    await cdp.waitFor("document.getElementById('parcel-search-note').textContent.startsWith('지번(예:')");
    // 현재 위치: 권한 허용 + 위치 흉내(필지 4-2 안) → 점·정확도 원 표시, 그 필지 열림. 범위 밖 → 안내. 권한 거부 → 안내. 위치는 IndexedDB 에 남지 않는다
    await cdp.send("Browser.grantPermissions", { origin: base, permissions: ["geolocation"] });
    await cdp.send("Emulation.setGeolocationOverride", { latitude: 37.5705, longitude: 126.9996, accuracy: 12 });
    await cdp.clickRect("#map-locate");
    await cdp.waitFor("document.getElementById('parcel-search-note').textContent.includes('현재 위치는 가상동 4-2 필지 안')");
    assert.match(await cdp.eval("document.getElementById('parcel-search-note').textContent"), /정확도 ±12 m\)\. 위치는 저장하지 않습니다\.$/);
    assert.equal(await cdp.eval("document.querySelector('#map-svg .layer-locate').getAttribute('visibility')"), "visible");
    assert.ok(Number(await cdp.eval("document.querySelector('#map-svg .locate-ring').getAttribute('r')")) > 0, "정확도 원");
    assert.equal(await cdp.eval("document.getElementById('parcel-title').textContent"), "가상동 4-2");
    await cdp.send("Emulation.setGeolocationOverride", { latitude: 37.6, longitude: 127.1, accuracy: 30 });
    await cdp.clickRect("#map-locate");
    await cdp.waitFor("document.getElementById('parcel-search-note').textContent.includes('불러온 필지 범위 밖')");
    await cdp.send("Browser.setPermission", { origin: base, permission: { name: "geolocation" }, setting: "denied" });
    await cdp.clickRect("#map-locate");
    await cdp.waitFor("document.getElementById('parcel-search-note').textContent.includes('위치 권한이 거부')");
    assert.equal(await cdp.eval("document.getElementById('map-locate').disabled"), false);
    await cdp.send("Browser.resetPermissions", {});
    await cdp.send("Emulation.clearGeolocationOverride", {});
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    await cdp.clickRect("#map-fit");
    await cdp.clickRect('#map-svg path.parcel[data-pnu="9999900100100010000"]');
    await cdp.waitFor("!document.getElementById('parcel-panel').hidden && document.getElementById('parcel-title').textContent === '가상동 1'");
    assert.equal(await cdp.eval("document.querySelector('#map-svg path.parcel[data-pnu=\"9999900100100010000\"]').getAttribute('class')"), "parcel synthetic zone-com sel");
    assert.equal(await cdp.eval("document.getElementById('parcel-attrs').hidden"), false);
    assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#parcel-attrs li')).slice(0, 4).map(li => [li.querySelector('.k').textContent, li.querySelector('.v').textContent])"),
      [["지목", "대"], ["공부면적", "1770.5 ㎡ (토지대장)"], ["공시지가", "12,340,000원/㎡ (2026년 1월 기준)"], ["용도지역", "일반상업지역"]]);
    assert.match(await cdp.eval("document.getElementById('parcel-attrs').textContent"), /소유구분개인 · 변동 2017-01-01/);
    assert.match(await cdp.eval("document.getElementById('parcel-attrs').textContent"), /속성 기준일2026-09-05 \(토지 자료 기준/);
    assert.ok(!(await cdp.eval("document.getElementById('parcel-panel').textContent")).includes("agrde"));
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    // 재접속 뒤에도 색이 켜져 있다 (IndexedDB meta). 그 뒤 속성 없는 번들로 돌아가면 버튼이 꺼지고 색이 없다
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel[class*=\"zone-\"]').length === 6");
    assert.equal(await cdp.eval("document.getElementById('map-zones').getAttribute('aria-pressed')"), "true");
    await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('view-map').hidden");
    await cdp.clickRect("#map-zones");
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel[class*=\"zone-\"]').length === 0");
    assert.equal(await cdp.eval("document.getElementById('zone-legend').hidden"), true);
    await cdp.setFiles("#parcels-file", [parcelsFile]);
    await cdp.waitFor("document.getElementById('parcels-note').textContent.includes('file:parcels.geojson')");
    assert.equal(await cdp.eval("document.getElementById('map-zones').disabled"), true);
    assert.equal(await cdp.eval("document.getElementById('parcel-search-form').hidden"), false);
    await cdp.clickRect('#map-svg path.parcel[data-pnu="9999900100100040002"]');
    await cdp.waitFor("!document.getElementById('parcel-panel').hidden && document.getElementById('parcel-title').textContent === '가상동 4-2'");
    assert.equal(await cdp.eval("document.getElementById('parcel-attrs').hidden"), true, "속성 없는 필지에는 속성 목록이 없다");
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    // 관측 (사진 1장, 태그 1개)
    const photo = join(tmp, "front.png");
    writeFileSync(photo, png1x1([0, 128, 255]));
    await cdp.eval("document.querySelectorAll('#asset-list li > button')[0].click(); 'ok'");
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
    await cdp.eval("document.querySelectorAll('#asset-list li > button')[1].click(); 'ok'");
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
    await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 2");
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
    await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 1");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 0);
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /위치점 있는 물건이 없다\. 목록에서 선택한다/);
    await cdp.setFiles("#seed-file", [seed2]);
    await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 2");
    // 잘못된 시드 파일은 거절되고 목록이 바뀌지 않는다
    const seedBad = join(tmp, "seed-bad.json");
    writeFileSync(seedBad, JSON.stringify([{ ...seedAll[0], extra: 1 }]));
    await cdp.setFiles("#seed-file", [seedBad]);
    await cdp.waitFor("document.getElementById('seed-note').textContent.includes('물건 목록 파일 오류')");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li > button').length"), 2);
    await cdp.eval("document.getElementById('load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 5");
    // 재접속: 기록 유지
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    assert.ok((await cdp.eval("document.getElementById('status-line').textContent")).includes("관측 1"));
    await cdp.waitFor("document.querySelectorAll('#map-svg g.pt').length === 4");
    await cdp.waitFor("document.querySelectorAll('#map-svg path.parcel').length === 6");
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 6개/, "재접속 후 필지 유지 (IndexedDB)");
    assert.match(await cdp.eval("document.getElementById('basemap-note').textContent"), /배경 도형 11개/, "재접속 후 배경 유지 (IndexedDB v3)");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg .layer-base path').length"), 11);
    await cdp.eval("document.getElementById('clear-basemap').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg .layer-base path').length === 0");
    assert.match(await cdp.eval("document.getElementById('basemap-note').textContent"), /^배경 없음/);
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 6개/, "배경을 지워도 필지는 남는다");
    // PC 자료 파일 (J5-023, ADR-17): 가상 정본에서 만든 .j5view.zip 을 넣어 물건·필지·정본 기록·거래를 한 번에 가져오고, 물건·필지 이력을 연도별로 본다
    const viewGen = spawnSync("python3", ["tests/fixtures/make_view.py", tmp], { cwd: ROOT, encoding: "utf8" });
    if (viewGen.error?.code === "ENOENT") console.log("python3 없음: PC 자료 파일 단계 건너뜀");
    else {
      assert.equal(viewGen.status, 0, viewGen.stderr);
      const viewZip = join(tmp, "synthetic.j5view.zip");
      await cdp.eval("document.getElementById('nav-settings').click(); 'ok'");
      await cdp.setFiles("#view-file", [viewZip]);
      await cdp.waitFor("document.getElementById('view-note').textContent.includes('오류')");
      assert.match(await cdp.eval("document.getElementById('view-note').textContent"), /작업 공간\(j5-synthetic-study\)이 설정\(e2e-study\)과 다름/, "다른 정본의 파일은 거절");
      assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li > button').length"), 5, "거절 시 목록 그대로");
      await cdp.eval("document.getElementById('study-id').value = 'j5-synthetic-study'; document.getElementById('save-settings').click(); 'ok'");
      await cdp.waitFor("document.getElementById('settings-note').textContent === '저장됨'");
      await cdp.setFiles("#view-file", [viewZip]);
      await cdp.waitFor("document.getElementById('view-note').textContent.includes('가져왔습니다')");
      assert.match(await cdp.eval("document.getElementById('view-note').textContent"), /^PC 자료를 가져왔습니다: 정본 v\d+ · 물건 5개 · 필지 6개 · 정본 기록 4건 · 거래 6건\. 이 기기의 기록은 그대로입니다\.$/);
      assert.match(await cdp.eval("document.getElementById('view-status').textContent"), /^정본 v\d+ \(PC 생성 .+\) · 정본 기록 4건 · 거래 6건 · 가져오기 .+ · 더 새 정본이 있는지는 PC 에서 확인$/);
      assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 6개 .* · 정본 v\d+ · view:synthetic\.j5view\.zip .* · 정본 연결 포함$/, "파생본의 필지(정본 연결 포함)");
      assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), 1, "이 기기의 기록은 그대로");
      // 물건 이력: 오늘 화면 카드의 '이력' → 연도별 (2026: 정본 관측·목표 매수가, 2025: 확정 연결 거래)
      await cdp.eval("document.getElementById('nav-home').click(); 'ok'");
      await cdp.eval("Array.from(document.querySelectorAll('#asset-list li')).find(li => li.querySelector('.title').textContent === '가상 물건 1').querySelector('.meta button').click(); 'ok'");
      await cdp.waitFor("!document.getElementById('sec-history').hidden");
      assert.equal(await cdp.eval("document.getElementById('history-title').textContent"), "가상 물건 1");
      assert.equal(await cdp.eval("document.activeElement.id"), "history-title", "이력 화면 제목으로 포커스");
      assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#history-list h3')).map(h => h.textContent)"), ["2026", "2025"]);
      const hist = await cdp.eval("Array.from(document.querySelectorAll('#history-list li')).map(li => [li.querySelector('.tl-date').textContent, li.querySelector('.tl-head').textContent, li.querySelector('.tl-text').textContent])");
      // 첫 항목은 이 e2e 가 앞서 이 기기에 저장한 오늘 관측(정본에는 없음), 그 뒤는 정본 기록·거래
      assert.equal(hist.length, 4, JSON.stringify(hist));
      assert.ok(hist[0][1].includes("이 기기") && hist[0][2].startsWith("변화 확인"), JSON.stringify(hist[0]));
      assert.deepEqual(hist.slice(1).map((h) => h[0]), ["2026-09-22", "2026-09-20", "2025-08-01"]);
      assert.ok(hist[1][1].includes("관측") && hist[1][1].includes("정본") && hist[1][2] === "변화 확인 · 1층 임대 광고 부착 · 사진 1장", JSON.stringify(hist[1]));
      assert.ok(hist[2][1].includes("목표 매수가") && hist[2][2].startsWith("목표 매수가 15억"), JSON.stringify(hist[2]));
      assert.ok(hist[3][1].includes("실거래") && hist[3][1].includes("확정 연결") && hist[3][2].startsWith("10억"), JSON.stringify(hist[3]));
      assert.match(await cdp.eval("document.getElementById('history-source').textContent"), /^PC 자료: 정본 v\d+ .* 더 새 정본이 있는지는 PC 에서 확인합니다\.$/);
      assert.equal(await cdp.eval("document.getElementById('history-record').hidden"), false, "물건 이력에서 기록 시작 가능");
      await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await cdp.waitFor("document.getElementById('sec-history').hidden");
      // 필지 이력: 지번 일치(이 필지)·부번 마스킹(번지대) 거래 + 안의 물건 이력. 동 전체 마스킹(*) 거래는 없다
      await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
      await cdp.clickRect('#map-svg path.parcel[data-pnu="9999900100100010000"]');
      await cdp.waitFor("!document.getElementById('parcel-panel').hidden && document.getElementById('parcel-title').textContent === '가상동 1'");
      assert.match(await cdp.eval("document.getElementById('parcel-attrs').textContent"), /공시지가13,000,000원\/㎡ .*소유구분법인 · 변동 2026-09-24.*속성 기준일2026-09-25/, "파생본의 현재 속성과 속성 기준일");
      // 공시지가 추이 (J5-030): 가상 파생본의 필지 1 은 같은 2026년 1월 기준에서 값이 바뀐 두 점
      assert.equal(await cdp.eval("document.getElementById('parcel-price').hidden"), false);
      assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#parcel-price-list li')).map(li => li.textContent)"),
        ["2026년 1월 기준12,340,000원/㎡ · 확인 2026-09-05", "2026년 1월 기준13,000,000원/㎡ (+5.3%) · 확인 2026-09-25 · 같은 기준연월의 값이 바뀜 (정정 여부 확인)"]);
      // 조건으로 필지 찾기 (J5-033): 상업 용도지역 → 필지 1 하나, 지도 테두리 1개, 열기로 패널. 저촉 규제 → 3필지. 지우기 → 강조 없음
      assert.equal(await cdp.eval("document.getElementById('parcel-filter').hidden"), false, "속성 있는 필지 파일에서 보인다");
      await cdp.eval("document.getElementById('parcel-filter').open = true; document.getElementById('pf-zone').value = 'com'; document.getElementById('parcel-filter-form').requestSubmit(); 'ok'");
      await cdp.waitFor("document.getElementById('parcel-filter-note').textContent.includes('일치')");
      assert.match(await cdp.eval("document.getElementById('parcel-filter-note').textContent"), /^속성 있는 필지 6개 중 1개 일치 \(공부면적 큰 순\) · 지도에 테두리로 표시$/);
      assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel.mark').length"), 1);
      assert.equal(await cdp.eval("document.querySelector('#parcel-filter-results li .title').textContent"), "가상동 1");
      // 물건 조건과 결과 줄의 물건 수 (J5-036): 가상 물건 1(관찰)이 필지 1, 가상 물건 2(상세 검토)가 필지 1-1 에 있다
      assert.match(await cdp.eval("document.querySelector('#parcel-filter-results li .sub').textContent"), / · 물건 1개 \(관찰목록 1\)$/);
      await cdp.eval("document.getElementById('pf-zone').value = ''; document.getElementById('pf-assets').value = 'watch'; document.getElementById('parcel-filter-form').requestSubmit(); 'ok'");
      await cdp.waitFor("document.getElementById('parcel-filter-note').textContent.includes('2개 일치')");
      assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#parcel-filter-results li .title')).map(t => t.textContent).sort()"), ["가상동 1", "가상동 1-1"]);
      await cdp.eval("document.getElementById('pf-assets').value = 'none'; document.getElementById('parcel-filter-form').requestSubmit(); 'ok'");
      await cdp.waitFor("document.getElementById('parcel-filter-note').textContent.includes('일치') && document.querySelectorAll('#parcel-filter-results li').length > 0");
      assert.equal(await cdp.eval("Array.from(document.querySelectorAll('#parcel-filter-results li .sub')).every(s => s.textContent.endsWith(' · 물건 없음'))"), true, "물건 없는 필지만");
      // 결과의 필지를 물건으로 만들면 "물건 없음" 결과에서 빠지고, 지우면 돌아온다 (리뷰 반영 PR #83: 물건 목록이 바뀌면 마지막 조건으로 다시 그린다)
      const noneBefore = await cdp.eval("document.querySelectorAll('#parcel-filter-results li').length");
      const firstTitle = await cdp.eval("document.querySelector('#parcel-filter-results li .title').textContent");
      await cdp.eval("document.querySelector('#parcel-filter-results li button').click(); 'ok'");
      await cdp.waitFor(`document.getElementById('parcel-title').textContent === ${JSON.stringify(firstTitle)} && !document.getElementById('parcel-new-asset').hidden`);
      await cdp.eval("document.getElementById('parcel-new-asset').click(); 'ok'");
      await cdp.waitFor("!document.getElementById('sec-observe').hidden");
      await cdp.waitFor(`document.querySelectorAll('#parcel-filter-results li').length === ${noneBefore - 1}`);
      assert.equal(await cdp.eval(`Array.from(document.querySelectorAll('#parcel-filter-results li .title')).some(t => t.textContent === ${JSON.stringify(firstTitle)})`), false);
      await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
      await cdp.waitFor("document.getElementById('sec-observe').hidden");
      await cdp.eval("Array.from(document.querySelectorAll('#asset-list button')).find(b => b.textContent === '삭제').click(); 'ok'");
      await cdp.waitFor(`document.querySelectorAll('#parcel-filter-results li').length === ${noneBefore}`);
      await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 5");
      await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
      await cdp.eval("document.getElementById('pf-assets').value = ''; document.getElementById('pf-zone').value = 'com'; document.getElementById('parcel-filter-form').requestSubmit(); 'ok'");
      await cdp.waitFor("document.getElementById('parcel-filter-note').textContent.includes('1개 일치')");
      await cdp.eval("document.getElementById('pf-zone').value = ''; document.getElementById('pf-restricted').checked = true; document.getElementById('parcel-filter-form').requestSubmit(); 'ok'");
      await cdp.waitFor("document.getElementById('parcel-filter-note').textContent.includes('3개 일치')");
      assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel.mark').length"), 3);
      await cdp.eval("document.getElementById('pf-restricted').checked = false; document.getElementById('pf-area-min').value = '9'; document.getElementById('pf-area-max').value = '1'; document.getElementById('parcel-filter-form').requestSubmit(); 'ok'");
      await cdp.waitFor("document.getElementById('parcel-filter-note').textContent.startsWith('조건 확인')");
      assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel.mark').length"), 0, "잘못된 조건이면 강조를 지운다");
      await cdp.eval("document.getElementById('pf-clear').click(); 'ok'");
      assert.equal(await cdp.eval("document.getElementById('pf-area-min').value + document.getElementById('parcel-filter-note').textContent"), "");
      await cdp.eval("document.getElementById('pf-zone').value = 'com'; document.getElementById('parcel-filter-form').requestSubmit(); 'ok'");
      await cdp.waitFor("document.querySelectorAll('#parcel-filter-results li button').length === 1");
      await cdp.eval("document.querySelector('#parcel-filter-results li button').click(); 'ok'");
      await cdp.waitFor("document.getElementById('parcel-title').textContent === '가상동 1'");
      // 필지 패널의 안의 물건에 관심 단계 배지 (J5-036)
      assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#parcel-assets .badge[class*=\"track-\"]')).map(b => b.textContent)"), ["관찰"]);
      await cdp.eval("document.getElementById('pf-clear').click(); 'ok'");
      assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel.mark').length"), 0);
      await cdp.eval("document.getElementById('parcel-history').click(); 'ok'");
      await cdp.waitFor("!document.getElementById('sec-history').hidden");
      assert.equal(await cdp.eval("document.getElementById('history-title').textContent"), "가상동 1 필지");
      const phAll = await cdp.eval("Array.from(document.querySelectorAll('#history-list li')).map(li => [li.querySelector('.tl-date').textContent, li.querySelector('.tl-head').textContent, (li.querySelector('.tl-where') || {}).textContent || '', li.querySelector('.tl-text').textContent])");
      // 필지 속성 이력 (J5-026): 토지 자료의 처음 확인(2026-09-05, 자료 종류 3개)과 기준일 2026-09-25 의 변경(공시지가·소유)이 기준일 연도에 든다
      const pa = phAll.filter((h) => h[1].includes("토지 자료"));
      assert.deepEqual(pa.map((h) => [h[0], h[1].includes("처음 확인") ? "first" : "change"]), [["2026-09-25", "change"], ["2026-09-25", "change"], ["2026-09-05", "first"], ["2026-09-05", "first"], ["2026-09-05", "first"]], JSON.stringify(pa));
      assert.ok(pa.some((h) => h[0] === "2026-09-25" && h[1].includes("토지특성") && h[3] === "공시지가 12,340,000원/㎡ → 13,000,000원/㎡"), JSON.stringify(pa));
      assert.ok(pa.some((h) => h[0] === "2026-09-25" && h[1].includes("토지소유") && h[3].startsWith("소유구분 개인 → 법인") && h[3].includes("소유 변동일 2017-01-01 → 2026-09-24")), JSON.stringify(pa));
      assert.ok(pa.some((h) => h[0] === "2026-09-05" && h[1].includes("토지특성") && h[3].startsWith("지목 대 · 공부면적 1770.5 ㎡ · 공시지가 12,340,000원/㎡")), JSON.stringify(pa));
      const ph = phAll.filter((h) => !h[1].includes("토지 자료"));
      assert.deepEqual(ph.slice(1).map((h) => h[0]), ["2026-09-22", "2026-09-20", "2025-08-02", "2025-08-01"], JSON.stringify(ph));
      assert.ok(ph[0][1].includes("이 기기") && ph[0][1].includes("가상 물건 1"), "안의 물건의 이 기기 관측도 물건 이름과 함께");
      assert.ok(ph[3][1].includes("번지대") && ph[3][2] === "가상동 1-*", JSON.stringify(ph[3]));
      assert.ok(ph[4][1].includes("이 필지") && ph[4][2] === "가상동 1", JSON.stringify(ph[4]));
      assert.ok(ph.every((h) => !h[2].includes("가상동 *")), "동 전체 마스킹 거래는 필지에 붙지 않는다");
      assert.equal(await cdp.eval("document.getElementById('history-record').hidden"), true, "필지 이력에는 기록 시작 없음");
      assert.deepEqual(await cdp.eval("Array.from(document.querySelectorAll('#history-list h3')).map(h => h.textContent)"), ["2026", "2025"]);
      await cdp.eval("document.getElementById('history-close').click(); 'ok'");
      await cdp.waitFor("document.getElementById('sec-history').hidden");
      // 재접속 뒤 유지
      await cdp.navigate(`${base}/index.html#settings`);
      await cdp.waitFor("document.getElementById('view-status').textContent.startsWith('정본 v')");
      // 관심 단계·관찰목록 필터 (J5-029, ADR-22): 파생본 시드의 정본 출력값을 배지로 보이고 '관찰목록만' 이 목록·지도를 줄이며 선택이 재접속 뒤 남는다
      await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 5");
      assert.equal(await cdp.eval("document.getElementById('asset-filter').hidden"), false, "관심 단계가 있는 시드에서 필터가 보인다");
      assert.equal(await cdp.eval("document.getElementById('asset-filter-note').textContent"), "전체 5개 (관찰목록 2개)");
      assert.match(await cdp.eval("document.getElementById('seed-status').textContent"), /물건 5개 \(연습용\) · 관찰목록 2개 · 마지막 가져오기/);
      const badge = (i) => cdp.eval(`Array.from(document.querySelector('#asset-list li[data-asset-id="7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a5${i}"]').querySelectorAll('.badge[class*="track-"], .badge.src-pending')).map(b => b.textContent)`);
      assert.deepEqual([await badge(0), await badge(1), await badge(2), await badge(3), await badge(4)], [["관찰"], ["상세 검토"], [], ["보류"], ["제외"]], "미검토는 배지 없음");
      assert.equal(await cdp.eval("document.querySelectorAll('#asset-list .badge.track-watchlist').length"), 2);
      assert.equal(await cdp.eval("Array.from(document.querySelectorAll('#asset-list button')).some(b => /단계|관찰목록/.test(b.textContent))"), false, "관심 단계를 바꾸는 버튼은 폰에 없다");
      await cdp.eval("document.querySelector('#asset-filter button[data-filter=\"watchlist\"]').click(); 'ok'");
      await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 2");
      assert.equal(await cdp.eval("document.getElementById('asset-filter-note').textContent"), "관찰목록 2개만 표시 (전체 5개)");
      assert.equal(await cdp.eval("document.querySelector('#asset-filter button[data-filter=\"watchlist\"]').getAttribute('aria-pressed')"), "true");
      assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 2, "지도 점도 관찰목록만");
      assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /^위치점 2개 표시/);
      await cdp.navigate(`${base}/index.html#home`);
      await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 2");
      assert.equal(await cdp.eval("document.querySelector('#asset-filter button[data-filter=\"watchlist\"]').getAttribute('aria-pressed')"), "true", "필터 선택이 재접속 뒤 남는다");
      await cdp.eval("document.getElementById('start-observing').click(); 'ok'");
      await cdp.waitFor("!document.getElementById('sec-observe').hidden");
      assert.match(await cdp.eval("document.getElementById('target-label').textContent"), /^가상 물건 [12]$/, "관측 시작은 관찰목록 안에서 고른다");
      await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
      await cdp.waitFor("document.getElementById('sec-observe').hidden");
      await cdp.eval("document.querySelector('#asset-filter button[data-filter=\"all\"]').click(); 'ok'");
      await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 5");
      assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 4);
      await cdp.navigate(`${base}/index.html#settings`);
      await cdp.waitFor("document.getElementById('view-status').textContent.startsWith('정본 v')");
      // 필지가 없는 파생본으로 바꾸면 기존 필지도 지워진다 (리뷰 반영: 같은 정본 버전의 자료만 남긴다)
      const noParcels = spawnSync("python3", ["tests/fixtures/make_view.py", tmp, "--no-parcels"], { cwd: ROOT, encoding: "utf8" });
      assert.equal(noParcels.status, 0, noParcels.stderr);
      await cdp.setFiles("#view-file", [join(tmp, "synthetic-noparcels.j5view.zip")]);
      await cdp.waitFor("document.getElementById('view-note').textContent.includes('가져왔습니다')");
      assert.match(await cdp.eval("document.getElementById('view-note').textContent"), /물건 5개 · 필지 0개 · 정본 기록 4건 · 거래 6건/);
      assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /^필지 없음/, "파생본에 필지가 없으면 기존 필지를 지운다");
      assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel').length"), 0);
      await cdp.setFiles("#view-file", [viewZip]);
      await cdp.waitFor("document.getElementById('view-note').textContent.includes('필지 6개')");
      // 지우기 뒤 물건·필지·이 기기 기록은 그대로
      await cdp.eval("document.getElementById('clear-view').click(); 'ok'");
      await cdp.waitFor("document.getElementById('view-status').textContent.startsWith('가져온 PC 자료가 없습니다')");
      await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 5");
      assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 6개/);
      assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), 1);
      await cdp.eval("document.getElementById('study-id').value = 'e2e-study'; document.getElementById('save-settings').click(); 'ok'");
      await cdp.waitFor("document.getElementById('settings-note').textContent === '저장됨'");
      await cdp.navigate(`${base}/index.html`);
      await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 5");
    }
    // 배경 타일 (J5-024, ADR-18): 설정에서 같은 출처 템플릿을 켜면 지도 맨 아래에 <image> 타일이 깔리고, 미리 받기로 서비스 워커 캐시에 저장되며, 끄면 사라진다
    await cdp.eval("document.getElementById('nav-settings').click(); 'ok'");
    await cdp.eval("document.getElementById('tiles-provider').value = 'custom'; document.getElementById('tiles-provider').dispatchEvent(new Event('change')); 'ok'");
    assert.equal(await cdp.eval("document.getElementById('tiles-template-field').hidden"), false);
    await cdp.eval("document.getElementById('tiles-template').value = 'https://tile.example.org/{z}/{x}/{y}.png'; document.getElementById('tiles-save').click(); 'ok'");
    await cdp.waitFor("document.getElementById('tiles-note').textContent.includes('허용되지 않은 호스트')");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg .layer-tiles image').length"), 0, "허용 밖 호스트는 켜지지 않는다");
    await cdp.eval("document.getElementById('tiles-template').value = './tiles/{key}/{z}/{x}/{y}.png'; document.getElementById('tiles-template').dispatchEvent(new Event('input')); document.getElementById('tiles-save').click(); 'ok'");
    await cdp.waitFor("document.getElementById('tiles-note').textContent.includes('인증키가 필요')");
    assert.equal(await cdp.eval("document.getElementById('tiles-key-field').hidden"), false, "{key} 가 있으면 키 입력이 열린다");
    await cdp.eval("document.getElementById('tiles-key').value = 'e2e-tile-key'; document.getElementById('tiles-save').click(); 'ok'");
    await cdp.waitFor("document.getElementById('tiles-note').textContent.startsWith('저장했습니다')");
    assert.ok(!(await cdp.eval("document.getElementById('tiles-note').textContent")).includes("e2e-tile-key"), "안내에 키가 없다");
    await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg .layer-tiles image').length > 0");
    const hrefs = await cdp.eval("Array.from(document.querySelectorAll('#map-svg .layer-tiles image')).map(i => i.getAttribute('href'))");
    assert.ok(hrefs.every((h) => /^\.\/tiles\/\{key\}\/\d+\/\d+\/\d+\.png$/.test(h)), JSON.stringify(hrefs.slice(0, 3)));
    assert.equal(await cdp.eval("document.querySelector('#map-svg').firstElementChild.className.baseVal"), "layer-tiles", "타일 층은 맨 아래");
    assert.equal(await cdp.eval("document.querySelector('#map-svg text.map-attrib').textContent"), "배경: 사용자 지정 타일");
    await cdp.waitFor(`(${JSON.stringify(tileReqs.length)}, true)`);
    await cdp.waitFor("document.getElementById('map-note').textContent.includes('배경 타일 켜짐')");
    await new Promise((r) => setTimeout(r, 300));
    assert.ok(tileReqs.length > 0, "타일 요청이 서버에 닿는다");
    const reqBefore = tileReqs.length;
    await cdp.clickRect("#map-zoom-in");
    await new Promise((r) => setTimeout(r, 300));
    assert.ok(tileReqs.length > reqBefore, "확대하면 다음 단계 타일을 받는다");
    // 현재 위치 + 타일 켜짐 (J5-027 리뷰 반영): 화면을 옮기지 않아 현재 위치 주변 타일을 요청하지 않는다. 점·패널은 열린다
    await cdp.send("Browser.grantPermissions", { origin: base, permissions: ["geolocation"] });
    await cdp.send("Emulation.setGeolocationOverride", { latitude: 37.5705, longitude: 126.9996, accuracy: 9 });
    const reqBeforeLocate = tileReqs.length;
    const viewBefore = await cdp.eval("document.querySelector('#map-svg .layer-parcels').getAttribute('transform')");
    await cdp.clickRect("#map-locate");
    await cdp.waitFor("document.getElementById('parcel-search-note').textContent.includes('배경 타일이 켜져 있어 화면을 옮기지 않았습니다')");
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(tileReqs.length, reqBeforeLocate, "위치를 읽어도 타일 요청이 늘지 않는다");
    assert.equal(await cdp.eval("document.querySelector('#map-svg .layer-parcels').getAttribute('transform')"), viewBefore, "화면이 그대로다");
    assert.equal(await cdp.eval("document.querySelector('#map-svg .layer-locate').getAttribute('visibility')"), "visible");
    assert.equal(await cdp.eval("document.getElementById('parcel-title').textContent"), "가상동 4-2");
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    await cdp.send("Browser.resetPermissions", {});
    await cdp.send("Emulation.clearGeolocationOverride", {});
    // 미리 받기: 대상·필지 범위를 14~18 단계로. 같은 출처라 서비스 워커가 저장한다
    await cdp.eval("document.getElementById('nav-settings').click(); 'ok'");
    await cdp.eval("document.getElementById('tiles-prefetch').click(); 'ok'");
    await cdp.waitFor("document.getElementById('tiles-prefetch-note').textContent.startsWith('받기 끝')", 60000);
    assert.match(await cdp.eval("document.getElementById('tiles-prefetch-note').textContent"), /^받기 끝: \d+장 성공 · 0장 실패 · 새로 저장 \d+장 \(확대 14~18 단계, 범위 여유 300 m\)$/);
    await cdp.waitFor("/^\\d+장 저장됨$/.test(document.getElementById('tiles-cache-status').textContent) && parseInt(document.getElementById('tiles-cache-status').textContent) > 0", 10000);
    const cached = parseInt(await cdp.eval("document.getElementById('tiles-cache-status').textContent"));
    assert.match(await cdp.eval("document.getElementById('tiles-prefetch-note').textContent"), /새로 저장 [1-9]\d*장/, "실제 저장 수를 보고한다");
    const cacheKeys = await cdp.eval("caches.open('j5-tiles-v1').then(c => c.keys()).then(ks => ks.map(k => k.url))");
    assert.ok(cacheKeys.length > 0 && cacheKeys.every((u) => u.includes("%7Bkey%7D") && !u.includes("e2e-tile-key")), "캐시 키에 인증키가 없다: " + cacheKeys[0]);
    // 같은 타일을 다시 보면 서비스 워커 캐시에서 온다 (서버 요청 수 불변)
    const reqAfterPrefetch = tileReqs.length;
    await cdp.navigate(`${base}/index.html#map`);
    await cdp.waitFor("document.querySelectorAll('#map-svg .layer-tiles image').length > 0");
    await new Promise((r) => setTimeout(r, 400));
    assert.equal(tileReqs.length, reqAfterPrefetch, "저장된 타일은 서버에 다시 요청하지 않는다");
    // 끄기 → 타일 없음, 다시 켜기 (오프라인 단계에서 저장된 타일로 보이는지 확인)
    await cdp.eval("document.getElementById('nav-settings').click(); 'ok'");
    await cdp.eval("document.getElementById('tiles-provider').value = ''; document.getElementById('tiles-provider').dispatchEvent(new Event('change')); document.getElementById('tiles-save').click(); 'ok'");
    await cdp.waitFor("document.getElementById('tiles-note').textContent.includes('끄고')");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg .layer-tiles image').length"), 0);
    await cdp.waitFor("!document.getElementById('map-note').textContent.includes('배경 타일')");
    await cdp.eval("document.getElementById('tiles-provider').value = 'custom'; document.getElementById('tiles-provider').dispatchEvent(new Event('change')); document.getElementById('tiles-template').value = './tiles/{key}/{z}/{x}/{y}.png'; document.getElementById('tiles-key').value = 'e2e-tile-key'; document.getElementById('tiles-save').click(); 'ok'");
    await cdp.waitFor("document.getElementById('tiles-note').textContent.startsWith('저장했습니다')");
    assert.ok(cached > 0);
    // 지도 실패 시 목록: SVG 요소 생성만 막아(저장소에서 createElementNS 는 map.js 만 쓴다) 지도가 못 뜨는 상황을 만든다.
    // 앱은 안내만 남기고 목록·기록은 그대로여야 하며 콘솔 오류를 내지 않아야 한다.
    const { identifier: stub } = await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      const orig = document.createElementNS.bind(document);
      document.createElementNS = (ns, name) => { if (String(ns).endsWith('/svg')) throw new Error('e2e: SVG 생성 불가'); return orig(ns, name); };` });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('map-note').textContent.includes('지도 표시 불가')");
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /지도 표시 불가: e2e: SVG 생성 불가\. 목록에서 선택한다/);
    await cdp.waitFor("document.querySelectorAll('#asset-list li > button').length === 5");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 0);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg path.parcel').length"), 0);
    assert.match(await cdp.eval("document.getElementById('parcels-note').textContent"), /필지 6개/, "지도 실패해도 필지 안내는 남는다");
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), 1, "지도 실패해도 기록 목록 유지");
    // 지도가 못 떠도 지번 찾기는 페이지 이동 없이 필지 패널을 연다 (J5-027 리뷰 반영)
    await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
    assert.equal(await cdp.eval("document.getElementById('parcel-search-form').hidden"), false);
    await cdp.eval("document.getElementById('parcel-search').value = '1-1'; document.getElementById('parcel-search-go').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('parcel-panel').hidden && document.getElementById('parcel-title').textContent === '가상동 1-1'");
    assert.ok(!(await cdp.eval("location.href")).includes("?") && (await cdp.eval("location.pathname")).endsWith("/index.html"), "폼 제출이 페이지 이동(GET ?…)으로 이어지지 않는다");
    await cdp.eval("document.getElementById('parcel-close').click(); 'ok'");
    await cdp.eval("document.querySelectorAll('#asset-list li > button')[2].click(); 'ok'");
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
    // 오프라인에서도 미리 받아 둔 배경 타일은 서비스 워커 캐시에서 나온다 (J5-024)
    await cdp.eval("document.getElementById('nav-map').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#map-svg .layer-tiles image').length > 0");
    assert.ok((await cdp.eval("caches.open('j5-tiles-v1').then(c => c.keys()).then(k => k.length)")) > 0, "타일 캐시는 앱 캐시와 별도로 남는다");
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
    await cdp.eval("document.querySelectorAll('#asset-list li > button')[0].click(); 'ok'");
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
