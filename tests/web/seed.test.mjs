import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateSeed } from "../../web/app/seed.js";

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
