// 필지 번들 검증·기하 (J5-013B-1). DOM 없이 가상 fixture 로 확인한다.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateParcels, labelPoint, pointInFeature, assetsInParcel, parcelAssets, BASIS_LABEL, parcelTitle, fmtArea, polygonsOf, MAX_PARCELS, zoneCategory, ZONE_LABELS, fmtInt, attrLines, attrsSummary, fmtAttrValue, attrChangeText, parseParcelQuery, findParcels, parcelAt, interiorPoint, priceTrend, priceTrendRow } from "../../web/app/parcels.js";
import { worldBbox, parcelPathD, parcelLabelVisible, mercator, fitView, FIT_MAX_SCALE, PARCEL_LOCAL_K } from "../../web/app/map.js";

const bundle = () => JSON.parse(readFileSync(new URL("../fixtures/parcels/synthetic.j5parcels.json", import.meta.url), "utf8"));
const vwBundle = () => JSON.parse(readFileSync(new URL("../fixtures/parcels/synthetic_vworld.j5parcels.json", import.meta.url), "utf8"));
const seed = () => JSON.parse(readFileSync(new URL("../fixtures/assets.seed.synthetic.json", import.meta.url), "utf8"));
const byLabel = (b) => Object.fromEntries(b.features.map((f) => [f.properties.label, f]));

test("validateParcels: fixture 통과, 흔한 오류 감지", () => {
  const b = bundle();
  assert.deepEqual(validateParcels(b), []);
  assert.equal(b.features.length, 6);
  const dup = bundle(); dup.features.push(dup.features[0]); dup.count = 7;
  assert.ok(validateParcels(dup).some((e) => e.includes("PNU 중복")));
  const cnt = bundle(); cnt.count = 5;
  assert.ok(validateParcels(cnt).some((e) => e.includes("count")));
  const ver = bundle(); ver.j5parcels = "2.0.0";
  assert.ok(validateParcels(ver).some((e) => e.includes("형식 버전")));
  const mode = bundle(); mode.data_mode = "private_real";
  assert.ok(validateParcels(mode).some((e) => e.includes("data_mode")));
  const extra = bundle(); extra.extra = 1;
  assert.ok(validateParcels(extra).some((e) => e.includes("허용되지 않은 필드")));
  const badId = bundle(); badId.features[0].id = "123"; badId.features[0].properties.pnu = "123";
  assert.ok(validateParcels(badId).some((e) => e.includes("19자리")));
  const geom = bundle(); geom.features[1].geometry = { type: "Point", coordinates: [127, 37] };
  assert.ok(validateParcels(geom).some((e) => e.includes("Polygon")));
  const ring = bundle(); ring.features[1].geometry.coordinates[0] = [[127, 37], [127.1, 37]];
  assert.ok(validateParcels(ring).some((e) => e.includes("좌표 형식")));
  const noSrc = bundle(); delete noSrc.source.geometry_version;
  assert.ok(validateParcels(noSrc).some((e) => e.includes("geometry_version")));
  const areaNull = bundle(); areaNull.features[0].properties.area_m2_geom = null;
  assert.ok(validateParcels(areaNull).some((e) => e.includes("area_missing_reason")), "null 면적은 사유가 필요");
  areaNull.features[0].properties.area_missing_reason = "source_geographic_crs";
  assert.deepEqual(validateParcels(areaNull), []);
  assert.deepEqual(validateParcels([]), ["객체가 아님"]);
  assert.ok(validateParcels({ ...bundle(), features: "x" }).some((e) => e.includes("배열")));
  const many = bundle(); many.features = Array.from({ length: MAX_PARCELS + 1 }, (_, i) => ({ ...many.features[0], id: String(1e18 + i).padStart(19, "0") })); many.count = many.features.length;
  assert.ok(validateParcels(many).some((e) => e.includes("넘음")));
});

