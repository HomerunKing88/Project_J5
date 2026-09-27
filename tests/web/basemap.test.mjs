// basemap.js (J5-022, ADR-16): 배경 번들 검증, 선 가운데 점·각도, 도로명 라벨 선택. map.js 의 linePathD. DOM 없음.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateBasemap, partsOf, lineMidpoint, pickRoadLabels, labelBox, MAX_BASEMAP, LAYERS } from "../../web/app/basemap.js";
import { linePathD, mercator, PARCEL_LOCAL_K } from "../../web/app/map.js";

const FIX = new URL("../fixtures/basemap/synthetic.j5basemap.json", import.meta.url);
const doc = () => JSON.parse(readFileSync(FIX, "utf-8"));

test("가상 배경 번들은 유효하고 층별 수가 맞는다", () => {
  const d = doc();
  assert.deepEqual(validateBasemap(d), []);
  assert.deepEqual(d.counts, { building: 6, road_area: 2, road: 3 });
  assert.deepEqual(LAYERS, ["building", "road_area", "road"]);
  assert.equal(MAX_BASEMAP, 20000);
});

test("validateBasemap: 형식·층·이름 규칙 위반을 잡는다", () => {
  const bad = (mutate) => { const d = doc(); mutate(d); return validateBasemap(d); };
  assert.ok(bad((d) => { d.j5basemap = "2.0.0"; }).some((e) => e.includes("형식 버전")));
  assert.ok(bad((d) => { d.count = 5; }).some((e) => e.includes("count(5)")));
  assert.ok(bad((d) => { d.features[0].properties.name = "건물 이름"; }).some((e) => e.includes("도로 중심선만 이름")));
  assert.ok(bad((d) => { d.features[0].properties.floors = 3; }).some((e) => e.includes("허용되지 않음")));
  assert.ok(bad((d) => { d.features[0].geometry = { type: "LineString", coordinates: [[127, 37.5], [127.1, 37.5]] }; }).some((e) => e.includes("Polygon/MultiPolygon")));
  const road = doc().features.findIndex((f) => f.properties.layer === "road");
  assert.ok(bad((d) => { d.features[road].geometry = { type: "Polygon", coordinates: [[[127, 37.5], [127.1, 37.5], [127.1, 37.6], [127, 37.5]]] }; }).some((e) => e.includes("LineString")));
  assert.ok(bad((d) => { d.counts.road = 9; }).some((e) => e.includes("counts.road")));
  assert.ok(bad((d) => { d.sources[1] = { ...d.sources[0] }; }).some((e) => e.includes("두 번")));
  assert.ok(bad((d) => { d.sources.push({ ...d.sources[0] }); }).some((e) => e.includes("1~3개")));
  assert.ok(bad((d) => { d.extra = 1; }).some((e) => e.includes("허용되지 않은 필드")));
  assert.deepEqual(validateBasemap(null), ["객체가 아님"]);
});

test("partsOf·linePathD: 선은 M…L… 로, 닫지 않는다", () => {
  const f = doc().features.find((x) => x.geometry.type === "MultiLineString");
  const parts = partsOf(f);
  assert.equal(parts.length, 2);
  const origin = mercator(parts[0][0]);
  const d = linePathD(parts, origin, PARCEL_LOCAL_K);
  assert.ok(d.startsWith("M0.0 0.0L") && !d.includes("Z") && (d.match(/M/g) ?? []).length === 2, d);
});

test("lineMidpoint: 가장 긴 파트의 절반 지점과 -90~90 도 각도", () => {
  const parts = [[{ x: 0, y: 0 }, { x: 10, y: 0 }], [{ x: 0, y: 0 }, { x: 0, y: 2 }]];
  const m = lineMidpoint(parts);
  assert.deepEqual([m.x, m.y, m.angle, m.length], [5, 0, 0, 10]);
  const diag = lineMidpoint([[{ x: 0, y: 0 }, { x: -3, y: -3 }]]);
  assert.equal(Math.round(diag.angle), 45, "왼쪽 아래로 가는 선도 읽을 수 있는 방향(45도)으로");
  assert.equal(lineMidpoint([[{ x: 1, y: 1 }, { x: 1, y: 1 }]]), null);
  assert.equal(lineMidpoint([]), null);
});

test("pickRoadLabels: 같은 이름은 화면 안 가장 긴 것 하나, 짧으면 없음", () => {
  const view = { scale: 1000, tx: 0, ty: 0 };
  const roads = [
    { id: "road:1", name: "가상로", mid: { x: 0.1, y: 0.1, length: 0.1 } },   // 100px
    { id: "road:2", name: "가상로", mid: { x: 0.2, y: 0.1, length: 0.2 } },   // 200px → 선택
    { id: "road:3", name: "가상로", mid: { x: 5, y: 5, length: 1 } },         // 화면 밖
    { id: "road:4", name: "긴이름의도로명", mid: { x: 0.3, y: 0.1, length: 0.05 } }, // 50px < 7자·10px + 24
    { id: "road:5", name: null, mid: { x: 0.3, y: 0.1, length: 1 } },
  ];
  assert.deepEqual([...pickRoadLabels(roads, view, 400, 300)], ["road:2"]);
  assert.deepEqual([...pickRoadLabels(roads, { scale: 100, tx: 0, ty: 0 }, 400, 300)], [], "축소하면 다 짧아진다");
});

test("pickRoadLabels: 다른 이름끼리 라벨 사각형이 겹치면 긴 쪽만 남긴다", () => {
  const view = { scale: 1000, tx: 0, ty: 0 };
  const roads = [
    { id: "road:1", name: "가상로", mid: { x: 0.2, y: 0.1, angle: 90, length: 0.3 } },     // 세로 300px
    { id: "road:2", name: "가상1길", mid: { x: 0.2, y: 0.1, angle: 0, length: 0.2 } },    // 같은 자리 가로 200px → 겹쳐서 제외
    { id: "road:3", name: "가상2길", mid: { x: 0.2, y: 0.15, angle: 0, length: 0.2 } },   // 50px 아래: 세로 라벨(높이 30px)과 안 겹침
  ];
  assert.deepEqual([...pickRoadLabels(roads, view, 400, 300)].sort(), ["road:1", "road:3"]);
  const b = labelBox(100, 100, 30, 14, 90);
  assert.ok(Math.abs(b.x1 - b.x0 - 14) < 1e-9 && Math.abs(b.y1 - b.y0 - 30) < 1e-9, "90도 회전하면 폭·높이가 바뀐다");
});
