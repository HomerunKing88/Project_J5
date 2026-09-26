// ui.js 순수 함수 (J5-020): 해시 → 화면, 짧은 시각, 물건별 기록 요약, 다음 대상, 내보내기 단계. DOM 없음.
import test from "node:test";
import assert from "node:assert/strict";
import { viewFromHash, shortWhen, assetSummary, nextAsset, exportStep, VIEWS, DEFAULT_VIEW, MODE_LABEL } from "../../web/app/ui.js";

const ev = (asset, saved, status = "no_change", photos = 0, exported = false) => ({
  asset_id: asset, saved_at: saved, status: exported ? "exported" : "saved",
  event: { observed_at: saved, payload: { change_status: status }, attachment_refs: Array.from({ length: photos }, () => ({})) },
});

test("viewFromHash: 아는 화면만, 나머지는 기본 화면", () => {
  assert.equal(viewFromHash("#map"), "map");
  assert.equal(viewFromHash("records"), "records");
  assert.equal(viewFromHash("#nope"), DEFAULT_VIEW);
  assert.equal(viewFromHash(""), DEFAULT_VIEW);
  assert.deepEqual(VIEWS, ["home", "map", "records", "export", "settings"]);
  assert.equal(MODE_LABEL.private_real, "실제 자료 (비공개)");
});

test("shortWhen: 오늘은 시각만, 다른 날은 월·일 포함, 없으면 null", () => {
  const now = new Date(2026, 8, 26, 15, 0, 0);
  assert.equal(shortWhen(new Date(2026, 8, 26, 9, 5).toISOString(), now), "오늘 09:05");
  assert.equal(shortWhen(new Date(2026, 8, 25, 22, 40).toISOString(), now), "9월 25일 22:40");
  assert.equal(shortWhen(null, now), null);
  assert.equal(shortWhen("not-a-date", now), "not-a-date");
});

test("assetSummary: 마지막 기록·건수·사진·미내보냄", () => {
  const events = [ev("a", "2026-09-26T01:00:00Z", "change_observed", 2), ev("a", "2026-09-26T03:00:00Z", "no_change", 1, true), ev("b", "2026-09-26T02:00:00Z")];
  const s = assetSummary("a", events);
  assert.deepEqual([s.count, s.photos, s.unexported, s.lastStatus, s.lastAt], [2, 3, 1, "no_change", "2026-09-26T03:00:00Z"]);
  const none = assetSummary("zzz", events);
  assert.deepEqual([none.count, none.lastStatus, none.lastAt], [0, null, null]);
});

test("nextAsset: 기록 없는 다음 물건 우선, 없으면 다음 물건, 하나뿐이면 null", () => {
  const assets = [{ asset_id: "a" }, { asset_id: "b" }, { asset_id: "c" }];
  assert.equal(nextAsset(assets, "a", [ev("b", "x")]).asset_id, "c", "b 는 기록이 있으니 c");
  assert.equal(nextAsset(assets, "c", [ev("a", "x")]).asset_id, "b", "끝에서는 앞으로 돈다");
  assert.equal(nextAsset(assets, "a", [ev("b", "x"), ev("c", "x")]).asset_id, "b", "모두 기록됐으면 다음 물건");
  assert.equal(nextAsset(assets, null, []).asset_id, "a", "현재 물건이 없으면 처음부터");
  assert.equal(nextAsset(assets, null, [ev("a", "x")]).asset_id, "b", "처음부터 돌며 기록 없는 것");
  assert.equal(nextAsset([{ asset_id: "a" }], "a", []), null);
  assert.equal(nextAsset([], "a", []), null);
});

test("exportStep: 파일 없음 2 → 파일 있음 3 → 저장 시도 4", () => {
  assert.equal(exportStep({ hasPackage: false, attempted: false, done: false }), 2);
  assert.equal(exportStep({ hasPackage: true, attempted: false, done: false }), 3);
  assert.equal(exportStep({ hasPackage: true, attempted: true, done: false }), 4);
});