test("labelPoint·pointInFeature·assetsInParcel: 구멍·다중 조각·물건 연결", () => {
  const b = bundle(), by = byLabel(b), s = seed();
  const p1 = by["1"];
  const lp = labelPoint(p1);
  assert.ok(pointInFeature(lp, p1), "라벨 점은 필지 안");
  assert.ok(Math.abs(lp[0] - 126.99885) < 1e-6 && Math.abs(lp[1] - 37.5702) < 1e-6, "사각형은 중심");
  // 구멍: 구멍 안의 점은 밖, 구멍 밖은 안
  const hole = by["4-2"];
  assert.equal(polygonsOf(hole)[0].length, 2);
  assert.equal(pointInFeature([126.9994, 37.5704], hole), false, "구멍 안");
  assert.equal(pointInFeature([126.9995, 37.57055], hole), true);
  assert.equal(pointInFeature([126.99, 37.57], hole), false, "bbox 밖 빠른 거절");
  // 두 조각
  const mt = by["산1-2"];
  assert.equal(polygonsOf(mt).length, 2);
  assert.equal(pointInFeature([127.0001, 37.5703], mt), true);
  assert.equal(pointInFeature([127.0002, 37.5703], mt), false, "조각 사이");
  assert.ok(pointInFeature(labelPoint(mt), mt));
  // 물건 연결 (가상 시드: 1→1, 2→1-1, 4→2, 5→3, 3 은 주소만)
  assert.deepEqual(assetsInParcel(p1, s).map((a) => a.label), ["가상 물건 1"]);
  assert.deepEqual(assetsInParcel(by["1-1"], s).map((a) => a.label), ["가상 물건 2"]);
  assert.deepEqual(assetsInParcel(by["2"], s).map((a) => a.label), ["가상 물건 4"]);
  assert.deepEqual(assetsInParcel(by["3"], s).map((a) => a.label), ["가상 물건 5"]);
  assert.deepEqual(assetsInParcel(hole, s), []);
  assert.deepEqual(assetsInParcel(mt, s), []);
  // 정본 연결(asset_ids, 파생본): 주소만 있는 물건 3 을 필지 4-2 에 연결하면 위치점 없이도 뜬다. 위치점 포함과 근거를 구분한다
  const linkedHole = { ...hole, properties: { ...hole.properties, asset_ids: [s[2].asset_id] } };
  assert.deepEqual(parcelAssets(linkedHole, s).map((x) => [x.asset.label, x.basis]), [["가상 물건 3", "linked"]]);
  const linkedP1 = { ...p1, properties: { ...p1.properties, asset_ids: [s[0].asset_id, s[2].asset_id] } };
  assert.deepEqual(parcelAssets(linkedP1, s).map((x) => [x.asset.label, x.basis]), [["가상 물건 1", "both"], ["가상 물건 3", "linked"]]);
  assert.deepEqual(parcelAssets(p1, s).map((x) => [x.asset.label, x.basis]), [["가상 물건 1", "inside"]]);
  assert.deepEqual(parcelAssets(linkedP1, s, { linksValid: false }).map((x) => [x.asset.label, x.basis]), [["가상 물건 1", "inside"]], "시드가 바뀐 뒤에는 정본 연결을 쓰지 않는다");
  const proj = bundle(); proj.study_id = "j5-synthetic-study"; proj.source_dataset_version = 3;
  assert.deepEqual(validateParcels(proj), [], "파생본의 study_id·source_dataset_version 허용");
  proj.source_dataset_version = -1;
  assert.ok(validateParcels(proj).some((e) => e.includes("source_dataset_version")));
  assert.equal(BASIS_LABEL.inside, "위치점 포함 (연결 미확정)");
  const withIds = bundle(); withIds.features[0].properties.asset_ids = [s[0].asset_id]; withIds.features[0].properties.geometry_version = "2026-09-01";
  assert.deepEqual(validateParcels(withIds), [], "파생본의 asset_ids·geometry_version 허용");
  withIds.features[0].properties.asset_ids = "x";
  assert.ok(validateParcels(withIds).some((e) => e.includes("asset_ids")));
  assert.equal(parcelTitle(p1.properties), "가상동 1");
  assert.equal(parcelTitle({ ...p1.properties, emd_name: null }), "9999900100 1");
  assert.equal(fmtArea(12.34), "12.3 ㎡");
  assert.equal(fmtArea(null, "source_geographic_crs"), "미확인 (source_geographic_crs)");
});

