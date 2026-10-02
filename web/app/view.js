// 조회 파생본의 내용 검증과 이력(연도별 타임라인) 조립 (J5-023, ADR-17). DOM 없음.
// 파생본은 PC 정본의 고정 버전에서 만든 읽기 전용 자료다. 폰은 표시만 하고 정본을 편집하지 않으며, 최신 여부는 파일만으로 알 수 없다.

import { CHANGE_STATUS_LABEL } from "./event.js";
import { ATTR_KIND_LABELS, attrChangeText } from "./parcels.js";

export const VIEW_FORMAT = "j5view";
export const VIEW_SCHEMA_VERSIONS = ["1.0.0"];
export const TRANSACTIONS_FORMAT = "1.0.0";
export const RECORD_TYPE_LABEL = {
  field_observation: "현장 기록", target_price: "목표 매수가", investment_judgment: "투자판단", regulation_review: "규제 검토",
  development_plan: "개발안", financing_plan: "자금안", acquisition_review: "매입 준비 검토",
};
export const PRICE_KIND_LABEL = { target_buy: "목표 매수가", walk_away: "상한(중단선)", reference: "참고 추정" };
export const LINK_LABEL = { confirmed: "확정 연결", pending_evidence: "근거 대기", candidate: "후보", unlinked: "미연결" };
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const isStr = (v) => typeof v === "string" && v.length > 0;

/** manifest 검증. studyId·dataMode 가 주어지면 설정과 같은지도 본다 (다른 정본의 파생본은 넣지 않는다). */
export function validateViewManifest(m, { studyId = null, dataMode = null } = {}) {
  const errs = [];
  if (!m || typeof m !== "object") return ["manifest 가 객체가 아님"];
  if (m.format !== VIEW_FORMAT) errs.push(`format 은 ${VIEW_FORMAT} (파일: ${m.format})`);
  if (!VIEW_SCHEMA_VERSIONS.includes(m.projection_schema_version)) errs.push(`이 앱이 모르는 파생본 형식 버전 ${m.projection_schema_version} (아는 버전: ${VIEW_SCHEMA_VERSIONS.join(", ")})`);
  if (!isStr(m.study_id)) errs.push("study_id 누락");
  if (!["synthetic", "private_real"].includes(m.data_mode)) errs.push("data_mode 는 synthetic 또는 private_real");
  if (!Number.isInteger(m.source_dataset_version) || m.source_dataset_version < 0) errs.push("source_dataset_version 형식");
  if (typeof m.generated_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(m.generated_at)) errs.push("generated_at 형식");
  if (!Array.isArray(m.scope_ids) || !m.scope_ids.every((s) => UUID_RE.test(s))) errs.push("scope_ids 형식");
  if (!m.counts || typeof m.counts !== "object") errs.push("counts 누락");
  const paths = new Set((m.files ?? []).map((f) => f?.path));
  for (const p of ["assets.seed.json", "assets.geojson", "records.jsonl"]) if (!paths.has(p)) errs.push(`필수 파일 없음: ${p}`);
  if (studyId && m.study_id !== studyId) errs.push(`파생본의 작업 공간(${m.study_id})이 설정(${studyId})과 다름. 같은 정본의 파일을 넣는다`);
  if (dataMode && m.data_mode !== dataMode) errs.push(`파생본의 자료 종류(${m.data_mode})가 설정(${dataMode})과 다름`);
  return errs;
}

/** records.jsonl → 기록 배열. 줄마다 필수 필드의 형을 확인한다. 문제가 있으면 throw. */
export function parseRecordsJsonl(text) {
  const out = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    let r;
    try { r = JSON.parse(line); } catch { throw new Error(`records.jsonl ${i + 1}행이 JSON 이 아님`); }
    if (!r || typeof r !== "object" || !UUID_RE.test(r.record_id) || !UUID_RE.test(r.asset_id) || !isStr(r.record_type) || !isStr(r.observed_at) || r.observed_at.length < 10
        || !r.payload || typeof r.payload !== "object" || !Array.isArray(r.attachments) || (r.supersedes_id != null && !UUID_RE.test(r.supersedes_id))) {
      throw new Error(`records.jsonl ${i + 1}행의 필수 필드 형이 맞지 않음`);
    }
    out.push(r);
  }
  return out;
}

