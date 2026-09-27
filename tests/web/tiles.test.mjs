// tiles.js (J5-024, ADR-18): 타일 설정 해석·템플릿 검사·확대 단계·화면 타일 목록·미리 받기 계획. DOM·통신 없음.
import test from "node:test";
import assert from "node:assert/strict";
import { PROVIDERS, TILE_HOSTS, resolveTileConfig, validateTemplate, maskUrl, tileUrl, tileZoom, tilesFor, tileRect, prefetchPlan, padBbox, PREFETCH_MAX_TILES } from "../../web/app/tiles.js";

test("resolveTileConfig: 제공자·키·템플릿 규칙", () => {
  assert.deepEqual(TILE_HOSTS, ["api.vworld.kr"]);
  const v = resolveTileConfig({ provider: "vworld_base", key: "ab c/=1" });
  assert.equal(v.url, "https://api.vworld.kr/req/wmts/1.0.0/ab%20c%2F%3D1/Base/{z}/{y}/{x}.png");
  assert.deepEqual([v.minZoom, v.maxZoom, v.attribution], [6, 19, "배경: VWorld (국토교통부)"]);
  assert.ok(resolveTileConfig({ provider: "vworld_base", key: " " }).errors[0].includes("인증키"));
  assert.ok(resolveTileConfig({ provider: "nope" }).errors.length);
  const c = resolveTileConfig({ provider: "custom", template: "./tiles/{z}/{x}/{y}.png" });
  assert.equal(c.url, "./tiles/{z}/{x}/{y}.png");
  assert.ok(resolveTileConfig({ provider: "custom", template: "" }).errors[0].includes("템플릿"));
  assert.ok(resolveTileConfig({ provider: "custom", template: "https://tile.example.org/{z}/{x}/{y}.png" }).errors[0].includes("허용되지 않은 호스트"));
  assert.ok(Object.values(PROVIDERS).every((p) => p.verified === null), "형식은 아직 공식 문서로 확인하지 못했다");
});

test("validateTemplate: 자리표·경로 탈출·스킴", () => {
  assert.deepEqual(validateTemplate("./t/{z}/{x}/{y}.png"), []);
  assert.deepEqual(validateTemplate("https://api.vworld.kr/x/{z}/{y}/{x}.png"), []);
  assert.ok(validateTemplate("./t/{z}/{x}.png").some((e) => e.includes("{y}")));
  assert.ok(validateTemplate("./t/../{z}/{x}/{y}.png").some((e) => e.includes("상위 경로")));
  assert.ok(validateTemplate("t/{z}/{x}/{y}.png").some((e) => e.includes("같은 출처")));
  assert.ok(validateTemplate("//evil/{z}/{x}/{y}.png").some((e) => e.includes("//")));
  assert.ok(validateTemplate("http://api.vworld.kr/{z}/{x}/{y}.png").some((e) => e.includes("https://")));
  assert.ok(validateTemplate("./t/{z}/{x}/{y}.png\n").some((e) => e.includes("제어문자")));
  assert.equal(maskUrl("https://api.vworld.kr/req/k%2Fey/{z}", "k/ey"), "https://api.vworld.kr/req/****/{z}");
  assert.equal(tileUrl("./t/{z}/{x}/{y}.png", 15, 27943, 12707), "./t/15/27943/12707.png");
});

test("tileZoom·tilesFor·tileRect: 축척에 맞는 단계와 화면을 덮는 타일", () => {
  assert.equal(tileZoom(256 * 2 ** 15, { minZoom: 6, maxZoom: 19 }), 15);
  assert.equal(tileZoom(256 * 2 ** 15 * 1.5, { minZoom: 6, maxZoom: 19 }), 15, "타일 하나가 256~512px 이면 같은 단계");
  assert.equal(tileZoom(256 * 2 ** 15, { minZoom: 6, maxZoom: 19, dpr: 2 }), 16, "고해상도는 한 단계 위");
  assert.equal(tileZoom(256 * 2 ** 3, { minZoom: 6, maxZoom: 19 }), 6);
  assert.equal(tileZoom(256 * 2 ** 21, { minZoom: 6, maxZoom: 19 }), 19);
  const view = { scale: 256 * 2 ** 2, tx: -100, ty: -50 }; // z=2: 타일 256px, 4×4 세계
  const list = tilesFor(view, 390, 300, 2, 0);
  assert.deepEqual(list.map((t) => t.key), ["2/0/0", "2/1/0", "2/0/1", "2/1/1"]);
  const r = tileRect({ z: 2, x: 1, y: 1 }, view);
  assert.deepEqual([r.x, r.y, r.size], [156, 206, 256]);
  assert.ok(tilesFor({ scale: 256 * 2 ** 2, tx: 5000, ty: 5000 }, 390, 300, 2, 0).length === 0, "세계 밖은 없음");
  assert.equal(tilesFor(view, 390, 300, 2, 1).length, 9, "여분 1 이면 3×3 (경계 안에서)");
});

test("prefetchPlan·padBbox: 범위·단계·상한", () => {
  const bbox = [126.998, 37.5698, 127.001, 37.5716];
  const plan = prefetchPlan(bbox, { minZoom: 14, maxZoom: 16 });
  assert.ok(plan.count > 0 && plan.count === plan.tiles.length && !plan.tooMany);
  assert.ok(plan.tiles.every((t) => t.z >= 14 && t.z <= 16) && plan.tiles[0].z === 14);
  const big = prefetchPlan([126, 37, 128, 38], { minZoom: 14, maxZoom: 18 });
  assert.ok(big.tooMany && big.tiles.length === 0 && big.cap === PREFETCH_MAX_TILES);
  assert.deepEqual(prefetchPlan(null), { count: 0, tiles: [], tooMany: false });
  const padded = padBbox(bbox, 300);
  assert.ok(padded[0] < bbox[0] && padded[1] < bbox[1] && padded[2] > bbox[2] && padded[3] > bbox[3]);
  assert.ok(Math.abs((padded[3] - bbox[3]) * 111320 - 300) < 1);
});
