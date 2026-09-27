// extmap.js (J5-021, ADR-15): 외부 지도 링크는 좌표와 고정 문구만 담고, 잘못된 좌표에는 링크를 만들지 않는다. DOM 없음.
import test from "node:test";
import assert from "node:assert/strict";
import { EXTERNAL_MAPS, externalMapLinks, roundPoint, bboxCenter, LINK_ATTRS } from "../../web/app/extmap.js";

test("roundPoint: [경도, 위도] 를 소수 6자리 문자열로, 범위·형식 밖은 null", () => {
  assert.deepEqual(roundPoint([126.99861234, 37.57021999]), { lat: "37.570220", lng: "126.998612" });
  assert.equal(roundPoint([181, 37]), null);
  assert.equal(roundPoint([127, 91]), null);
  assert.equal(roundPoint([NaN, 37]), null);
  assert.equal(roundPoint(["127", "37"]), null);
  assert.equal(roundPoint(null), null);
  assert.equal(roundPoint([127]), null);
});

test("bboxCenter: 가운데 점, 형식 밖은 null", () => {
  assert.deepEqual(bboxCenter([126.9, 37.5, 127.1, 37.7]), [127, 37.6]);
  assert.equal(bboxCenter([126.9, 37.5, 127.1]), null);
  assert.equal(bboxCenter(undefined), null);
});

test("externalMapLinks: 지도마다 https 링크 하나, 좌표만 담고 이름·지번은 없음", () => {
  const links = externalMapLinks([126.998612, 37.570220]);
  assert.equal(links.length, EXTERNAL_MAPS.length);
  assert.deepEqual(links.map((l) => l.id), ["apple", "naver", "kakao", "google"]);
  for (const l of links) {
    const u = new URL(l.href);
    assert.equal(u.protocol, "https:");
    assert.ok(l.href.includes("37.570220") && l.href.includes("126.998612"), l.href);
    assert.equal(u.hostname, EXTERNAL_MAPS.find((m) => m.id === l.id).host);
    assert.ok(!/가상|물건|pnu|asset/i.test(decodeURIComponent(l.href)), l.href);
  }
  assert.equal(links[0].href, "https://maps.apple.com/?ll=37.570220,126.998612&q=%EC%84%A0%ED%83%9D%20%EC%9C%84%EC%B9%98&z=17");
  assert.equal(links[0].verified, "2026-09-27");
  assert.equal(links[1].verified, null, "네이버 형식은 공식 문서로 확인하지 못했다");
});

test("externalMapLinks: 좌표 없음·잘못됨 → 빈 배열", () => {
  assert.deepEqual(externalMapLinks(null), []);
  assert.deepEqual(externalMapLinks([200, 0]), []);
});

test("LINK_ATTRS: 새 창, opener 차단, 참조 주소 미전달", () => {
  assert.equal(LINK_ATTRS.target, "_blank");
  assert.ok(LINK_ATTRS.rel.split(" ").includes("noopener") && LINK_ATTRS.rel.split(" ").includes("noreferrer"));
  assert.equal(LINK_ATTRS.referrerPolicy, "no-referrer");
});