/** transactions.json 검증. 빈 배열이면 유효. */
export function validateTransactionsDoc(doc) {
  const errs = [];
  if (!doc || typeof doc !== "object") return ["transactions.json 이 객체가 아님"];
  if (doc.j5transactions !== TRANSACTIONS_FORMAT) errs.push(`거래 목록 형식 버전은 ${TRANSACTIONS_FORMAT} (파일: ${doc.j5transactions})`);
  if (!Array.isArray(doc.transactions)) { errs.push("transactions 가 배열이 아님"); return errs; }
  if (doc.count !== doc.transactions.length) errs.push(`count(${doc.count})와 거래 수(${doc.transactions.length})가 다름`);
  for (let i = 0; i < doc.transactions.length && errs.length < 20; i++) {
    const t = doc.transactions[i];
    const at = `transactions[${i}]`;
    if (!t || typeof t !== "object") { errs.push(`${at} 형식`); continue; }
    if (!UUID_RE.test(t.transaction_id)) errs.push(`${at} transaction_id`);
    if (typeof t.deal_ymd !== "string" || !/^\d{6}$/.test(t.deal_ymd)) errs.push(`${at} deal_ymd`);
    if (t.deal_date !== null && (typeof t.deal_date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(t.deal_date))) errs.push(`${at} deal_date`);
    if (t.emd_name !== null && typeof t.emd_name !== "string") errs.push(`${at} emd_name`);
    if (t.jibun !== null && typeof t.jibun !== "string") errs.push(`${at} jibun`);
    if (typeof t.jibun_masked !== "boolean") errs.push(`${at} jibun_masked`);
    if (t.amount_krw !== null && !(Number.isFinite(t.amount_krw) && t.amount_krw >= 0)) errs.push(`${at} amount_krw`);
    if (t.asset_id !== null && !UUID_RE.test(t.asset_id)) errs.push(`${at} asset_id`);
    if (!isStr(t.zone) || !isStr(t.link_status) || !isStr(t.scope)) errs.push(`${at} zone·link_status·scope`);
  }
  // J5-047: 범위 규칙·수집 개월 (없으면 이전 형식: 폰 연도별 요약은 모름으로 표시)
  if (doc.zone_rule !== undefined) {
    const zr = doc.zone_rule;
    if (!zr || typeof zr !== "object" || !Number.isInteger(zr.version) || !["core", "comparison"].every((k) => Array.isArray(zr[k]) && zr[k].every((x) => typeof x === "string"))
        || !(zr.lawd_cd === undefined || zr.lawd_cd === null || (typeof zr.lawd_cd === "string" && /^\d{5}$/.test(zr.lawd_cd)))) errs.push("zone_rule 형식");
  }
  if (doc.coverage !== undefined) {
    const seen = new Set();
    const bad = !Array.isArray(doc.coverage) || doc.coverage.some((c) => {
      const key = `${c?.lawd_cd}:${c?.year}`;
      const ok = c && typeof c === "object" && typeof c.lawd_cd === "string" && /^\d{5}$/.test(c.lawd_cd) && Number.isInteger(c.year)
        && Number.isInteger(c.months_complete) && Number.isInteger(c.months_any) && c.months_complete >= 0 && c.months_complete <= c.months_any && c.months_any <= 12 && !seen.has(key);
      seen.add(key);
      return !ok;
    });
    if (bad) errs.push("coverage 형식 (시군구 5자리·연도·수집 개월 0~12, 완전 ≤ 전체, 중복 없음)");
  }
  return errs;
}