test("map: worldBbox·parcelPathD·parcelLabelVisible", () => {
  const b = bundle(), by = byLabel(b);
  const wb = worldBbox(by["1"].properties.bbox);
  assert.ok(wb.x0 < wb.x1 && wb.y0 < wb.y1);
  const origin = { x: (wb.x0 + wb.x1) / 2, y: (wb.y0 + wb.y1) / 2 };
  const d = parcelPathD(polygonsOf(by["1"]), origin);
  assert.match(d, /^M-?\d+\.\d -?\d+\.\d(L-?\d+\.\d -?\d+\.\d){4}Z$/, "닫힌 사각형 하나");
  assert.equal((parcelPathD(polygonsOf(by["4-2"]), origin).match(/M/g) || []).length, 2, "구멍은 두 번째 서브패스");
  assert.equal((parcelPathD(polygonsOf(by["산1-2"]), origin).match(/M/g) || []).length, 2);
  // 로컬 단위: 필지 1 의 폭 약 53m → 37.6° 에서 1 단위 ≈ 0.118m → 약 450 단위
  const xs = d.match(/-?\d+\.\d/g).filter((_, i) => i % 2 === 0).map(Number);
  const width = Math.max(...xs) - Math.min(...xs);
  assert.ok(width > 400 && width < 500, `로컬 폭 ${width}`);
  // 라벨 표시: 전체 보기(FIT_MAX_SCALE)에서 폭 53m ≈ 56px → "1" 은 보이고, 8배 축소면 숨김
  const w = 320, h = 280;
  const view = fitView([{ x: wb.x0, y: wb.y0 }, { x: wb.x1, y: wb.y1 }], w, h);
  assert.equal(view.scale, FIT_MAX_SCALE);
  assert.equal(parcelLabelVisible(wb, "1", view, w, h), true);
  assert.equal(parcelLabelVisible(wb, "1234567890", view, w, h), false, "긴 라벨은 더 큰 폭이 필요");
  assert.equal(parcelLabelVisible(wb, "1", { ...view, scale: view.scale / 8 }, w, h), false);
  assert.equal(parcelLabelVisible(wb, "1", { ...view, tx: view.tx + 10000 }, w, h), false, "화면 밖");
  assert.equal(PARCEL_LOCAL_K, 2 ** 28);
  assert.ok(mercator([127, 37.57]).x > 0.85);
});

// ---- 필지 속성 (J5-025, ADR-19)
test("validateParcels: 속성 번들(VWorld 가상 묶음) 통과, attrs·attrs_sources 형식 오류 감지, 개인 필드 없음", () => {
  const b = vwBundle();
  assert.deepEqual(validateParcels(b), []);
  assert.equal(b.features.filter((f) => f.properties.attrs).length, 6);
  assert.deepEqual(b.attrs_sources.map((a) => a.kind), ["land_feature", "land_plan", "land_ownership"]);
  const text = JSON.stringify(b);
  assert.ok(!text.includes("agrde") && !text.includes("resdnc"), "연령대·거주 구분은 번들에 없다");
  const badSrc = vwBundle(); badSrc.attrs_sources = [{ kind: 1 }];
  assert.ok(validateParcels(badSrc).some((e) => e.includes("attrs_sources")));
  const extra = vwBundle(); extra.features[0].properties.attrs.owner_name = "x";
  assert.ok(validateParcels(extra).some((e) => e.includes("attrs.owner_name")), "소유자 이름 같은 허용되지 않은 필드는 거절");
  const arr = vwBundle(); arr.features[0].properties.attrs = [];
  assert.ok(validateParcels(arr).some((e) => e.includes("attrs 가 객체가 아님")));
  const zones = vwBundle(); zones.features[0].properties.attrs.plan_zones = [{ name: "x" }];
  assert.ok(validateParcels(zones).some((e) => e.includes("plan_zones")));
  const neg = vwBundle(); neg.features[0].properties.attrs.official_land_price_krw_m2 = -1;
  assert.ok(validateParcels(neg).some((e) => e.includes("official_land_price_krw_m2")));
});

