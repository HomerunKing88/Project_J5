// view.js (J5-023, ADR-17): manifest·기록·거래 검증, 금액 표기, 지번 대조, 물건·필지 이력 조립, 연도 묶음. DOM 없음.
import test from "node:test";
import assert from "node:assert/strict";
import { validateViewManifest, parseRecordsJsonl, validateTransactionsDoc, checkProjectionConsistency, fmtKrw, parseJibun, jibunMatch, assetHistory, parcelHistory, attrHistoryItems, groupByYear, viewSummary, parcelYearSummary, parcelYearSummaryInfo, YEAR_SUMMARY_MAX, yearSummaryRow, yearSummaryCells, recentDeals, parcelSummaryItems } from "../../web/app/view.js";

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

test("recentDeals: 같은 필지·확정 연결 거래만 최근 것부터, 번지대는 수만 (J5-057)", () => {
  const feature = { id: "9999900100100010000", properties: { emd_name: "가상동", emd_code: "9999900100", label: "1", bon: 1, bu: 0, mountain: false } };
  const tx = (id, ymd, jibun, extra = {}) => T(ID(id), ymd, jibun, { lawd_cd: "99999", ...extra });
  const txs = [tx(1, "202308", "1"), tx(2, "202508", "1", { building_use: "제1종근린생활", plottage_area_m2: 30 }), tx(3, "202601", "1-*"), tx(4, "202409", "1"), tx(5, "202602", "1"),
    tx(6, "202407", "9", { asset_id: ID(50), link_status: "confirmed" }), tx(7, "202601", "2")];
  const items = parcelHistory(feature, [{ asset_id: ID(50), label: "가상 물건" }], { events: [], records: [], transactions: txs });
  const d = recentDeals(items, 3);
  assert.deepEqual(d.rows.map((r) => [r.date.slice(0, 7), r.match]), [["2026-02", "exact"], ["2025-08", "exact"], ["2024-09", "exact"]], "최근 3건");
  assert.equal(d.total, 5, "같은 필지 4건 + 확정 연결 1건");
  assert.equal(d.prefix, 1, "번지대 거래는 목록에 넣지 않고 센다");
  assert.deepEqual(d.rows[1].details, ["제1종근린생활", "건물 50.5㎡", "대지 30㎡"]);
  assert.equal(d.rows[1].amount, "10억");
  assert.deepEqual(recentDeals(items, 10).rows.map((r) => r.match).filter((m) => m === "linked").length, 1, "확정 연결 거래는 지번이 달라도 들어간다");
  assert.deepEqual(recentDeals([], 3), { rows: [], total: 0, prefix: 0 });
});

test("parcelSummaryItems: 위치로만 찾은 대상의 연결 거래는 필지 요약에서 뺀다 (J5-057 리뷰)", () => {
  const feature = { id: "9999900100100010000", properties: { emd_name: "가상동", emd_code: "9999900100", label: "1", bon: 1, bu: 0, mountain: false } };
  const tx = (id, ymd, jibun, extra = {}) => T(ID(id), ymd, jibun, { lawd_cd: "99999", ...extra });
  const txs = [tx(1, "202508", "1"), tx(2, "202509", "77", { asset_id: ID(50), link_status: "confirmed" }), tx(3, "202510", "88", { asset_id: ID(60), link_status: "confirmed" })];
  // 대상 50 은 PC 에서 이 필지에 연결, 대상 60 은 위치점만 이 필지 안
  const items = parcelHistory(feature, [{ asset_id: ID(50), label: "연결 대상" }, { asset_id: ID(60), label: "위치 대상" }], { events: [], records: [], transactions: txs });
  assert.equal(items.filter((i) => i.kind === "transaction").length, 3, "이력 목록에는 모두 남는다");
  const summary = parcelSummaryItems(items, [ID(50)]);
  assert.deepEqual(summary.filter((i) => i.kind === "transaction").map((i) => [i.id, i.match]), [[ID(2), "linked"], [ID(1), "exact"]]);
  assert.deepEqual(recentDeals(summary).rows.map((r) => r.match), ["linked", "exact"], "최근 거래에 위치 대상의 거래가 들어가지 않는다");
  assert.equal(parcelYearSummary(feature, summary, null, []).find((r) => r.year === 2025).linked, 1, "연도별 표의 연결 수도 PC 연결 대상만");
  assert.equal(parcelSummaryItems(items, []).filter((i) => i.match === "linked").length, 0);
});