/** 파생본 안의 파일들이 같은 정본 버전에서 나왔는지 manifest 와 대조한다 (PC 검증기와 같은 규칙, 리뷰 반영). */
export function checkProjectionConsistency(manifest, { transactions = null, parcels = null } = {}) {
  const errs = [];
  const cmp = (name, doc, keys) => {
    for (const k of keys) if (doc[k] !== manifest[k]) errs.push(`${name} 의 ${k}(${doc[k]})가 manifest(${manifest[k]})와 다름`);
  };
  if (transactions) cmp("transactions.json", transactions, ["study_id", "source_dataset_version", "generated_at"]);
  if (transactions && transactions.data_mode !== manifest.data_mode) errs.push(`transactions.json 의 data_mode(${transactions.data_mode})가 manifest(${manifest.data_mode})와 다름`);
  if (parcels) cmp("parcels.geojson", parcels, ["study_id", "source_dataset_version", "generated_at"]);
  return errs;
}

/** 원 → 한국식 짧은 표기. null 은 "금액 미확인". */
export function fmtKrw(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return "금액 미확인";
  if (n >= 1e8) { const v = n / 1e8; return (Number.isInteger(v) ? v.toString() : v.toFixed(v >= 100 ? 0 : 1).replace(/\.0$/, "")) + "억"; }
  if (n >= 1e4) return Math.round(n / 1e4).toLocaleString("ko-KR") + "만";
  return n.toLocaleString("ko-KR") + "원";
}

/** 제공자 지번 문자열 파싱 (PC j5/db/txlinks.py parse_jibun 과 같은 규칙, J5-043). 못 읽으면 null.
 * 온전한 지번: {mountain, masked:false, bon, bu}. 예: "1" → 부번 0, "1-1", "산1-2".
 * 마스킹: {mountain, masked:true, bonPrefix, bonDigits, buPrefix, buDigits}. 실제 표기는 자릿수를 별표로 가린다: "1**"(본번 세 자리, 부번 없음), "1**-*", "16*-3", "1-*".
 * 부번이 없으면 buPrefix "0"·buDigits null(부번 0 인 필지만). "*" 처럼 앞자리가 하나도 없으면 wholeMasked(어느 필지에도 대조하지 않음). */
export function parseJibun(s) {
  if (typeof s !== "string") return null;
  const m = /^(산)?\s*([0-9*]+)(?:-([0-9*]+))?$/.exec(s.trim());
  if (!m) return null;
  const mountain = m[1] === "산";
  const bon = m[2], bu = m[3] ?? "0";
  if (!bon.includes("*") && !bu.includes("*")) return { mountain, masked: false, bon: parseInt(bon, 10), bu: parseInt(bu, 10) };
  const bonPrefix = bon.split("*")[0];
  return { mountain, masked: true, bonPrefix, bonDigits: bon.length, buPrefix: bu.includes("*") ? bu.split("*")[0] : bu, buDigits: bu.includes("*") ? bu.length : null,
           wholeMasked: bonPrefix === "" };
}

/** 거래 지번이 필지에 닿는지: "exact"(같은 필지) · "prefix"(마스킹된 자릿수 범위 안: 이 번지대, 필지 미확정) · null.
 * 법정동 이름이 다르거나, 시군구 코드(거래 lawd_cd ↔ 필지 emd_code 앞 5자리)가 다르면 null (J5-044: 이름이 같은 다른 시군구의 법정동).
 * PC txlinks.jibun_matches(연결 후보 CSV)·parcel_timeline(필지 이력)과 같은 규칙이다. */
export function jibunMatch(tx, props) {
  if (!tx || tx.emd_name == null || props?.emd_name == null || tx.emd_name !== props.emd_name) return null;
  if (typeof tx.lawd_cd === "string" && typeof props.emd_code === "string" && tx.lawd_cd !== props.emd_code.slice(0, 5)) return null;
  const j = parseJibun(tx.jibun);
  if (!j || j.mountain !== !!props.mountain || !Number.isInteger(props.bon)) return null;
  const pbu = props.bu ?? 0;
  if (!j.masked) return j.bon === props.bon && j.bu === pbu ? "exact" : null;
  if (j.wholeMasked) return null;
  const bonS = String(props.bon), buS = String(pbu);
  if (bonS.length !== j.bonDigits || !bonS.startsWith(j.bonPrefix)) return null;
  if (j.buDigits === null ? buS !== j.buPrefix : buS.length !== j.buDigits || !buS.startsWith(j.buPrefix)) return null;
  return "prefix";
}

