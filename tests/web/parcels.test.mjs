// 필지 번들 검증·기하 (J5-013B-1). DOM 없이 가상 fixture 로 확인한다.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateParcels, labelPoint, pointInFeature, assetsInParcel, parcelTitle, fmtArea, polygonsOf, MAX_PARCELS } from "../../web/app/parcels.js";
import { worldBbox, parcelPathD, parcelLabelVisible, mercator, fitView, FIT_MAX_SCALE, PARCEL_LOCAL_K } from "../../web/app/map.js";

const bundle = () => JSON.parse(readFileSync(new URL("../fixtures/parcels/synthetic.j5parcels.json", import.meta.url), "utf8"));
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