// ---- 지도의 실거래 (J5-063) ----
import { readFileSync as readFileSyncDeals } from "node:fs";
import { placeDeals, dealKind, unitPrice, dealValueText, dealDetailText, dealsSince, dealSince, validateTransactionsDoc as validateTxDeals, PYEONG_M2 } from "../../web/app/view.js";

test("dealKind·unitPrice·dealValueText: 종류 짧은 이름, 총액·평당 단가, 낼 수 없는 사유", () => {
  assert.equal(dealKind({ building_use: "제2종근린생활" }), "근린");
  assert.equal(dealKind({ building_use: "업무" }), "업무");
  assert.equal(dealKind({ building_use: null, building_area_m2: null, plottage_area_m2: 120 }), "토지");
  assert.equal(dealKind({}), "건물");
  const t = { amount_krw: 1_850_000_000, plottage_area_m2: 100 * PYEONG_M2, building_area_m2: 250 * PYEONG_M2 };
  assert.equal(unitPrice(t, "total"), 1_850_000_000);
  assert.equal(Math.round(unitPrice(t, "land")), 18_500_000);
  assert.equal(dealValueText(t, "total"), "18.5억");
  assert.equal(dealValueText(t, "land"), "1,850만/평");
  assert.equal(dealValueText(t, "floor"), "740만/평");
  assert.equal(dealValueText({ ...t, share_deal: true }, "land"), "지분 거래", "지분 거래는 단가를 내지 않는다");
  assert.equal(unitPrice({ ...t, share_deal: true }, "total"), 1_850_000_000, "총액은 그대로");
  assert.equal(dealValueText({ amount_krw: 5e8, plottage_area_m2: null }, "land"), "대지 면적 미확인");
  assert.equal(dealValueText({ amount_krw: 5e8, building_area_m2: 0 }, "floor"), "연면적 미확인");
  assert.equal(dealValueText({ amount_krw: null }, "total"), "금액 미확인");
  assert.equal(dealDetailText({ ...t, plottage_area_m2: 330.6, building_area_m2: 1250, build_year: 1998 }), "대지 330.6㎡ (1,850만/평) · 연면적 1250㎡ (489만/평) · 1998년 건축");
});

test("dealsSince·dealSince: 기간 거르기 (월만 아는 거래는 그달 1일)", () => {
  assert.equal(dealSince(5, "2026-10-13"), "2021-10-13");
  assert.equal(dealSince(0, "2026-10-13"), null, "전체");
  const txs = [{ deal_date: "2021-10-12", deal_ymd: "202110" }, { deal_date: "2021-10-13", deal_ymd: "202110" }, { deal_date: null, deal_ymd: "202111" }, { deal_date: null, deal_ymd: "202109" }];
  assert.equal(dealsSince(txs, "2021-10-13").length, 2);
  assert.equal(dealsSince(txs, null).length, 4);
});

test("placeDeals: 온전한 지번은 그 필지, 가린 지번은 번지대 묶음(필지 미확정), 닿지 않으면 수만 (연습용 거래·필지)", () => {
  const doc = JSON.parse(readFileSyncDeals(new URL("../../web/data/transactions.synthetic.json", import.meta.url), "utf8"));
  const parcels = JSON.parse(readFileSyncDeals(new URL("../../web/data/parcels.synthetic.j5parcels.json", import.meta.url), "utf8"));
  assert.deepEqual(validateTxDeals(doc), [], "연습용 거래 파일은 폰 검사기를 통과한다");
  assert.equal(doc.data_mode, "synthetic");
  const r = placeDeals(doc.transactions, parcels.features);
  const byPnu = Object.fromEntries(r.parcels.map((p) => [p.pnu, p.txs.map((t) => t.deal_date)]));
  assert.deepEqual(byPnu["9999900100100010000"], ["2024-03-15", "2019-06-20"], "최근 것부터");
  assert.deepEqual(byPnu["9999900100100040002"], ["2023-11-09", "2020-04-10"], "토지 매매도 그 필지 (J5-065)");
  assert.equal(r.groups.length, 1);
  assert.equal(r.groups[0].jibun, "1-*");
  assert.deepEqual(r.groups[0].features.map((f) => f.id).sort(), ["9999900100100010000", "9999900100100010001"], "1-* 는 부번 한 자리인 1·1-1 이 후보");
  assert.equal(r.groups[0].txs.length, 2);
  assert.equal(r.outside, 1, "3* 는 두 자리 본번이라 불러온 필지에 닿지 않는다");
  // 다른 시군구의 같은 이름 법정동은 닿지 않는다 (J5-044 규칙 그대로)
  const other = placeDeals([{ ...doc.transactions[0], lawd_cd: "11110" }], parcels.features);
  assert.deepEqual([other.parcels.length, other.groups.length, other.outside], [0, 0, 1]);
});