const dateOf = (s) => (typeof s === "string" ? s.slice(0, 10) : "");
const ymToDate = (ym) => `${ym.slice(0, 4)}-${ym.slice(4, 6)}`;

function recordItem(r, supersededIds, subject) {
  const p = r.payload ?? {};
  let text = "";
  if (r.record_type === "field_observation") text = (CHANGE_STATUS_LABEL[p.change_status] ?? p.change_status ?? "") + (p.note ? ` · ${p.note}` : "") + (r.attachments.length ? ` · 사진 ${r.attachments.length}장` : "");
  else if (r.record_type === "target_price") text = `${PRICE_KIND_LABEL[p.price_kind] ?? p.price_kind ?? ""} ${fmtKrw(p.price_krw)}` + (p.strategy ? ` · ${p.strategy}` : "");
  else if (r.record_type === "investment_judgment") text = [p.status, p.decision, p.strategy].filter(Boolean).join(" · ");
  else text = p.note ?? p.title ?? p.summary ?? "";
  return { id: r.record_id, date: dateOf(r.observed_at), year: dateOf(r.observed_at).slice(0, 4), kind: r.record_type, kindLabel: RECORD_TYPE_LABEL[r.record_type] ?? r.record_type,
           text, source: "pc", superseded: supersededIds.has(r.record_id), subject };
}

function eventItem(e, subject) {
  const p = e.event?.payload ?? {};
  const n = e.event?.attachment_refs?.length ?? 0;
  return { id: e.event_id, date: dateOf(e.event?.observed_at), year: dateOf(e.event?.observed_at).slice(0, 4), kind: "field_observation", kindLabel: "현장 기록",
           text: (CHANGE_STATUS_LABEL[p.change_status] ?? p.change_status ?? "") + (p.note ? ` · ${p.note}` : "") + (n ? ` · 사진 ${n}장` : ""),
           source: "device", deviceStatus: e.status, superseded: false, subject };
}

export function transactionItem(t, { match = null, subject = null } = {}) {
  const date = t.deal_date ?? ymToDate(t.deal_ymd);
  const parts = [fmtKrw(t.amount_krw)];
  if (t.building_use) parts.push(t.building_use);
  if (Number.isFinite(t.building_area_m2)) parts.push(`건물 ${t.building_area_m2}㎡`);
  if (Number.isFinite(t.plottage_area_m2)) parts.push(`대지 ${t.plottage_area_m2}㎡`);
  if (t.share_deal) parts.push("지분 거래");
  const where = `${t.emd_name ?? "동 미상"} ${t.jibun ?? "지번 미공개"}`;
  return { id: t.transaction_id, date, year: date.slice(0, 4), kind: "transaction", kindLabel: "실거래", text: parts.join(" · "), where,
           masked: !!t.jibun_masked, match, link: t.link_status, linkedAsset: t.asset_id, source: "pc", superseded: false, subject };
}

const byDateDesc = (a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : a.id < b.id ? 1 : -1);

/** 물건 하나의 이력: 이 기기 관측(정본에 같은 기록이 있으면 정본 것으로 대체) + 정본 기록 + 확정 연결 거래. 날짜 내림차순. */
export function assetHistory(assetId, { events = [], records = [], transactions = [] } = {}, subject = null) {
  const recs = records.filter((r) => r.asset_id === assetId);
  const superseded = new Set(records.filter((r) => r.supersedes_id).map((r) => r.supersedes_id));
  const inPc = new Set(recs.map((r) => r.record_id));
  const items = recs.map((r) => recordItem(r, superseded, subject));
  for (const e of events) if (e.asset_id === assetId && !inPc.has(e.event_id)) items.push(eventItem(e, subject));
  for (const t of transactions) if (t.asset_id === assetId) items.push(transactionItem(t, { match: "linked", subject }));
  return items.sort(byDateDesc);
}