test("zoneCategory·fmtInt·attrLines·attrsSummary", () => {
  assert.deepEqual(["제1종전용주거지역", "제2종일반주거지역", "준주거지역", "일반상업지역", "준공업지역", "자연녹지지역", "보전관리지역", "농림지역", "개발제한구역", null, "", 3].map(zoneCategory),
    ["res1", "res2", "res3", "com", "ind", "green", "rural", "rural", "other", null, null, null]);
  assert.ok(Object.keys(ZONE_LABELS).length === 8);
  assert.deepEqual([fmtInt(12340000), fmtInt(999), fmtInt(0), fmtInt(null)], ["12,340,000", "999", "0", null]);
  const by = byLabel(vwBundle());
  const l1 = Object.fromEntries(attrLines(by["1"].properties.attrs));
  assert.equal(l1["지목"], "대");
  assert.equal(l1["공부면적"], "1770.5 ㎡ (토지대장)");
  assert.equal(l1["공시지가"], "12,340,000원/㎡ (2026년 1월 기준)");
  assert.equal(l1["용도지역"], "일반상업지역");
  assert.equal(l1["규제·지역지구"], "도시지역, 일반상업지역, 지구단위계획구역(가상)");
  assert.equal(l1["소유구분"], "개인 · 변동 2017-01-01");
  assert.equal(l1["속성 기준일"], "미확인", "변환 번들에는 as_of 가 없고 fallback 도 안 주면 미확인");
  assert.equal(Object.fromEntries(attrLines(by["1"].properties.attrs, "2026-09-05"))["속성 기준일"], "2026-09-05 (토지 자료 기준, 도형 기준일과 다를 수 있음)");
  assert.equal(Object.fromEntries(attrLines({ ...by["1"].properties.attrs, as_of: "2026-08-01" }, "2026-09-05"))["속성 기준일"], "2026-08-01 (토지 자료 기준, 도형 기준일과 다를 수 있음)", "파생본의 as_of 가 우선");
  const badAsOf = vwBundle(); badAsOf.features[0].properties.attrs.as_of = "2026/08/01";
  assert.ok(validateParcels(badAsOf).some((e) => e.includes("as_of")));
  const l2 = Object.fromEntries(attrLines(by["2"].properties.attrs));
  assert.equal(l2["용도지역"], "제3종일반주거지역 · 준주거지역");
  assert.match(l2["규제·지역지구"], /ZA0014\(저촉\) … \(이름 일부는 원본 열 길이에 잘림/);
  assert.equal(l2["소유구분"], "개인 · 공유 3인 · 변동 2011-07-07");
  const l3 = Object.fromEntries(attrLines(by["3"].properties.attrs));
  assert.equal(l3["공시지가"], "미확인", "빈 값은 미확인 (0 이 아니다)");
  assert.equal(l3["소유구분"], "미확인");
  const l42 = Object.fromEntries(attrLines(by["4-2"].properties.attrs));
  assert.equal(l42["도로접면"], "미확인");
  assert.equal(l42["소유구분"], "국유지 · 변동 1995-01-01");
  assert.deepEqual(attrLines(null), []);
  assert.deepEqual(attrLines(undefined), []);
  const sm = attrsSummary(vwBundle());
  assert.equal(sm.withAttrs, 6);
  assert.deepEqual([...sm.byZone.entries()].sort(), [["com", 1], ["green", 1], ["ind", 1], ["res2", 2], ["rural", 1]]);
  assert.equal(sm.sources.length, 3);
  assert.deepEqual(attrsSummary(bundle()).withAttrs, 0, "기존 번들에는 속성이 없다");
  assert.deepEqual(attrsSummary(null).withAttrs, 0);
});

test("attrs_history 검증·fmtAttrValue·attrChangeText (J5-026)", () => {
  const withHist = vwBundle();
  withHist.features[0].properties.attrs_history = [{ as_of: "2026-09-05", kind: "land_feature", first: true, changes: { jimok_name: { from: null, to: "대" } } },
    { as_of: "2027-01-15", kind: "land_ownership", first: false, changes: { ownership_kind: { from: "개인", to: "법인" } } }];
  assert.deepEqual(validateParcels(withHist), []);
  const bad1 = vwBundle(); bad1.features[0].properties.attrs_history = [{ as_of: "2026-09-05", kind: "weather", first: true, changes: { x: { from: null, to: 1 } } }];
  assert.ok(validateParcels(bad1).some((e) => e.includes("attrs_history 항목")));
  const bad2 = vwBundle(); bad2.features[0].properties.attrs_history = [{ as_of: "2026-09-05", kind: "land_plan", first: true, changes: { x: { to: 1 } } }];
  assert.ok(validateParcels(bad2).some((e) => e.includes("attrs_history 항목")));
  const bad3 = vwBundle(); bad3.features[0].properties.attrs_history = {};
  assert.ok(validateParcels(bad3).some((e) => e.includes("attrs_history 형식")));
  const owner = vwBundle(); owner.features[0].properties.attrs_history = [{ as_of: "2026-09-05", kind: "land_ownership", first: true, changes: { owner_name: { from: null, to: "x" } } }];
  assert.ok(validateParcels(owner).some((e) => e.includes("허용되지 않은 필드 owner_name")), "소유자 이름 같은 필드는 이력으로도 거절");
  const wrongKind = vwBundle(); wrongKind.features[0].properties.attrs_history = [{ as_of: "2026-09-05", kind: "land_plan", first: true, changes: { jimok_name: { from: null, to: "대" } } }];
  assert.ok(validateParcels(wrongKind).some((e) => e.includes("허용되지 않은 필드 jimok_name")), "자료 종류에 없는 필드도 거절");
  assert.deepEqual([fmtAttrValue("official_land_price_krw_m2", 12340000), fmtAttrValue("registered_area_m2", 60.2), fmtAttrValue("jimok_name", null), fmtAttrValue("plan_zones_truncated", true), fmtAttrValue("plan_zones", []),
    fmtAttrValue("plan_zones", [{ code: "A", name: "도시지역", relation: "포함" }, { code: "ZA0014", name: null, relation: "저촉" }])],
    ["12,340,000원/㎡", "60.2 ㎡", "없음", "예", "없음", "도시지역, ZA0014(저촉)"]);
  assert.equal(attrChangeText({ first: true, changes: { official_land_price_krw_m2: { from: null, to: 1000 }, jimok_name: { from: null, to: "대" } } }), "지목 대 · 공시지가 1,000원/㎡", "표시 순서는 필드 순서");
  assert.equal(attrChangeText({ first: false, changes: { ownership_kind: { from: "개인", to: "법인" }, ownership_changed_on: { from: "2017-01-01", to: "2026-09-24" } } }), "소유구분 개인 → 법인 · 소유 변동일 2017-01-01 → 2026-09-24");
  assert.equal(attrChangeText({ first: false, changes: {} }), "");
  assert.equal(attrChangeText({ first: false, changes: { ownership_kind_code: { from: "01", to: "06" }, price_base_year: { from: 2026, to: 2027 } } }), "", "코드·기준 연월만 바뀐 항목은 빈 글 (이력에 넣지 않음)");
});

test("parseParcelQuery·findParcels·parcelAt (J5-027)", () => {
  assert.deepEqual(parseParcelQuery("182-13"), { emd: null, mountain: false, bon: 182, bu: 13 });
  assert.deepEqual(parseParcelQuery(" 산1 - 2 "), { emd: null, mountain: true, bon: 1, bu: 2 });
  assert.deepEqual(parseParcelQuery("종로5가 182-13대"), { emd: "종로5가", mountain: false, bon: 182, bu: 13 });
  assert.deepEqual(parseParcelQuery("가상동 1"), { emd: "가상동", mountain: false, bon: 1, bu: null });
  assert.deepEqual(parseParcelQuery("9999900100100010001"), { pnu: "9999900100100010001" });
  assert.equal(parseParcelQuery(""), null);
  assert.equal(parseParcelQuery("abc"), null);
  assert.equal(parseParcelQuery(null), null);
  const feats = bundle().features;
  const ids = (r) => r.matches.map((f) => f.properties.label);
  assert.deepEqual(ids(findParcels(feats, "1-1")), ["1-1"]);
  assert.deepEqual(ids(findParcels(feats, "1")), ["1"], "부번을 안 적으면 부번 0 을 먼저");
  assert.deepEqual(ids(findParcels(feats, "4")), ["4-2"], "부번 0 이 없으면 같은 본번 전체");
  assert.deepEqual(ids(findParcels(feats, "산1-2")), ["산1-2"]);
  assert.deepEqual(ids(findParcels(feats, "1-2")), [], "산 여부가 다르면 아님");
  assert.deepEqual(ids(findParcels(feats, "가상동 2")), ["2"]);
  assert.deepEqual(ids(findParcels(feats, "다른동 2")), []);
  assert.deepEqual(ids(findParcels(feats, "9999900100100040002")), ["4-2"]);
  assert.deepEqual(findParcels(feats, "?!").matches, []);
  assert.equal(findParcels(feats, "?!").query, null);
  const many = findParcels(feats.concat(feats.map((f) => ({ ...f, properties: { ...f.properties, bu: 9 } }))), "1", { limit: 1 });
  assert.equal(many.total, 1, "부번 0 이 있으면 그것만");
  assert.equal(findParcels(null, "1").matches.length, 0);
  assert.equal(parcelAt(feats, [126.9996, 37.5705]).properties.label, "4-2");
  assert.equal(parcelAt(feats, [126.9994, 37.5704]), null, "구멍 안은 밖");
  assert.equal(parcelAt(feats, [127.1, 37.6]), null);
  assert.equal(parcelAt(feats, null), null);
  // interiorPoint (J5-028): 항상 필지 안의 점. 구멍 필지·두 조각 필지도
  for (const f of feats) { const p = interiorPoint(f); assert.ok(p && pointInFeature(p, f), f.properties.label); }
  const cshape = { id: "x", geometry: { type: "Polygon", coordinates: [[[0, 0], [3, 0], [3, 3], [0, 3], [0, 2], [2, 2], [2, 1], [0, 1], [0, 0]]] }, properties: { bbox: [0, 0, 3, 3] } };
  const cp = interiorPoint(cshape);
  assert.ok(cp && pointInFeature(cp, cshape), "라벨 위치가 밖이면 격자에서 안쪽 점을 찾는다");
  assert.equal(interiorPoint({ id: "y", geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [0, 0], [0, 0]]] }, properties: {} }), null);
});