// ---- 주변 사례 (J5-064) ----
import { nearbyComps, priceForYear, median, COMP_MIN_RATIOS } from "../../web/app/view.js";

const compParcel = (id, bon, prices) => ({ id, type: "Feature", geometry: null, properties: { emd_name: "가상동", emd_code: "9999900100", bon, bu: 0, mountain: false,
  attrs: { official_land_price_krw_m2: prices.at(-1)[1], price_base_year: prices.at(-1)[0], as_of: `${prices.at(-1)[0]}-06-01` },
  attrs_history: prices.map(([y, v], i) => ({ kind: "land_feature", as_of: `${y}-06-01`, first: i === 0, changes: i === 0 ? {} : { official_land_price_krw_m2: { from: prices[i - 1][1], to: v }, price_base_year: { from: prices[i - 1][0], to: y } } })) } });
const compTx = (id, date, jibun, amount, plottage, extra = {}) => ({ transaction_id: id, lawd_cd: "99999", deal_ymd: date.slice(0, 4) + date.slice(5, 7), deal_date: date, emd_name: "가상동",
  jibun, jibun_masked: jibun.includes("*"), amount_krw: amount, plottage_area_m2: plottage, building_area_m2: plottage * 3, share_deal: false, building_use: "제2종근린생활", ...extra });

test("median·priceForYear: 중간값, 거래 연도 이하 기준연도의 공시지가만 (늦은 해 값으로 거슬러 쓰지 않음)", () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 2, 3]), 2.5);
  assert.equal(median([]), null);
  const f = compParcel("p1", 1, [[2020, 1_000_000], [2023, 1_200_000], [2025, 1_500_000]]);
  assert.deepEqual(priceForYear(f, 2024), { price: 1_200_000, year: 2023 });
  assert.deepEqual(priceForYear(f, 2025), { price: 1_500_000, year: 2025 });
  assert.equal(priceForYear(f, 2019), null);
});

test("nearbyComps: 같은 시군구·동, 기간, 평당 단가 중간·범위, 거래 연도 공시지가 배율, 사례 3건부터 참고 추정", () => {
  const fs = [compParcel("p1", 1, [[2020, 1_000_000], [2024, 1_250_000]]), compParcel("p2", 2, [[2020, 2_000_000], [2024, 2_500_000]]),
    compParcel("p3", 3, [[2020, 500_000], [2024, 600_000]]), compParcel("p4", 4, [[2024, 800_000]])];
  const ids = (n) => `00000000-0000-4000-8000-00000000000${n}`;
  const txs = [
    compTx(ids(1), "2024-05-01", "1", 2_500_000_000, 1000),   // 250만/㎡ ÷ 125만 = 2배 (2024 공시지가)
    compTx(ids(2), "2021-03-01", "2", 5_000_000_000, 1000),   // 500만/㎡ ÷ 200만(2020) = 2.5배
    compTx(ids(3), "2023-07-01", "3", 1_500_000_000, 1000),   // 150만/㎡ ÷ 50만(2020) = 3배
    compTx(ids(4), "2022-01-01", "1**", 9_000_000_000, 1000),  // 번지대: 배율 없음
    compTx(ids(5), "2024-02-01", "2", 1_000_000_000, 100, { share_deal: true }),   // 지분: 단가·배율 없음
    compTx(ids(6), "2024-02-01", "1", 7_000_000_000, 1000, { emd_name: "다른동" }),   // 다른 동
    compTx(ids(7), "2024-02-01", "1", 7_000_000_000, 1000, { lawd_cd: "11110" }),   // 다른 시군구
    compTx(ids(8), "2015-02-01", "1", 7_000_000_000, 1000),   // 기간 밖
  ];
  const c = nearbyComps(fs[3], txs, fs, { since: "2020-10-13" });
  assert.equal(c.count, 5, "같은 시군구·동·기간 안 5건");
  assert.deepEqual(c.comps.map((x) => x.t.transaction_id), [ids(1), ids(5), ids(3), ids(4), ids(2)], "최근 것부터");
  assert.equal(c.land.n, 4, "지분 거래는 단가에서 뺀다");
  assert.equal(Math.round(c.land.median / PYEONG_M2), 3_750_000, "㎡당 150만·250만·500만·900만의 중간 375만을 평당으로");
  assert.equal(Math.round(c.land.min / PYEONG_M2), 1_500_000);
  assert.equal(Math.round(c.land.max / PYEONG_M2), 9_000_000);
  assert.deepEqual(c.comps.map((x) => x.ratio === null ? null : Number(x.ratio.toFixed(2))), [2, null, 3, null, 2.5]);
  assert.deepEqual(c.comps.map((x) => x.priceYear), [2024, null, 2020, null, 2020], "배율에 쓴 공시지가의 실제 기준연도 (2023 거래는 2020 값)");
  assert.equal(c.floor.n, 4);
  assert.equal(Math.round(c.floor.min / PYEONG_M2), 500_000, "연면적 평당 범위 (연면적은 대지의 3배)");
  assert.equal(Math.round(c.floor.max / PYEONG_M2), 3_000_000);
  assert.equal(c.ratio.n, 3);
  assert.equal(c.ratio.median, 2.5);
  assert.equal(c.reference.ratio, 2.5);
  assert.equal(c.reference.price, 800_000);
  assert.equal(Math.round(c.reference.perM2), 2_000_000, "이 필지 공시지가 80만 × 2.5배");
  assert.equal(c.comps.find((x) => x.t.transaction_id === ids(4)).where, "가상동 1** 번지대");
  // 사례가 모자라면 참고값을 내지 않는다
  const few = nearbyComps(fs[3], txs.slice(0, 2), fs, { since: "2020-10-13" });
  assert.match(few.reference.missing, new RegExp(`2건이라 참고값을 내지 않습니다 \\(${COMP_MIN_RATIOS}건부터\\)`));
  // 이 필지의 공시지가가 없으면 참고값을 내지 않는다
  const noPrice = nearbyComps({ id: "x", properties: { emd_name: "가상동", emd_code: "9999900100", bon: 9, bu: 0 } }, txs, fs, { since: "2020-10-13" });
  assert.match(noPrice.reference.missing, /공시지가 자료가 없어/);
  assert.equal(c.comps.find((x) => x.t.transaction_id === ids(1)).mine, false);
  assert.equal(nearbyComps(fs[0], txs, fs, { since: "2020-10-13" }).comps.find((x) => x.t.transaction_id === ids(1)).mine, true, "이 필지의 거래 표시");
});

