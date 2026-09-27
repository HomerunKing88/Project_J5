// view.js (J5-023, ADR-17): manifest·기록·거래 검증, 금액 표기, 지번 대조, 물건·필지 이력 조립, 연도 묶음. DOM 없음.
import test from "node:test";
import assert from "node:assert/strict";
import { validateViewManifest, parseRecordsJsonl, validateTransactionsDoc, fmtKrw, parseJibun, jibunMatch, assetHistory, parcelHistory, groupByYear, viewSummary } from "../../web/app/view.js";

const A0 = "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50", A1 = "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a51";
const R = (id, asset, type, at, payload, extra = {}) => ({ record_id: id, asset_id: asset, record_type: type, observed_at: at, payload, attachments: [], supersedes_id: null, ...extra });
const T = (id, ymd, jibun, extra = {}) => ({ transaction_id: id, deal_ymd: ymd, deal_date: `${ymd.slice(0, 4)}-${ymd.slice(4)}-01`, emd_name: "가상동", jibun, jibun_masked: jibun.includes("*"),
  amount_krw: 1e9, building_use: "근생", building_area_m2: 50.5, plottage_area_m2: null, share_deal: false, asset_id: null, link_status: "unlinked", zone: "core", scope: "unclear", ...extra });
const ID = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const manifest = () => ({ format: "j5view", projection_schema_version: "1.0.0", study_id: "s", data_mode: "synthetic", source_dataset_version: 8, generated_at: "2026-09-27T02:53:00Z",
  scope_ids: [A0], counts: { assets: 1, records: 0 }, files: [{ path: "assets.seed.json" }, { path: "assets.geojson" }, { path: "records.jsonl" }] });

test("validateViewManifest: 형식·버전·필수 파일·설정 대조", () => {
  assert.deepEqual(validateViewManifest(manifest(), { studyId: "s", dataMode: "synthetic" }), []);
  assert.ok(validateViewManifest({ ...manifest(), projection_schema_version: "9.0.0" }).some((e) => e.includes("모르는 파생본 형식")));
  assert.ok(validateViewManifest(manifest(), { studyId: "other" }).some((e) => e.includes("작업 공간")));
  assert.ok(validateViewManifest(manifest(), { dataMode: "private_real" }).some((e) => e.includes("자료 종류")));
  assert.ok(validateViewManifest({ ...manifest(), files: [] }).some((e) => e.includes("필수 파일")));
  assert.deepEqual(validateViewManifest(null), ["manifest 가 객체가 아님"]);
});

test("parseRecordsJsonl·validateTransactionsDoc", () => {
  const good = JSON.stringify(R(ID(1), A0, "field_observation", "2026-09-22", { change_status: "no_change" })) + "\n";
  assert.equal(parseRecordsJsonl(good + "\n").length, 1);
  assert.throws(() => parseRecordsJsonl(good + "{bad\n"), /JSON 이 아님/);
  assert.throws(() => parseRecordsJsonl(JSON.stringify({ record_id: "x" }) + "\n"), /필수 필드/);
  assert.deepEqual(validateTransactionsDoc({ j5transactions: "1.0.0", count: 1, transactions: [T(ID(2), "202508", "1")] }), []);
  assert.ok(validateTransactionsDoc({ j5transactions: "1.0.0", count: 2, transactions: [T(ID(2), "202508", "1")] }).some((e) => e.includes("count(2)")));
  assert.ok(validateTransactionsDoc({ j5transactions: "1.0.0", count: 1, transactions: [{ ...T(ID(2), "202508", "1"), amount_krw: -1 }] }).some((e) => e.includes("amount_krw")));
});

test("fmtKrw·parseJibun·jibunMatch", () => {
  assert.deepEqual([fmtKrw(1e9), fmtKrw(1.23e9), fmtKrw(15e8), fmtKrw(85e6), fmtKrw(9000), fmtKrw(null)], ["10억", "12.3억", "15억", "8,500만", "9,000원", "금액 미확인"]);
  assert.deepEqual(parseJibun("1-*"), { mountain: false, bon: 1, bu: null, masked: true, wholeMasked: false });
  assert.deepEqual(parseJibun("산1-2"), { mountain: true, bon: 1, bu: 2, masked: false, wholeMasked: false });
  assert.equal(parseJibun("*").wholeMasked, true);
  assert.equal(parseJibun("abc"), null);
  const p1 = { emd_name: "가상동", bon: 1, bu: 0, mountain: false }, p11 = { emd_name: "가상동", bon: 1, bu: 1, mountain: false }, s12 = { emd_name: "가상동", bon: 1, bu: 2, mountain: true };
  assert.equal(jibunMatch(T(ID(1), "202508", "1"), p1), "exact");
  assert.equal(jibunMatch(T(ID(1), "202508", "1"), p11), null, "부번이 다르면 아님");
  assert.equal(jibunMatch(T(ID(1), "202508", "1-*"), p1), "prefix");
  assert.equal(jibunMatch(T(ID(1), "202508", "1-*"), p11), "prefix");
  assert.equal(jibunMatch(T(ID(1), "202508", "1-*"), s12), null, "산 여부가 다르면 아님");
  assert.equal(jibunMatch(T(ID(1), "202508", "*"), p1), null, "동 전체 마스킹은 어느 필지에도 아님");
  assert.equal(jibunMatch(T(ID(1), "202508", "1", { emd_name: "다른동" }), p1), null);
});