test("공시지가 추이 (J5-030): 현재 속성에서 변화 항목을 거꾸로 짚어 기준일마다의 값을 되살린다", () => {
  const attrs = { official_land_price_krw_m2: 14_000_000, price_base_year: 2028, price_base_month: 1, jimok_name: "대", as_of: "2028-06-01" };
  const history = [
    { as_of: "2027-06-01", kind: "land_feature", first: false, changes: { official_land_price_krw_m2: { from: 12_000_000, to: 13_000_000 }, price_base_year: { from: 2026, to: 2027 } } },
    { as_of: "2026-06-01", kind: "land_feature", first: true, changes: { official_land_price_krw_m2: { from: null, to: 12_000_000 }, price_base_year: { from: null, to: 2026 }, price_base_month: { from: null, to: 1 }, jimok_name: { from: null, to: "대" } } },
    { as_of: "2026-06-01", kind: "land_ownership", first: true, changes: { ownership_kind: { from: null, to: "개인" } } },
    { as_of: "2027-09-01", kind: "land_feature", first: false, changes: { land_use_situation: { from: "상업용", to: "업무용" } } },
    { as_of: "2028-06-01", kind: "land_feature", first: false, changes: { official_land_price_krw_m2: { from: 13_000_000, to: 14_000_000 }, price_base_year: { from: 2027, to: 2028 } } },
  ];
  const t = priceTrend(attrs, history);
  assert.deepEqual(t.map((q) => [q.as_of, q.price, q.year, q.month]), [["2026-06-01", 12_000_000, 2026, 1], ["2027-06-01", 13_000_000, 2027, 1], ["2028-06-01", 14_000_000, 2028, 1]],
    "순서가 섞인 항목도 기준일순, 가격과 무관한 변화(이용상황)는 점이 아니다, 소유 자료는 무시");
  assert.equal(t[0].deltaPct, null);
  assert.equal(t[1].deltaPct.toFixed(2), "8.33");
  assert.ok(!t.some((q) => q.sameBase || q.earlier));
  assert.deepEqual(priceTrendRow(t[1]), ["2027년 1월 기준", "13,000,000원/㎡", "+8.3%", "확인 2027-06-01"]);
  assert.deepEqual(priceTrendRow(t[0]), ["2026년 1월 기준", "12,000,000원/㎡", "", "확인 2026-06-01"]);
});