// ---- 수집 종류 (J5-065): 상업·업무용·단독·다가구·토지 ----
import { DEAL_KIND_NAMES } from "../../web/app/view.js";

test("수집 종류: dealKind·단가 사유·상세 글 (토지·단독·다가구)", () => {
  assert.deepEqual(Object.keys(DEAL_KIND_NAMES), ["nrg", "sh", "land"], "PC j5.collect.rt.KINDS 와 같은 이름");
  assert.equal(dealKind({ deal_kind: "land", building_use: null, plottage_area_m2: null }), "토지", "면적이 없어도 토지 매매는 토지");
  assert.equal(dealKind({ deal_kind: "sh", building_use: "다가구" }), "다가구");
  assert.equal(dealKind({ deal_kind: "sh", building_use: "단독" }), "단독");
  assert.equal(dealKind({ deal_kind: "sh", building_use: null, building_area_m2: 120 }), "주택", "주택유형이 없으면 주택");
  assert.equal(dealKind({ deal_kind: "nrg", building_use: "업무" }), "업무");
  const land = { deal_kind: "land", amount_krw: 1_000_000_000, plottage_area_m2: 100 * PYEONG_M2, building_area_m2: null, land_category: "대", land_use: "일반상업지역" };
  assert.equal(dealValueText(land, "land"), "1,000만/평");
  assert.equal(dealValueText(land, "floor"), "토지 거래 (건물 없음)", "토지 거래의 연면적 단가는 미확인이 아니라 해당 없음");
  assert.equal(dealValueText({ ...land, plottage_area_m2: null }, "land"), "토지 면적 미확인");
  assert.equal(dealDetailText({ ...land, plottage_area_m2: 330.6 }), "토지 330.6㎡ (1,000만/평) · 지목 대 · 일반상업지역");
  assert.equal(dealDetailText({ ...land, plottage_area_m2: 330.6, land_category: null, land_use: null }), "토지 330.6㎡ (1,000만/평)");
});