test("assetHistory: 정본 기록·이 기기 관측(정본에 있으면 대체)·확정 연결 거래, 날짜 내림차순, 정정 표시", () => {
  const records = [R(ID(1), A0, "field_observation", "2026-09-22T10:15:00+09:00", { change_status: "change_observed", note: "광고" }),
    R(ID(3), A0, "target_price", "2026-09-20", { price_kind: "target_buy", price_krw: 15e8, strategy: "보유" }),
    R(ID(4), A0, "target_price", "2026-09-25", { price_kind: "target_buy", price_krw: 16e8 }, { supersedes_id: ID(3) })];
  const events = [{ event_id: ID(1), asset_id: A0, status: "exported", event: { observed_at: "2026-09-22T10:15:00+09:00", payload: { change_status: "change_observed" }, attachment_refs: [] } },
    { event_id: ID(9), asset_id: A0, status: "saved", event: { observed_at: "2026-09-26T09:00:00+09:00", payload: { change_status: "no_change", note: "그대로" }, attachment_refs: [{}, {}] } },
    { event_id: ID(8), asset_id: A1, status: "saved", event: { observed_at: "2026-09-26", payload: { change_status: "no_change" }, attachment_refs: [] } }];
  const transactions = [T(ID(5), "202508", "1", { asset_id: A0, link_status: "confirmed" }), T(ID(6), "202608", "1-1")];
  const items = assetHistory(A0, { events, records, transactions });
  assert.deepEqual(items.map((i) => [i.date, i.kind, i.source]), [["2026-09-26", "field_observation", "device"], ["2026-09-25", "target_price", "pc"], ["2026-09-22", "field_observation", "pc"], ["2026-09-20", "target_price", "pc"], ["2025-08-01", "transaction", "pc"]]);
  assert.equal(items.filter((i) => i.id === ID(1)).length, 1, "정본에 있는 관측은 한 번만");
  assert.equal(items.find((i) => i.id === ID(3)).superseded, true);
  assert.equal(items.find((i) => i.id === ID(9)).text, "변화 없음 · 그대로 · 사진 2장");
  assert.equal(items.find((i) => i.id === ID(5)).match, "linked");
  assert.ok(items.find((i) => i.id === ID(4)).text.startsWith("목표 매수가 16억"));
});

test("parcelHistory·groupByYear: 지번 일치·번지대 거래 + 안의 물건 이력, 연도 내림차순 묶음", () => {
  const feature = { id: "9999900100100010000", properties: { emd_name: "가상동", label: "1", bon: 1, bu: 0, mountain: false } };
  const transactions = [T(ID(5), "202508", "1", { asset_id: A0, link_status: "confirmed" }), T(ID(6), "202508", "1-*"), T(ID(7), "202608", "*"), T(ID(8), "202608", "1-1"), T(ID(10), "202408", "1")];
  const records = [R(ID(1), A0, "field_observation", "2026-09-22", { change_status: "no_change" })];
  const items = parcelHistory(feature, [{ asset_id: A0, label: "가상 물건 1" }], { events: [], records, transactions });
  assert.deepEqual(items.map((i) => [i.id, i.match ?? i.kind]), [[ID(1), "field_observation"], [ID(6), "prefix"], [ID(5), "exact"], [ID(10), "exact"]]);
  assert.equal(items.filter((i) => i.id === ID(5)).length, 1, "필지 거래와 물건 연결 거래가 같은 건이면 한 번만");
  const groups = groupByYear(items);
  assert.deepEqual(groups.map((g) => [g.year, g.items.length]), [["2026", 1], ["2025", 2], ["2024", 1]]);
  assert.deepEqual(groupByYear([{ year: "", date: "", id: "x" }]).map((g) => g.year), ["날짜 미상"]);
  const sm = viewSummary({ manifest: manifest(), records, transactions: { transactions }, photos_skipped: 2, source: "view:a.zip", loaded_at: "2026-09-27T03:00:00Z" });
  assert.deepEqual([sm.version, sm.records, sm.transactions, sm.photosSkipped], [8, 1, 5, 2]);
  assert.equal(viewSummary(null), null);
});