/** 필지 속성 변화(attrs_history, J5-026) → 이력 항목. 처음 확인은 "토지특성 (처음 확인)" 등으로 표시하고 값은 자료 그대로다. 날짜는 속성 기준일. */
export function attrHistoryItems(feature) {
  const props = feature.properties ?? {};
  const out = [];
  for (const [i, c] of (props.attrs_history ?? []).entries()) {
    const text = attrChangeText(c);
    if (!text) continue;
    out.push({ id: `attrs:${feature.id}:${c.kind}:${c.as_of}:${i}`, date: c.as_of, year: String(c.as_of).slice(0, 4), kind: "land_attrs", kindLabel: ATTR_KIND_LABELS[c.kind] ?? c.kind,
               first: !!c.first, text, source: "pc", superseded: false, subject: null });
  }
  return out;
}

/** 필지 하나의 이력: 지번이 닿는 거래(같은 필지 / 번지대) + 그 안의 물건들의 이력 + 필지 속성 변화(토지 자료). */
export function parcelHistory(feature, assetsInside, data) {
  const props = feature.properties;
  const items = attrHistoryItems(feature);
  const seenTx = new Set();
  for (const t of data.transactions ?? []) {
    const match = jibunMatch(t, props);
    if (match) { items.push(transactionItem(t, { match, subject: null })); seenTx.add(t.transaction_id); }
  }
  for (const a of assetsInside) {
    for (const it of assetHistory(a.asset_id, data, a.label)) if (!(it.kind === "transaction" && seenTx.has(it.id))) items.push(it);
  }
  return items.sort(byDateDesc);
}

/** 필지 연도별 요약 (J5-047, PC `db parcels-years` 와 같은 규칙). 최근 연도 먼저 [{year, price, deltaPct, exact, prefix, linked, monthsComplete, monthsAny, tx}].
 * items 는 parcelHistory 결과, txDoc 은 파생본 transactions.json(없으면 null), prices 는 [{year: 공시 기준연도, price}] (priceTrend 결과를 바꾼 것).
 * tx: "counted"(그해 거래가 있거나 완전 수집한 달이 있음: 수는 센 값) · "not_collected"(수집 실행 없음) · "incomplete"(실패·부분 수집만) ·
 * "outside"(이 필지의 법정동이 거래 범위 밖이라 파생본에 거래가 없음) · "unknown"(거래 파일이나 수집 현황이 없음). counted 가 아니면 수는 null 이다(0 건이 아니다). */
export function parcelYearSummary(feature, items, txDoc, prices = [], opts = {}) {
  return parcelYearSummaryInfo(feature, items, txDoc, prices, opts).rows;
}

/** 폰 연도별 요약의 연도 상한. PC `db parcels-years` 의 MAX_YEARS 와 같다 (J5-051) */
export const YEAR_SUMMARY_MAX = 40;

/** parcelYearSummary 와 같은 줄에 더해, 상한 때문에 보이지 않는 앞 연도 {from, to, count}(자료가 있는 연도 수) 또는 null (J5-051).
 * ownership 은 parcels.ownershipChanges 의 { known, dates } 이며, 주면 줄마다 owner = { known, dates(그 연도의 소유 변동일) } (J5-053, PC parcels-years 와 같다).
 * 소유 변동일은 오래된 날짜가 많아 연도 범위를 정하는 데 쓰지 않는다(범위 안이면 적는다, PC 와 같다). 주지 않으면 owner 는 null.
 * thisYear 를 주면 끝 연도를 그해까지 늘린다(PC 의 올해와 같은 뜻, 폰은 PC 자료 파일을 만든 해). */