test("수집 종류: 거래 파일 검사와 연도별 표의 받은 종류", () => {
  const ok = { j5transactions: "1.0.0", count: 1, transactions: [{ ...T(ID(1), "202608", "1"), deal_kind: "land", land_category: "대" }],
    coverage: [{ lawd_cd: "99999", year: 2026, months_complete: 8, months_any: 8, kind_months: { nrg: 8, land: 2 } }] };
  assert.deepEqual(validateTransactionsDoc(ok), []);
  assert.deepEqual(validateTransactionsDoc({ ...ok, transactions: [{ ...ok.transactions[0], deal_kind: "apt" }] }), ["transactions[0] deal_kind"]);
  assert.deepEqual(validateTransactionsDoc({ ...ok, transactions: [{ ...ok.transactions[0], land_category: 3 }] }), ["transactions[0] land_category"]);
  for (const km of [{ apt: 1 }, { nrg: 9 }, { nrg: 0 }, { nrg: 1.5 }, ["nrg"], "nrg"]) {
    assert.equal(validateTransactionsDoc({ ...ok, coverage: [{ ...ok.coverage[0], kind_months: km }] }).length, 1, `kind_months ${JSON.stringify(km)}`);
  }
  const feature = { id: "9999900100100010000", properties: { emd_name: "가상동", emd_code: "9999900100", label: "1", bon: 1, bu: 0, mountain: false } };
  const txDoc = { ...ok, zone_rule: { version: 1, core: ["가상동"], comparison: [] }, transactions: [] , count: 0,
    coverage: [{ lawd_cd: "99999", year: 2026, months_complete: 8, months_any: 8, kind_months: { nrg: 8 } }, { lawd_cd: "99999", year: 2025, months_complete: 12, months_any: 12, kind_months: { nrg: 12, sh: 12, land: 12 } },
      { lawd_cd: "99999", year: 2024, months_complete: 12, months_any: 12, kind_months: { nrg: 12, sh: 1, land: 1 } }] };
  const rows = parcelYearSummary(feature, [], txDoc, []);
  assert.deepEqual(yearSummaryCells(rows[0]).txNotes, ["수집 8/12개월", "상업·업무만"], "일부 종류만 받은 해의 0건은 그 종류 안의 0건");
  assert.match(yearSummaryRow(rows[0])[1], /\(수집 8\/12개월, 상업·업무만\)$/);
  assert.deepEqual(yearSummaryCells(rows[1]).txNotes, ["수집 12/12개월"], "모든 종류를 같은 개월 받은 해는 덧붙이지 않는다");
  assert.deepEqual(yearSummaryCells(rows[2]).txNotes, ["수집 12/12개월", "상업·업무 12 · 단독·다가구 1 · 토지 1개월"], "리뷰 반영(PR #112): 세 종류가 있어도 개월이 다르면 종류별 개월");
  const old = parcelYearSummary(feature, [], { ...txDoc, coverage: txDoc.coverage.map(({ kind_months, ...c }) => c) }, []);
  assert.deepEqual(yearSummaryCells(old[0]).txNotes, ["수집 8/12개월"], "종류 정보가 없는 이전 파일은 그대로");
  assert.equal("kindMonths" in old[0], false);
});

// ---- 거래 종류 거르기 (J5-066) ----
import { dealKindOf, dealsOfKind, dealKindsPresent, DEAL_KIND_FILTERS } from "../../web/app/view.js";

test("거래 종류 거르기: 종류 칸이 없으면 상업·업무용, 종류별 거르기, 든 종류", () => {
  assert.deepEqual(DEAL_KIND_FILTERS, ["all", "nrg", "sh", "land"]);
  assert.equal(dealKindOf({}), "nrg", "J5-065 전 파일은 상업·업무용만 담았다");
  assert.equal(dealKindOf({ deal_kind: null }), "nrg");
  assert.equal(dealKindOf({ deal_kind: "land" }), "land");
  assert.equal(dealKindOf({ deal_kind: "apt" }), "nrg", "모르는 값은 검사기가 거절하며, 여기서는 기본 종류로 본다");
  const txs = [{ id: 1 }, { id: 2, deal_kind: "sh" }, { id: 3, deal_kind: "land" }, { id: 4, deal_kind: "land" }];
  assert.equal(dealsOfKind(txs, "all"), txs, "전체는 그대로");
  assert.deepEqual(dealsOfKind(txs, "land").map((t) => t.id), [3, 4]);
  assert.deepEqual(dealsOfKind(txs, "nrg").map((t) => t.id), [1]);
  assert.deepEqual(dealsOfKind(null, "land"), []);
  assert.deepEqual(dealKindsPresent(txs), ["nrg", "sh", "land"]);
  assert.deepEqual(dealKindsPresent([{ deal_kind: "land" }, {}]), ["nrg", "land"], "정해진 순서");
  assert.deepEqual(dealKindsPresent([]), []);
  const doc = JSON.parse(readFileSyncDeals(new URL("../../web/data/transactions.synthetic.json", import.meta.url), "utf8"));
  assert.deepEqual(dealKindsPresent(doc.transactions), ["nrg", "sh", "land"], "연습용 거래에는 세 종류가 다 있다");
});
