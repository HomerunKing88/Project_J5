import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  mercator, unmercator, clampScale, fitView, toScreen, toWorld, zoomAt, pan, metersPerPixel, scaleBar,
  locatedAssets, shortLabel, MIN_SCALE, MAX_SCALE, FIT_MAX_SCALE, MAX_LAT, SCALE_STEPS,
} from "../../web/app/map.js";

const seed = () => JSON.parse(readFileSync(new URL("../fixtures/assets.seed.synthetic.json", import.meta.url), "utf8"));
const near = (a, b, eps, msg) => assert.ok(Math.abs(a - b) <= eps, `${msg ?? ""} ${a} ≠ ${b} (±${eps})`);

test("mercator: 기준값·범위·단조", () => {
  assert.deepEqual(mercator([0, 0]), { x: 0.5, y: 0.5 });
  assert.equal(mercator([180, 0]).x, 1);
  assert.equal(mercator([-180, 0]).x, 0);
  near(mercator([0, MAX_LAT]).y, 0, 1e-6, "북쪽 한계");
  near(mercator([0, -MAX_LAT]).y, 1, 1e-6, "남쪽 한계");
  const pole = mercator([0, 90]);
  assert.ok(Number.isFinite(pole.y) && pole.y >= 0 && pole.y <= 1, "극점도 유한");
  const p = mercator([126.9986, 37.5702]);
  near(p.x, 0.85277, 1e-3, "종로 x");
  near(p.y, 0.3864, 1e-3, "종로 y");
  assert.ok(mercator([0, 38]).y < mercator([0, 37]).y, "위도가 커지면 y 가 작다 (북쪽이 위)");
  const [lon, lat] = unmercator(p);
  near(lon, 126.9986, 1e-9, "역투영 lon"); near(lat, 37.5702, 1e-9, "역투영 lat");
});

test("fitView: 모든 점이 여백 안, 중심 일치, 점 하나·범위 0·빈 배열", () => {
  const pts = locatedAssets(seed()).map((a) => mercator(a.location_point));
  assert.equal(pts.length, 4);
  const w = 320, h = 280, pad = 40;
  const v = fitView(pts, w, h, pad);
  assert.ok(Number.isFinite(v.scale) && v.scale <= FIT_MAX_SCALE && v.scale >= MIN_SCALE);
  const xs = [], ys = [];
  for (const p of pts) {
    const s = toScreen(p, v);
    assert.ok(s.x >= pad - 1e-6 && s.x <= w - pad + 1e-6, `x 여백 ${s.x}`);
    assert.ok(s.y >= pad - 1e-6 && s.y <= h - pad + 1e-6, `y 여백 ${s.y}`);
    xs.push(s.x); ys.push(s.y);
  }
  near((Math.min(...xs) + Math.max(...xs)) / 2, w / 2, 1e-6, "bbox 중심 x");
  near((Math.min(...ys) + Math.max(...ys)) / 2, h / 2, 1e-6, "bbox 중심 y");
  // 가상 시드는 폭 150m 정도라 기본 확대 상한에 걸린다. 더 넓은 범위(약 1km) 는 한 축이 여백에 닿는다.
  assert.equal(v.scale, FIT_MAX_SCALE, "좁은 범위는 FIT_MAX_SCALE 상한");
  const wide = [mercator([126.99, 37.57]), mercator([127.0, 37.575])];
  const vw = fitView(wide, w, h, pad);
  assert.ok(vw.scale < FIT_MAX_SCALE);
  const sw = wide.map((p) => toScreen(p, vw));
  near(Math.max(sw[0].x, sw[1].x) - Math.min(sw[0].x, sw[1].x), w - 2 * pad, 1e-6, "넓은 범위는 x 축이 여백에 닿는다");
  // 점 하나 → 기본 확대, 중심
  const one = fitView([pts[0]], w, h, pad);
  assert.equal(one.scale, FIT_MAX_SCALE);
  const s1 = toScreen(pts[0], one);
  near(s1.x, w / 2, 1e-6); near(s1.y, h / 2, 1e-6);
  // 극소 범위 → 유한, 상한 이하
  const tiny = fitView([pts[0], { x: pts[0].x + 1e-9, y: pts[0].y }], w, h, pad);
  assert.ok(Number.isFinite(tiny.scale) && tiny.scale <= FIT_MAX_SCALE);
  // 빈 배열 → NaN 없음
  const empty = fitView([], w, h, pad);
  assert.ok([empty.scale, empty.tx, empty.ty].every(Number.isFinite));
  assert.equal(empty.scale, MIN_SCALE);
  const c = toScreen({ x: 0.5, y: 0.5 }, empty);
  near(c.x, w / 2, 1e-9); near(c.y, h / 2, 1e-9);
});