export function parcelYearSummaryInfo(feature, items, txDoc, prices = [], { maxYears = YEAR_SUMMARY_MAX, ownership = null, thisYear = null } = {}) {
  const props = feature?.properties ?? {};
  const sgg = typeof props.emd_code === "string" ? props.emd_code.slice(0, 5) : typeof feature?.id === "string" ? feature.id.slice(0, 5) : null;
  const cov = new Map();
  const hasCoverage = !!txDoc && Array.isArray(txDoc.coverage);
  if (hasCoverage) for (const c of txDoc.coverage) if (c.lawd_cd === sgg) cov.set(c.year, c);
  const zr = txDoc?.zone_rule;
  // 규칙에 시군구가 있으면 다른 시군구의 같은 이름 법정동은 범위 밖이다 (J5-048, PC zones.classify 와 같다)
  const inRange = zr ? (!zr.lawd_cd || zr.lawd_cd === sgg) && (zr.core.includes(props.emd_name) || zr.comparison.includes(props.emd_name)) : null;
  const txByYear = new Map();
  for (const it of items) {
    if (it.kind !== "transaction" || !/^\d{4}$/.test(it.year)) continue;
    const y = Number(it.year);
    const c = txByYear.get(y) ?? { exact: 0, prefix: 0, linked: 0 };
    if (it.match === "exact" || it.match === "prefix" || it.match === "linked") c[it.match] += 1;
    txByYear.set(y, c);
  }
  const priceByYear = new Map();
  for (const q of prices) if (Number.isInteger(q.year) && Number.isFinite(q.price)) priceByYear.set(q.year, q.price);
  const years = [...priceByYear.keys(), ...txByYear.keys(), ...cov.keys()];
  // 끝 연도는 PC parcels-years 처럼 "올해" 까지 늘린다. 폰의 올해는 PC 자료 파일을 만든 해(thisYear)이며 기기 시계를 쓰지 않는다.
  // 그해의 소유 변동만 있는 경우에도 줄이 생긴다. 오래된 소유 변동일은 범위를 넓히지 않는다 (J5-053 리뷰)
  const ownThisYear = Number.isInteger(thisYear) && (ownership?.dates ?? []).some((d) => d.startsWith(`${thisYear}-`));
  if (!years.length && !ownThisYear) return { rows: [], omitted: null };
  const ends = Number.isInteger(thisYear) ? [...years, thisYear] : years;
  const y1 = Math.max(...ends), y0 = Math.max(years.length ? Math.min(...years) : y1, y1 - maxYears + 1);
  const before = [...new Set(years.filter((y) => y < y0))];
  const omitted = before.length ? { from: Math.min(...before), to: y0 - 1, count: before.length } : null;
  const rows = [];
  let prev = null;
  for (let y = y0; y <= y1; y++) {
    const price = priceByYear.get(y) ?? null;
    const c = cov.get(y);
    const t = txByYear.get(y);
    let tx;
    if (t) tx = "counted";
    else if (!txDoc) tx = "unknown";
    else if (inRange === false) tx = "outside";
    else if (!hasCoverage) tx = "unknown";
    else if (c && c.months_complete > 0) tx = "counted";
    else tx = c && c.months_any > 0 ? "incomplete" : "not_collected";
    const counted = tx === "counted";
    rows.push({ year: y, price, deltaPct: price != null && prev ? ((price - prev) / prev) * 100 : null,
                exact: counted ? t?.exact ?? 0 : null, prefix: counted ? t?.prefix ?? 0 : null, linked: counted ? t?.linked ?? 0 : null,
                monthsComplete: c?.months_complete ?? (hasCoverage ? 0 : null), monthsAny: c?.months_any ?? (hasCoverage ? 0 : null), tx,
                owner: ownership ? { known: !!ownership.known, dates: (ownership.dates ?? []).filter((d) => d.startsWith(`${y}-`)) } : null });
    prev = price;
  }
  return { rows: rows.reverse(), omitted };
}

const TX_STATE_TEXT = { not_collected: "거래 미수집", incomplete: "거래 수집 실패·부분만", outside: "거래 범위 밖 (PC 자료에 거래가 없음)", unknown: "거래 수집 현황 모름" };

