// tiles.js (J5-024, ADR-18): 타일 설정 해석·템플릿 검사·확대 단계·화면 타일 목록·미리 받기 계획. DOM·통신 없음.
import test from "node:test";
import assert from "node:assert/strict";
import { KEY_IN_TEMPLATE_ERROR, hasKeyLike, PROVIDERS, TILE_HOSTS, resolveTileConfig, validateTemplate, maskUrl, tileUrl, tileZoom, tilesFor, tileRect, prefetchPlan, padBbox, PREFETCH_MAX_TILES } from "../../web/app/tiles.js";

test("resolveTileConfig: 미검증 제공자는 선택 불가, 키는 자리표로만, 템플릿 규칙", () => {
  assert.deepEqual(TILE_HOSTS, ["api.vworld.kr"]);
  const v = resolveTileConfig({ provider: "vworld_satellite", key: "abc" });
  assert.ok(v.errors && v.errors[0].includes("아직 고를 수 없다"), "확인하기 전에는 미리 설정을 쓸 수 없다");
  const b = resolveTileConfig({ provider: "vworld_base", key: "abc" });
  assert.equal(b.url, "https://api.vworld.kr/req/wmts/1.0.0/{key}/Base/{z}/{y}/{x}.png", "기본 지도: 키는 자리표 그대로");
  assert.equal(b.prefetch, false, "약관 확인 전이라 미리 받기 안 함 (리뷰 반영 PR #127)");
  assert.equal(resolveTileConfig({ provider: "custom", template: "./tiles/{z}/{x}/{y}.png" }).prefetch, true);
  assert.equal(resolveTileConfig({ provider: "vworld_base", key: "" }).errors[0].includes("인증키"), true, "키 없으면 거절");
  // J5-079: VWorld 기본 지도는 실기기에서 확인해 켰다. 항공사진 두 가지는 확인 전이라 고를 수 없다
  assert.deepEqual(Object.entries(PROVIDERS).filter(([, p]) => p.enabled).map(([id]) => id).sort(), ["custom", "vworld_base"]);
  assert.equal(PROVIDERS.vworld_base.verified, "2026-10-14");
  assert.ok(["vworld_satellite", "vworld_hybrid"].every((id) => PROVIDERS[id].verified === null && resolveTileConfig({ provider: id, key: "abc" }).errors));
  assert.ok(resolveTileConfig({ provider: "nope" }).errors.length);
  const c = resolveTileConfig({ provider: "custom", template: "./tiles/{z}/{x}/{y}.png" });
  assert.equal(c.url, "./tiles/{z}/{x}/{y}.png");
  assert.equal(c.needsKey, false);
  const k = resolveTileConfig({ provider: "custom", template: "https://api.vworld.kr/req/wmts/1.0.0/{key}/Base/{z}/{y}/{x}.png", key: "ab c" });
  assert.equal(k.url, "https://api.vworld.kr/req/wmts/1.0.0/{key}/Base/{z}/{y}/{x}.png", "URL 에 키를 넣지 않는다 (서비스 워커가 바꾼다)");
  assert.equal(k.needsKey, true);
  assert.ok(resolveTileConfig({ provider: "custom", template: "./t/{key}/{z}/{x}/{y}.png", key: " " }).errors[0].includes("인증키"));
  assert.ok(resolveTileConfig({ provider: "custom", template: "" }).errors[0].includes("템플릿"));
  assert.ok(resolveTileConfig({ provider: "custom", template: "https://tile.example.org/{z}/{x}/{y}.png" }).errors[0].includes("허용되지 않은 호스트"));
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
  // 인증키를 주소 칸에 넣은 경우 (J5-061, 실기기에서 있었던 실수): 키 자리에 키를 중괄호째 넣거나 그대로 넣으면 거절하고 인증키 칸을 안내한다
  const uuid = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D";   // 가상 값
  for (const t of [`https://api.vworld.kr/req/wmts/1.0.0/{${uuid}}/Base/{z}/{y}/{x}.png`, `https://api.vworld.kr/req/wmts/1.0.0/${uuid}/Base/{z}/{y}/{x}.png`,
    `./t/${"a1".repeat(16)}/{z}/{x}/{y}.png`, "./t/{zoom}/{x}/{y}.png", "./tiles/{z}/{x}/{y}/{token.png", "./tiles/{z}/{x}/{y}/token}.png", "./t/{{z}}/{x}/{y}.png"]) {
    assert.deepEqual(validateTemplate(t), [KEY_IN_TEMPLATE_ERROR], t);
  }
  assert.ok(KEY_IN_TEMPLATE_ERROR.includes("인증키 칸") && !KEY_IN_TEMPLATE_ERROR.includes(uuid));
  // 캐시 키(퍼센트 인코딩된 요청 주소)에서도 찾는다 (리뷰 반영 PR #108: 예전에 저장된 주소와 그 타일을 지운다). 자리표만 든 주소는 아니다
  assert.equal(hasKeyLike(`https://api.vworld.kr/req/wmts/1.0.0/%7B${uuid}%7D/Base/15/1/2.png`), true);
  assert.equal(hasKeyLike("https://api.vworld.kr/req/wmts/1.0.0/%7Bkey%7D/Base/15/12707/27943.png"), false);
  assert.equal(hasKeyLike("%E0%A4%A"), false, "잘못된 인코딩도 오류 없이");
  assert.deepEqual(resolveTileConfig({ provider: "custom", template: `https://api.vworld.kr/req/wmts/1.0.0/{${uuid}}/Base/{z}/{y}/{x}.png` }).errors, [KEY_IN_TEMPLATE_ERROR], "자리표가 없어 인증키를 묻기 전에 주소부터 바로잡는다");
  assert.equal(maskUrl("https://api.vworld.kr/req/k%2Fey/{z}", "k/ey"), "https://api.vworld.kr/req/****/{z}");
  assert.equal(tileUrl("./t/{key}/{z}/{x}/{y}.png", 1, 2, 3), "./t/{key}/1/2/3.png", "자리표는 남는다");
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

test("resolveTileConfig: 직접 입력에 넣은 VWorld 주소는 그 제공자의 확대 범위를 따른다 (J5-083)", () => {
  // 0.2.40 이전에는 VWorld 기본 지도를 직접 입력으로만 켤 수 있었다. 그 설정이 확대 상한 22 로 남아 z20 이상 타일(VWorld 에 없음)을 요청해 배경이 비었다
  const base = resolveTileConfig({ provider: "custom", template: "https://api.vworld.kr/req/wmts/1.0.0/{key}/Base/{z}/{y}/{x}.png", key: "abc" });
  assert.equal(base.provider, "vworld_base", "기본 지도 주소와 같으면 기본 지도로 본다 (주변 필지·찾기도 켜진다)");
  assert.equal(base.maxZoom, 19);
  assert.equal(base.attribution, PROVIDERS.vworld_base.attribution);
  assert.equal(base.prefetch, false);
  const upper = resolveTileConfig({ provider: "custom", template: "https://API.vworld.kr/req/wmts/1.0.0/{key}/Base/{z}/{y}/{x}.png", key: "abc" });
  assert.equal(upper.provider, "vworld_base", "호스트 대소문자는 가리지 않는다");
  const sat = resolveTileConfig({ provider: "custom", template: "https://api.vworld.kr/req/wmts/1.0.0/{key}/Satellite/{z}/{y}/{x}.jpeg", key: "abc" });
  assert.equal(sat.provider, "custom", "확인 전 제공자는 직접 입력으로 남긴다");
  assert.equal(sat.maxZoom, 19, "VWorld 호스트는 19 단계까지만 요청하고 그 위는 19 단계 타일을 늘려 그린다");
  assert.equal(resolveTileConfig({ provider: "custom", template: "./tiles/{z}/{x}/{y}.png" }).maxZoom, 22, "같은 출처 타일은 그대로");
  assert.equal(tileZoom(256 * 2 ** 21, { minZoom: base.minZoom, maxZoom: base.maxZoom, dpr: 2 }), 19);
});
