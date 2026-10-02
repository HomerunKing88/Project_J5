// view.js (J5-023, ADR-17): manifest·기록·거래 검증, 금액 표기, 지번 대조, 물건·필지 이력 조립, 연도 묶음. DOM 없음.
import test from "node:test";
import assert from "node:assert/strict";
import { validateViewManifest, parseRecordsJsonl, validateTransactionsDoc, checkProjectionConsistency, fmtKrw, parseJibun, jibunMatch, assetHistory, parcelHistory, attrHistoryItems, groupByYear, viewSummary, parcelYearSummary, parcelYearSummaryInfo, YEAR_SUMMARY_MAX, yearSummaryRow, yearSummaryCells } from "../../web/app/view.js";

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

test("checkProjectionConsistency: 거래·필지 파일의 정본 버전·생성 시각·작업 공간이 manifest 와 같아야 한다", () => {
  const m = manifest();
  const tx = { j5transactions: "1.0.0", study_id: "s", data_mode: "synthetic", source_dataset_version: 8, generated_at: m.generated_at, count: 0, transactions: [] };
  const pc = { study_id: "s", source_dataset_version: 8, generated_at: m.generated_at };
  assert.deepEqual(checkProjectionConsistency(m, { transactions: tx, parcels: pc }), []);
  assert.deepEqual(checkProjectionConsistency(m, {}), []);
  assert.ok(checkProjectionConsistency(m, { transactions: { ...tx, source_dataset_version: 7 } })[0].includes("transactions.json 의 source_dataset_version(7)"));
  assert.ok(checkProjectionConsistency(m, { transactions: { ...tx, study_id: "other" } })[0].includes("study_id"));
  assert.ok(checkProjectionConsistency(m, { transactions: { ...tx, data_mode: "private_real" } })[0].includes("data_mode"));
  assert.ok(checkProjectionConsistency(m, { parcels: { ...pc, generated_at: "2020-01-01T00:00:00Z" } })[0].includes("parcels.geojson 의 generated_at"));
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
  assert.deepEqual(parseJibun("1-*"), { mountain: false, masked: true, bonPrefix: "1", bonDigits: 1, buPrefix: "", buDigits: 1, wholeMasked: false });
  assert.deepEqual(parseJibun("산1-2"), { mountain: true, masked: false, bon: 1, bu: 2 });
  assert.deepEqual(parseJibun("1**"), { mountain: false, masked: true, bonPrefix: "1", bonDigits: 3, buPrefix: "0", buDigits: null, wholeMasked: false }, "실제 표기: 자릿수를 별표로 가린다 (J5-043)");
  assert.equal(parseJibun("*").wholeMasked, true);
  assert.equal(parseJibun("abc"), null);
  assert.equal(parseJibun(""), null);
  const p1 = { emd_name: "가상동", bon: 1, bu: 0, mountain: false }, p11 = { emd_name: "가상동", bon: 1, bu: 1, mountain: false }, s12 = { emd_name: "가상동", bon: 1, bu: 2, mountain: true };
  assert.equal(jibunMatch(T(ID(1), "202508", "1"), p1), "exact");
  assert.equal(jibunMatch(T(ID(1), "202508", "1"), p11), null, "부번이 다르면 아님");
  assert.equal(jibunMatch(T(ID(1), "202508", "1-*"), p1), "prefix");
  assert.equal(jibunMatch(T(ID(1), "202508", "1-*"), p11), "prefix");
  assert.equal(jibunMatch(T(ID(1), "202508", "1-*"), s12), null, "산 여부가 다르면 아님");
  assert.equal(jibunMatch(T(ID(1), "202508", "*"), p1), null, "동 전체 마스킹은 어느 필지에도 아님");
  assert.equal(jibunMatch(T(ID(1), "202508", "1", { emd_name: "다른동" }), p1), null);
  // 실제 마스킹 표기 (J5-014 실측: "1**", "8*", "1**-*", "16*-3"). PC txlinks.jibun_matches 와 같은 결과 (J5-043)
  const P = (bon, bu = 0, mountain = false) => ({ emd_name: "가상동", bon, bu, mountain });
  const cases = [["1**", P(160), "prefix"], ["1**", P(160, 3), null], ["1**", P(16), null], ["1**-*", P(160, 3), "prefix"], ["16*-3", P(160, 3), "prefix"],
    ["16*-3", P(160, 4), null], ["8*", P(85), "prefix"], ["8*", P(8), null], ["1-*", P(1, 12), null], ["산1**", P(123, 0, true), "prefix"], ["산1**", P(123), null], ["*-1", P(1, 1), null]];
  for (const [j, p, want] of cases) assert.equal(jibunMatch(T(ID(1), "202508", j), p), want, `${j} ↔ ${p.mountain ? "산" : ""}${p.bon}-${p.bu}`);
  // 시군구 코드 (J5-044): 거래 lawd_cd 와 필지 emd_code 앞 5자리가 다르면 이름·지번이 같아도 아님. 한쪽이 없으면 이름으로만 본다
  const pc = { ...p1, emd_code: "9999900100" };
  assert.equal(jibunMatch(T(ID(1), "202508", "1", { lawd_cd: "99999" }), pc), "exact");
  assert.equal(jibunMatch(T(ID(1), "202508", "1", { lawd_cd: "11110" }), pc), null, "이름이 같은 다른 시군구의 법정동");
  assert.equal(jibunMatch(T(ID(1), "202508", "1-*", { lawd_cd: "11110" }), pc), null);
  assert.equal(jibunMatch(T(ID(1), "202508", "1", { lawd_cd: "11110" }), p1), "exact", "필지에 법정동 코드가 없으면 이름으로만");
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
  assert.equal(items.find((i) => i.id === ID(9)).text, "그대로 · 그대로 · 사진 2장");
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

test("attrHistoryItems·parcelHistory: 필지 속성 변화가 기준일 연도에 토지 자료 항목으로 든다 (J5-026)", () => {
  const feature = { id: "9999900100100010000", properties: { emd_name: "가상동", label: "1", bon: 1, bu: 0, mountain: false,
    attrs_history: [{ as_of: "2024-09-05", kind: "land_feature", first: true, changes: { jimok_name: { from: null, to: "대" }, official_land_price_krw_m2: { from: null, to: 12340000 } } },
      { as_of: "2024-09-05", kind: "land_ownership", first: true, changes: { ownership_kind: { from: null, to: "개인" } } },
      { as_of: "2026-01-15", kind: "land_feature", first: false, changes: { official_land_price_krw_m2: { from: 12340000, to: 13000000 } } },
      { as_of: "2026-01-15", kind: "land_plan", first: false, changes: {} }] } };
  const items = attrHistoryItems(feature);
  assert.deepEqual(items.map((i) => [i.date, i.kindLabel, i.first, i.text]), [["2024-09-05", "토지특성", true, "지목 대 · 공시지가 12,340,000원/㎡"], ["2024-09-05", "토지소유", true, "소유구분 개인"], ["2026-01-15", "토지특성", false, "공시지가 12,340,000원/㎡ → 13,000,000원/㎡"]], "빈 변화는 항목이 없다");
  assert.ok(items.every((i) => i.kind === "land_attrs" && i.source === "pc" && !i.superseded));
  const transactions = [T(ID(5), "202508", "1")];
  const all = parcelHistory(feature, [], { events: [], records: [], transactions });
  assert.deepEqual(all.map((i) => i.date), ["2026-01-15", "2025-08-01", "2024-09-05", "2024-09-05"]);
  assert.deepEqual(groupByYear(all).map((g) => [g.year, g.items.length]), [["2026", 1], ["2025", 1], ["2024", 2]]);
  assert.deepEqual(attrHistoryItems({ id: "x", properties: {} }), []);
});

test("parcelYearSummary·yearSummaryRow: 연도별 요약, 0 건과 미수집·실패·범위 밖·모름 구분 (J5-047)", () => {
  const feature = { id: "9999900100100010000", properties: { emd_name: "가상동", emd_code: "9999900100", label: "1", bon: 1, bu: 0, mountain: false } };
  const tx = (id, ymd, jibun) => T(ID(id), ymd, jibun, { lawd_cd: "99999" });
  const txDoc = { j5transactions: "1.0.0", count: 3, transactions: [tx(1, "202608", "1"), tx(2, "202608", "1-*"), tx(3, "202108", "1")],
    zone_rule: { version: 1, core: ["가상동"], comparison: [] },
    coverage: [{ lawd_cd: "99999", year: 2026, months_complete: 8, months_any: 8 }, { lawd_cd: "99999", year: 2025, months_complete: 12, months_any: 12 },
      { lawd_cd: "99999", year: 2023, months_complete: 0, months_any: 2 }, { lawd_cd: "99999", year: 2021, months_complete: 1, months_any: 1 }, { lawd_cd: "11110", year: 2024, months_complete: 12, months_any: 12 }] };
  assert.deepEqual(validateTransactionsDoc(txDoc), []);
  const items = parcelHistory(feature, [], { events: [], records: [], transactions: txDoc.transactions });
  const rows = parcelYearSummary(feature, items, txDoc, [{ year: 2025, price: 12e6 }, { year: 2026, price: 12.6e6 }]);
  assert.deepEqual(rows.map((r) => [r.year, r.tx, r.exact, r.prefix, r.monthsComplete]), [
    [2026, "counted", 1, 1, 8], [2025, "counted", 0, 0, 12], [2024, "not_collected", null, null, 0], [2023, "incomplete", null, null, 0], [2022, "not_collected", null, null, 0], [2021, "counted", 1, 0, 1]],
    "수집했고 거래 없는 해는 0, 수집 안 한 해·실패만 있는 해는 모름. 다른 시군구(11110)의 수집 현황은 쓰지 않는다");
  assert.equal(rows[0].deltaPct.toFixed(1), "5.0");
  assert.deepEqual(yearSummaryRow(rows[0]), ["2026", "공시지가 12,600,000원/㎡ (+5.0%) · 같은 필지 1 · 번지대 1 (수집 8/12개월)"]);
  assert.deepEqual(yearSummaryRow(rows[2]), ["2024", "공시지가 자료 없음 · 거래 미수집"]);
  assert.equal(yearSummaryRow(rows[3])[1], "공시지가 자료 없음 · 거래 수집 실패·부분만");
  // 범위 밖 동, 거래 파일 없음, 수집 현황 없는 이전 형식
  const out = parcelYearSummary({ ...feature, properties: { ...feature.properties, emd_name: "가상2동" } }, [], txDoc, [{ year: 2026, price: 1 }]);
  assert.equal(out.find((r) => r.year === 2026).tx, "outside");
  assert.deepEqual(parcelYearSummary(feature, [], null, [{ year: 2026, price: 1 }]).map((r) => r.tx), ["unknown"]);
  const old = { ...txDoc, coverage: undefined, zone_rule: undefined };
  assert.deepEqual(parcelYearSummary(feature, [], old, [{ year: 2026, price: 1 }]).map((r) => [r.tx, r.monthsComplete]), [["unknown", null]]);
  assert.deepEqual(parcelYearSummary(feature, [], txDoc, []).length, 6, "공시지가가 없어도 수집 현황이 있는 해는 보인다");
  assert.equal(parcelYearSummary(feature, [], txDoc, [], { maxYears: 2 }).length, 2, "최근 연도만");
  assert.deepEqual(parcelYearSummary(feature, [], { ...txDoc, coverage: [] }, []), [], "자료가 없으면 빈 요약");
  // 형식 검사
  assert.ok(validateTransactionsDoc({ ...txDoc, coverage: [{ lawd_cd: "999", year: 2026, months_complete: 1, months_any: 1 }] }).some((e) => e.includes("coverage")));
  assert.ok(validateTransactionsDoc({ ...txDoc, coverage: [{ lawd_cd: "99999", year: 2026, months_complete: 3, months_any: 2 }] }).some((e) => e.includes("coverage")));
  assert.ok(validateTransactionsDoc({ ...txDoc, coverage: [txDoc.coverage[0], txDoc.coverage[0]] }).some((e) => e.includes("coverage")), "중복");
  assert.ok(validateTransactionsDoc({ ...txDoc, zone_rule: { version: 1, core: "가상동", comparison: [] } }).some((e) => e.includes("zone_rule")));
  // 범위 규칙의 시군구 (J5-048): 규칙 시군구가 필지 시군구와 다르면 이름이 같아도 범위 밖
  const zrSgg = (lawd) => ({ ...txDoc, zone_rule: { ...txDoc.zone_rule, lawd_cd: lawd } });
  assert.deepEqual(validateTransactionsDoc(zrSgg("99999")), []);
  assert.deepEqual(validateTransactionsDoc(zrSgg(null)), []);
  assert.ok(validateTransactionsDoc(zrSgg("9999")).some((e) => e.includes("zone_rule")));
  assert.equal(parcelYearSummary(feature, [], zrSgg("11110"), [{ year: 2026, price: 1 }]).find((r) => r.year === 2026).tx, "outside");
  assert.equal(parcelYearSummary(feature, [], zrSgg("99999"), [{ year: 2026, price: 1 }]).find((r) => r.year === 2026).tx, "counted");
});

test("parcelYearSummary·yearSummaryRow: 연도마다 소유 변동일, 소유 자료 없음은 모름 (J5-053)", () => {
  const feature = { id: "9999900100100010000", properties: { emd_name: "가상동", emd_code: "9999900100", label: "1", bon: 1, bu: 0, mountain: false } };
  const prices = [{ year: 2026, price: 1 }, { year: 2027, price: 2 }];
  const rows = parcelYearSummary(feature, [], null, prices, { ownership: { known: true, dates: ["2017-01-01", "2027-09-01", "2027-11-02"] } });
  assert.deepEqual(rows.map((r) => [r.year, r.owner.dates]), [[2027, ["2027-09-01", "2027-11-02"]], [2026, []]], "범위 안의 날짜만, 오래된 변동일은 범위를 넓히지 않는다");
  assert.equal(yearSummaryRow(rows[0])[1], "공시지가 2원/㎡ (+100.0%) · 거래 수집 현황 모름 · 소유 변동 2027-09-01, 2027-11-02");
  assert.equal(yearSummaryRow(rows[1])[1], "공시지가 1원/㎡ · 거래 수집 현황 모름", "변동이 없는 해는 덧붙이지 않는다");
  const none = parcelYearSummary(feature, [], null, prices, { ownership: { known: false, dates: [] } });
  assert.equal(yearSummaryRow(none[0])[1], "공시지가 2원/㎡ (+100.0%) · 거래 수집 현황 모름 · 소유 자료 없음", "PC years_text 와 같은 글");
  assert.equal(parcelYearSummary(feature, [], null, prices)[0].owner, null, "주지 않으면 소유 칸 없음");
  // 끝 연도는 PC 처럼 올해(폰은 PC 자료 파일을 만든 해)까지: 공시지가가 2025 까지여도 2026 소유 변동이 보인다 (J5-053 리뷰)
  const own26 = { known: true, dates: ["2017-01-01", "2026-09-01"] };
  const upTo = parcelYearSummary(feature, [], null, [{ year: 2025, price: 1 }], { ownership: own26, thisYear: 2026 });
  assert.deepEqual(upTo.map((r) => [r.year, r.price, r.owner.dates]), [[2026, null, ["2026-09-01"]], [2025, 1, []]]);
  assert.deepEqual(parcelYearSummary(feature, [], null, [], { ownership: own26, thisYear: 2026 }).map((r) => r.year), [2026], "다른 자료가 없어도 그해 소유 변동은 보인다");
  assert.deepEqual(parcelYearSummary(feature, [], null, [], { ownership: { known: true, dates: ["2017-01-01"] }, thisYear: 2026 }), [], "오래된 변동일만 있으면 빈 요약");
  assert.equal(parcelYearSummary(feature, [], null, [{ year: 2025, price: 1 }], { thisYear: 2026 }).length, 2, "올해 줄은 소유 자료와 무관하게 생긴다 (PC 와 같다)");
});

test("parcelYearSummaryInfo: 연도 상한은 PC 와 같은 40, 빠진 앞 연도를 알린다 (J5-051)", () => {
  const feature = { id: "9999900100100010000", properties: { emd_name: "가상동", emd_code: "9999900100", label: "1", bon: 1, bu: 0, mountain: false } };
  assert.equal(YEAR_SUMMARY_MAX, 40);
  // 2006~2026 (21개 연도): 기본 상한에서 모두 보이고 빠진 연도가 없다
  const prices = Array.from({ length: 21 }, (_, i) => ({ year: 2006 + i, price: 1e6 + i }));
  const all = parcelYearSummaryInfo(feature, [], null, prices);
  assert.deepEqual([all.rows.length, all.rows.at(-1).year, all.omitted], [21, 2006, null]);
  // 상한보다 앞 연도는 자료가 있는 연도만 센다 (빈 연도는 세지 않는다)
  const cut = parcelYearSummaryInfo(feature, [], null, [{ year: 2019, price: 1 }, { year: 2021, price: 2 }, { year: 2025, price: 3 }, { year: 2026, price: 4 }], { maxYears: 2 });
  assert.deepEqual([cut.rows.map((r) => r.year), cut.omitted], [[2026, 2025], { from: 2019, to: 2024, count: 2 }]);
  assert.deepEqual(parcelYearSummaryInfo(feature, [], null, []), { rows: [], omitted: null });
});

test("yearSummaryCells: 연도별 표의 칸, 글 한 줄과 같은 값·판정 (J5-055)", () => {
  const feature = { id: "9999900100100010000", properties: { emd_name: "가상동", emd_code: "9999900100", label: "1", bon: 1, bu: 0, mountain: false } };
  const tx = (id, ymd, jibun) => T(ID(id), ymd, jibun, { lawd_cd: "99999" });
  const txDoc = { j5transactions: "1.0.0", count: 2, transactions: [tx(1, "202608", "1"), tx(2, "202608", "1-*")],
    zone_rule: { version: 1, core: ["가상동"], comparison: [] },
    coverage: [{ lawd_cd: "99999", year: 2026, months_complete: 8, months_any: 8 }, { lawd_cd: "99999", year: 2024, months_complete: 0, months_any: 2 }] };
  const items = parcelHistory(feature, [], { events: [], records: [], transactions: txDoc.transactions });
  const rows = parcelYearSummary(feature, items, txDoc, [{ year: 2025, price: 12e6 }, { year: 2026, price: 11.4e6 }], { ownership: { known: true, dates: ["2026-09-24"] } });
  assert.deepEqual(yearSummaryCells(rows[0]), { year: "2026", price: "11,400,000", delta: "-5.0%", tx: "1건", txCounted: true, txNotes: ["번지대 1", "수집 8/12개월"], owner: "09-24" });
  assert.deepEqual(yearSummaryCells(rows[1]), { year: "2025", price: "12,000,000", delta: "", tx: "미수집", txCounted: false, txNotes: [], owner: "없음" }, "수집 안 한 해는 0 건이 아니라 미수집");
  assert.equal(yearSummaryCells(rows[2]).tx, "일부만 수집");
  assert.deepEqual(yearSummaryCells(rows[2]).txNotes, ["2개월 시도"], "일부만 받은 해는 시도한 개월 수 (PC 와 같은 값)");
  assert.equal(yearSummaryCells(rows[2]).price, null, "공시지가 자료 없음은 null");
  // 수집했고 거래가 없는 해는 0건, 연결 거래는 따로
  const zero = { year: 2027, price: null, deltaPct: null, exact: 0, prefix: 0, linked: 2, monthsComplete: 12, tx: "counted", owner: null };
  assert.deepEqual(yearSummaryCells(zero), { year: "2027", price: null, delta: "", tx: "0건", txCounted: true, txNotes: ["연결 2", "수집 12/12개월"], owner: null });
  assert.equal(yearSummaryCells({ ...zero, tx: "outside" }).tx, "범위 밖");
  assert.equal(yearSummaryCells({ ...zero, tx: "unknown" }).tx, "모름");
  assert.equal(yearSummaryCells({ ...zero, owner: { known: false, dates: [] } }).owner, "자료 없음", "토지소유 자료가 없으면 변동 없음이 아니다");
});