/** 연도별 요약 한 줄 글 [연도, 값] */
export function yearSummaryRow(r) {
  const price = r.price == null ? "공시지가 자료 없음" : `공시지가 ${Math.round(r.price).toLocaleString("ko-KR")}원/㎡` + (r.deltaPct == null ? "" : ` (${r.deltaPct > 0 ? "+" : ""}${r.deltaPct.toFixed(1)}%)`);
  let tx;
  if (r.tx === "counted") tx = `같은 필지 ${r.exact} · 번지대 ${r.prefix}` + (r.linked ? ` · 연결 ${r.linked}` : "") + (r.monthsComplete != null ? ` (수집 ${r.monthsComplete}/12개월)` : "");
  else tx = TX_STATE_TEXT[r.tx];
  // 소유 변동일 (J5-053): 토지소유 자료가 없으면 변동이 없는 것이 아니라 모름이다 (PC years_text 와 같은 글)
  const own = !r.owner ? "" : !r.owner.known ? " · 소유 자료 없음" : r.owner.dates.length ? ` · 소유 변동 ${r.owner.dates.join(", ")}` : "";
  return [String(r.year), `${price} · ${tx}${own}`];
}

const TX_STATE_SHORT = { not_collected: "미수집", incomplete: "일부만 수집", outside: "범위 밖", unknown: "모름" };

/**
 * 연도별 요약 한 줄을 표의 칸으로 (J5-055): 필지 카드·이력 화면의 표가 쓴다. 글 한 줄(yearSummaryRow, PC years_text 와 같은 글)과 값·판정은 같다.
 * price 는 원/㎡ 글(자료가 없으면 null), delta 는 전년 대비, tx 는 같은 필지 거래 수(수집했을 때) 또는 모르는 사유,
 * txNotes 는 번지대·연결 수와 수집 개월, owner 는 그 연도의 소유 변동일(월-일) 또는 "없음"·"자료 없음"(소유 칸이 없으면 null).
 */
export function yearSummaryCells(r) {
  const counted = r.tx === "counted";
  const txNotes = [];
  if (counted && r.prefix) txNotes.push(`번지대 ${r.prefix}`);
  if (counted && r.linked) txNotes.push(`연결 ${r.linked}`);
  if (counted && r.monthsComplete != null) txNotes.push(`수집 ${r.monthsComplete}/12개월`);
  const owner = !r.owner ? null : !r.owner.known ? "자료 없음" : r.owner.dates.length ? r.owner.dates.map((d) => d.slice(5)).join(", ") : "없음";
  return {
    year: String(r.year),
    price: r.price == null ? null : Math.round(r.price).toLocaleString("ko-KR"),
    delta: r.deltaPct == null ? "" : `${r.deltaPct > 0 ? "+" : ""}${r.deltaPct.toFixed(1)}%`,
    tx: counted ? `${r.exact}건` : TX_STATE_SHORT[r.tx], txCounted: counted, txNotes, owner,
  };
}

/** 연도별 묶음 [{year, items}] (최근 연도 먼저). 날짜 없는 항목은 "날짜 미상". */
export function groupByYear(items) {
  const map = new Map();
  for (const it of items) {
    const y = /^\d{4}$/.test(it.year) ? it.year : "날짜 미상";
    if (!map.has(y)) map.set(y, []);
    map.get(y).push(it);
  }
  return [...map.entries()].sort((a, b) => (a[0] === "날짜 미상" ? 1 : b[0] === "날짜 미상" ? -1 : b[0].localeCompare(a[0]))).map(([year, list]) => ({ year, items: list }));
}

/** 파생본 상태 문구용 요약. 최신 여부는 파일만으로 모르므로 항상 "PC 에서 확인" 을 붙인다. */
export function viewSummary(rec) {
  if (!rec) return null;
  const m = rec.manifest;
  const c = m.counts ?? {};
  return { version: m.source_dataset_version, generatedAt: m.generated_at, assets: c.assets ?? 0, records: rec.records?.length ?? c.records ?? 0,
           transactions: rec.transactions?.transactions?.length ?? 0, parcels: c.parcels ?? 0, photosSkipped: rec.photos_skipped ?? 0, source: rec.source, loadedAt: rec.loaded_at };
}