test("zoomAt: 앵커 고정, 상·하한 클램프; pan; toWorld 역변환", () => {
  const v = fitView(locatedAssets(seed()).map((a) => mercator(a.location_point)), 320, 280);
  const anchor = { x: 100, y: 50 };
  const before = toWorld(anchor.x, anchor.y, v);
  const z = zoomAt(v, 2, anchor.x, anchor.y);
  near(z.scale, v.scale * 2, 1e-6);
  const after = toScreen(before, z);
  near(after.x, anchor.x, 1e-6, "앵커 x"); near(after.y, anchor.y, 1e-6, "앵커 y");
  assert.equal(zoomAt({ scale: MAX_SCALE, tx: 0, ty: 0 }, 2, 0, 0).scale, MAX_SCALE);
  assert.equal(zoomAt({ scale: MIN_SCALE, tx: 0, ty: 0 }, 0.5, 0, 0).scale, MIN_SCALE);
  assert.equal(clampScale(1), MIN_SCALE);
  assert.equal(clampScale(1e12), MAX_SCALE);
  assert.deepEqual(pan({ scale: 1, tx: 2, ty: 3 }, 4, -5), { scale: 1, tx: 6, ty: -2 });
  const w = toWorld(77, 33, v);
  const s = toScreen(w, v);
  near(s.x, 77, 1e-9); near(s.y, 33, 1e-9);
});

test("metersPerPixel·scaleBar", () => {
  near(metersPerPixel(0, { scale: 256 }), 156543, 1, "z0 적도");
  near(metersPerPixel(37.57, { scale: 2 ** 25 }), 0.947, 0.01, "z17 서울");
  for (const scale of [256, 2 ** 15, 2 ** 20, 2 ** 25, MAX_SCALE]) {
    const bar = scaleBar(37.57, { scale }, 120);
    assert.ok(bar.px <= 120 + 1e-9, `막대 ${bar.px}px`);
    assert.ok(SCALE_STEPS.includes(bar.meters));
    near(bar.px, bar.meters / metersPerPixel(37.57, { scale }), 1e-9);
  }
  assert.equal(scaleBar(37.57, { scale: MAX_SCALE }).meters, 2, "최대 확대(약 0.03 m/px)에서는 2 m 막대");
  assert.equal(scaleBar(37.57, { scale: FIT_MAX_SCALE }).meters, 100, "기본 확대(약 0.95 m/px)에서는 100 m 막대");
});

test("locatedAssets·shortLabel", () => {
  const s = seed();
  assert.equal(locatedAssets(s).length, 4);
  assert.deepEqual(locatedAssets([{ location_point: [1] }, { location_point: ["1", "2"] }, { location_point: [Infinity, 0] }, { address: "x" }]), []);
  assert.equal(shortLabel("가상 물건 1"), "가상 물건 1");
  const long = shortLabel("가나다라마바사아자차카타파", 12);
  assert.equal(Array.from(long).length, 13);
  assert.ok(long.endsWith("…"));
  assert.equal(shortLabel("😀😀😀", 2), "😀😀…", "코드 포인트 기준 절단");
});