test("공시지가 추이: 같은 기준연월의 값 변경·하락·잘린 이력·이력 없음·가격 없음", () => {
  // 같은 기준연월에서 값만 바뀜 (가상 파생본의 필지 1 과 같은 모양): 정정 여부 확인 표시
  const same = priceTrend({ official_land_price_krw_m2: 13_000_000, price_base_year: 2026, price_base_month: 1 }, [
    { as_of: "2026-09-05", kind: "land_feature", first: true, changes: { official_land_price_krw_m2: { from: null, to: 12_340_000 }, price_base_year: { from: null, to: 2026 }, price_base_month: { from: null, to: 1 } } },
    { as_of: "2026-09-25", kind: "land_feature", first: false, changes: { official_land_price_krw_m2: { from: 12_340_000, to: 13_000_000 } } },
  ]);
  assert.equal(same.length, 2);
  assert.ok(same[1].sameBase && !same[0].sameBase);
  assert.match(priceTrendRow(same[1])[3], /^확인 2026-09-25 · 같은 기준연월의 값이 바뀜/);
  assert.equal(priceTrendRow(same[1])[2], "+5.3%");
  // 하락은 음수
  const down = priceTrend({ official_land_price_krw_m2: 9_000_000, price_base_year: 2027 }, [
    { as_of: "2026-06-01", kind: "land_feature", first: true, changes: { official_land_price_krw_m2: { from: null, to: 10_000_000 }, price_base_year: { from: null, to: 2026 } } },
    { as_of: "2027-06-01", kind: "land_feature", first: false, changes: { official_land_price_krw_m2: { from: 10_000_000, to: 9_000_000 }, price_base_year: { from: 2026, to: 2027 } } },
  ]);
  assert.equal(priceTrendRow(down[1])[2], "-10.0%");
  assert.equal(priceTrendRow(down[1])[0], "2027년 기준", "기준월이 없으면 연도만");
  // 파생본 이력 상한으로 '처음 확인' 이 잘림: 가장 앞 변화의 이전 값은 기준일을 모르는 점
  const cut = priceTrend({ official_land_price_krw_m2: 11_000_000, price_base_year: 2027 }, [
    { as_of: "2027-06-01", kind: "land_feature", first: false, changes: { official_land_price_krw_m2: { from: 10_000_000, to: 11_000_000 }, price_base_year: { from: 2026, to: 2027 } } },
  ]);
  assert.deepEqual(cut.map((q) => [q.as_of, q.price, q.earlier]), [[null, 10_000_000, true], ["2027-06-01", 11_000_000, false]]);
  assert.equal(priceTrendRow(cut[0])[3], "이전 자료 (기준일 모름)");
  // 잘린 이력의 가장 앞 항목이 가격과 무관해도(이용상황만 바뀜) 뒤의 가격 변화에서 이전 값을 되살린다 (리뷰 반영 PR #76)
  const cut2 = priceTrend({ official_land_price_krw_m2: 11_000_000, price_base_year: 2027 }, [
    { as_of: "2026-12-01", kind: "land_feature", first: false, changes: { land_use_situation: { from: "상업용", to: "업무용" } } },
    { as_of: "2027-06-01", kind: "land_feature", first: false, changes: { official_land_price_krw_m2: { from: 10_000_000, to: 11_000_000 }, price_base_year: { from: 2026, to: 2027 } } },
  ]);
  assert.deepEqual(cut2.map((q) => [q.as_of, q.price, q.year, q.earlier]), [[null, 10_000_000, 2026, true], ["2027-06-01", 11_000_000, 2027, false]]);
  assert.equal(priceTrendRow(cut2[1])[2], "+10.0%");
  // 잘렸지만 남은 항목에 가격 변화가 없으면 현재 값 하나뿐이다 (추이 표 없음)
  const cut3 = priceTrend({ official_land_price_krw_m2: 11_000_000, price_base_year: 2027, as_of: "2027-09-01" }, [
    { as_of: "2027-09-01", kind: "land_feature", first: false, changes: { road_side: { from: "세로한면", to: "광대로한면" } } },
  ]);
  assert.deepEqual(cut3.map((q) => [q.as_of, q.price, q.earlier]), [["2027-09-01", 11_000_000, false]]);
  // 이력 없는 번들(J5-025): 현재 값 하나. 공시지가 없는 필지: 점 없음. 속성 없음: 빈 목록
  assert.deepEqual(priceTrend({ official_land_price_krw_m2: 5_000_000, price_base_year: 2026, as_of: "2026-09-05" }, undefined).map((q) => [q.as_of, q.price]), [["2026-09-05", 5_000_000]]);
  assert.deepEqual(priceTrend({ official_land_price_krw_m2: null, jimok_name: "도" }, [{ as_of: "2026-09-05", kind: "land_feature", first: true, changes: { jimok_name: { from: null, to: "도" } } }]), []);
  assert.deepEqual(priceTrend(null, []), []);
  // 기준일이 새로 들어왔지만 값이 같으면(변화 항목 없음) 점이 늘지 않는다. 0 원은 값이다
  const zero = priceTrend({ official_land_price_krw_m2: 0, price_base_year: 2026 }, [
    { as_of: "2026-06-01", kind: "land_feature", first: true, changes: { official_land_price_krw_m2: { from: null, to: 0 }, price_base_year: { from: null, to: 2026 } } },
  ]);
  assert.deepEqual(zero.map((q) => [q.price, q.deltaPct]), [[0, null]]);
});

test("공시지가 추이: 가상 VWorld 번들(이력 없음)은 필지마다 점 하나 이하", () => {
  for (const f of vwBundle().features) {
    const t = priceTrend(f.properties.attrs, f.properties.attrs_history);
    assert.ok(t.length <= 1, f.id);
  }
});
