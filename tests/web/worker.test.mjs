// Cloudflare Worker 단위 시험 (J5-078). 실제 VWorld 를 부르지 않고 fetch 를 흉내 낸다. 가상 응답 시험이며 실제 VWorld·Cloudflare 확인이 아니다.
import test from "node:test";
import assert from "node:assert/strict";
import worker, { redact, findPnu, _resetProbe } from "../../worker/index.js";

const KEY = "ABCDEF12-3456-7890-ABCD-EF1234567890";
const env = (extra = {}) => ({ VWORLD_API_KEY: KEY, ASSETS: { fetch: async (req) => new Response("asset:" + new URL(req.url).pathname) }, ...extra });

function withFetch(handler, fn) {
  _resetProbe();
  const orig = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => { calls.push(String(url)); return handler(String(url), init); };
  return fn(calls).finally(() => { globalThis.fetch = orig; });
}

test("redact: 키 값과 key= 질의 값을 지운다", () => {
  assert.equal(redact(`https://x/?a=1&key=${KEY}&b=2`, KEY), "https://x/?a=1&key=<KEY>&b=2");
  assert.equal(redact(`/wmts/${KEY}/Base`, KEY), "/wmts/<KEY>/Base");
  assert.equal(redact("/x?key=zzz", ""), "/x?key=<KEY>");
});

test("findPnu: 응답 어디에 있든 19자리 pnu 를 찾는다", () => {
  assert.equal(findPnu({ response: { result: { featureCollection: { features: [{ properties: { pnu: "1111013800101820013" } }] } } } }), "1111013800101820013");
  assert.equal(findPnu({ PNU: "1111013800101820013" }), "1111013800101820013");
  assert.equal(findPnu({ pnu: "123" }), null);
  assert.equal(findPnu(null), null);
});

test("api 밖의 주소는 정적 자산으로, 모르는 api 는 404", async () => {
  const r = await worker.fetch(new Request("https://h.example/index.html"), env(), {});
  assert.equal(await r.text(), "asset:/index.html");
  const r2 = await worker.fetch(new Request("https://h.example/api/nope"), env(), {});
  assert.equal(r2.status, 404);
});

test("probe: 키 없으면 500, 응답·주소에 키가 남지 않는다, PNU 를 얻으면 토지 자료까지 부른다", async () => {
  const none = await worker.fetch(new Request("https://h.example/api/vworld/probe"), env({ VWORLD_API_KEY: "" }), {});
  assert.equal(none.status, 500);
  await withFetch((url) => {
    if (url.includes("LP_PA_CBND_BUBUN")) return new Response(JSON.stringify({ response: { status: "OK", echo: url, result: { featureCollection: { features: [{ properties: { pnu: "1111013800101820013" } }] } } } }), { headers: { "content-type": "application/json" } });
    if (url.includes("/wmts/")) return new Response(new Uint8Array(3000), { headers: { "content-type": "image/png" } });
    return new Response(`{"echo":"${url}"}`, { headers: { "content-type": "application/json" } });
  }, async (calls) => {
    const r = await worker.fetch(new Request("https://h.example/api/vworld/probe"), env(), {});
    assert.equal(r.status, 200);
    const text = await r.text();
    assert.ok(!text.includes(KEY), "응답에 키 없음");
    const out = JSON.parse(text);
    assert.equal(out.pnu, "1111013800101820013");
    assert.deepEqual(out.steps.map((s) => s.name), ["parcel_at_point", "zoning_at_point", "land_characteristics", "land_register", "land_price", "land_use_plan", "tile_base"]);
    assert.equal(out.domain_param, "https://h.example");
    const tile = out.steps.at(-1);
    assert.equal(tile.body, null, "타일 본문은 싣지 않는다");
    assert.ok(!tile.url.includes("?"), "타일 주소에 질의를 붙이지 않는다");
    assert.ok(calls.every((u) => u.startsWith("https://api.vworld.kr/")), "VWorld 에만 요청");
    assert.ok(calls[0].includes(`key=${encodeURIComponent(KEY)}`) || calls[0].includes(`key=${KEY}`), "요청에는 키를 싣는다");
  });
});

test("probe: 연결 실패도 키 없이 사유를 돌려준다, PNU 가 없으면 토지 자료는 부르지 않는다", async () => {
  await withFetch(() => { throw new Error(`connect failed https://api.vworld.kr/req/data?key=${KEY}`); }, async () => {
    const r = await worker.fetch(new Request("https://h.example/api/vworld/probe"), env(), {});
    const text = await r.text();
    assert.ok(!text.includes(KEY));
    const out = JSON.parse(text);
    assert.equal(out.pnu, null);
    assert.deepEqual(out.steps.map((s) => s.name), ["parcel_at_point", "zoning_at_point", "tile_base"]);
    assert.ok(out.steps.every((s) => s.status === null && s.error.includes("<KEY>")));
  });
});

test("probe: 동시에 여러 번 불려도 VWorld 는 한 번씩만 부르고, 끝난 결과는 다시 쓴다 (리뷰 반영 PR #125)", async () => {
  await withFetch(async (url) => {
    await new Promise((r) => setTimeout(r, 20));
    return new Response("{}", { headers: { "content-type": "application/json" } });
  }, async (calls) => {
    const rs = await Promise.all(Array.from({ length: 5 }, () => worker.fetch(new Request("https://h.example/api/vworld/probe"), env(), {})));
    assert.ok(rs.every((r) => r.status === 200));
    assert.equal(calls.length, 3, "PNU 없음: 필지·용도지역·타일 세 번뿐");
    const again = await (await worker.fetch(new Request("https://h.example/api/vworld/probe"), env(), {})).json();
    assert.equal(again.reused, true);
    assert.equal(calls.length, 3, "5분 안에는 다시 부르지 않는다");
  });
});
