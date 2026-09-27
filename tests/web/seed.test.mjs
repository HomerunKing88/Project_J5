import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateSeed, filterAssets, hasTracking, isWatchlist, TRACKING_LABEL, TRACKING_STATUSES, WATCHLIST_STATUSES } from "../../web/app/seed.js";

const seed = () => JSON.parse(readFileSync(new URL("../fixtures/assets.seed.synthetic.json", import.meta.url), "utf8"));

test("가상 시드는 유효하다", () => {
  assert.deepEqual(validateSeed(seed()), []);
});

test("스키마와 같은 거절 규칙 (tests/test_j5_003_schemas.py 의 시드 거절 목록 포함)", () => {
  const cases = [
    [(s) => { delete s[0].location_point; }, "위치점 또는 주소"],
    [(s) => { s[0].asset_id = "not-a-uuid"; }, "UUID"],
    [(s) => { s[0].data_mode = "real"; }, "data_mode"],
    [(s) => { s[0].extra_field = 1; }, "허용되지 않은 필드"],
    [(s) => { delete s[0].notes; }, "notes 누락"],
    [(s) => { s[0].location_point = [37.57, 126.99, 0]; }, "location_point"],
    [(s) => { s[0].location_point = [200, 0]; }, "location_point"],
    [(s) => { s[0].location_point = ["126.9", "37.5"]; }, "location_point"],
    [(s) => { s[0].created_at = "2026-13-01"; }, "created_at"],
    [(s) => { s[0].created_at = "2026-09-22T00:00:00Z"; }, "created_at"],
    [(s) => { s[0].notes = 5; }, "notes"],
    [(s) => { s[1].asset_id = s[0].asset_id; }, "중복"],
    [(s) => { s[0].label = ""; }, "label"],
    [(s) => { s[2].address = ""; }, "address"],
  ];
  for (const [mutate, expect] of cases) {
    const s = seed();
    mutate(s);
    const errs = validateSeed(s);
    assert.ok(errs.some((m) => m.includes(expect)), `${expect}: ${errs}`);
  }
  assert.deepEqual(validateSeed([]), ["비어 있음"]);
  assert.deepEqual(validateSeed({}), ["배열이 아님"]);
  assert.ok(validateSeed([null])[0].includes("객체가 아님"));
});

test("관심 단계·확인 상태 (J5-029): 파생본 시드의 선택 필드, 허용값만, 관찰목록 필터", () => {
  const s = seed();
  s[0].tracking_status = "watch";
  s[1].tracking_status = "purchase_ready";
  s[1].resolution_status = "confirmed";
  s[3].tracking_status = "hold";
  s[4].resolution_status = "pending";
  assert.deepEqual(validateSeed(s), []);
  for (const st of TRACKING_STATUSES) assert.ok(TRACKING_LABEL[st], st);
  assert.deepEqual(WATCHLIST_STATUSES, ["watch", "detailed_review", "purchase_ready"]);
  assert.ok(hasTracking(s) && !hasTracking(seed()), "관심 단계가 하나라도 있으면 필터를 보인다");
  assert.deepEqual(filterAssets(s, "watchlist").map((a) => a.label), ["가상 물건 1", "가상 물건 2"]);
  assert.equal(filterAssets(s, "all").length, 5);
  assert.equal(filterAssets(seed(), "watchlist").length, 0, "관심 단계 없는 시드는 관찰목록에 없다");
  assert.ok(isWatchlist(s[0]) && !isWatchlist(s[3]) && !isWatchlist({}));
  const bad = seed();
  bad[0].tracking_status = "favorite";
  assert.ok(validateSeed(bad).some((m) => m.includes("tracking_status")));
  const bad2 = seed();
  bad2[0].resolution_status = "maybe";
  assert.ok(validateSeed(bad2).some((m) => m.includes("resolution_status")));
});
