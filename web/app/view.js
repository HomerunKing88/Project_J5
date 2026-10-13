// 조회 파생본의 내용 검증과 이력(연도별 타임라인) 조립 (J5-023, ADR-17). DOM 없음.
// 파생본은 PC 정본의 고정 버전에서 만든 읽기 전용 자료다. 폰은 표시만 하고 정본을 편집하지 않으며, 최신 여부는 파일만으로 알 수 없다.

import { CHANGE_STATUS_LABEL } from "./event.js";
import { ATTR_KIND_LABELS, attrChangeText, priceTrend } from "./parcels.js";

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
    // J5-065: 수집 종류·지목 (없으면 이전 형식)
    if (t.deal_kind !== undefined && t.deal_kind !== null && !knownKind(t.deal_kind)) errs.push(`${at} deal_kind`);
    if (t.land_category !== undefined && t.land_category !== null && typeof t.land_category !== "string") errs.push(`${at} land_category`);
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
        && Number.isInteger(c.months_complete) && Number.isInteger(c.months_any) && c.months_complete >= 0 && c.months_complete <= c.months_any && c.months_any <= 12 && !seen.has(key)
        && (c.kind_months === undefined || (c.kind_months && typeof c.kind_months === "object" && !Array.isArray(c.kind_months)
          && Object.entries(c.kind_months).every(([k, v]) => knownKind(k) && Number.isInteger(v) && v >= 1 && v <= c.months_complete)));
      seen.add(key);
      return !ok;
    });
    if (bad) errs.push("coverage 형식 (시군구 5자리·연도·수집 개월 0~12, 완전 ≤ 전체, 중복 없음, 종류별 개월)");
  }
  return errs;
}

/** 범위 규칙(transactions.json 의 zone_rule)으로 법정동의 범위 → "core"·"comparison"·"outside" (PC j5.db.zones.classify 와 같은 규칙, J5-068).
 *  `11140:을지로4가` 처럼 시군구를 붙인 항목이 먼저 맞고, 이름만 쓴 항목은 규칙의 lawd_cd 시군구(없으면 모든 시군구)에만 맞는다. */
export function zoneOfRule(zr, emdName, sgg) {
  if (sgg && emdName) {
    const key = `${sgg}:${emdName}`;
    if (zr.core.includes(key)) return "core";
    if (zr.comparison.includes(key)) return "comparison";
  }
  if (zr.lawd_cd && zr.lawd_cd !== sgg) return "outside";
  if (zr.core.includes(emdName)) return "core";
  if (zr.comparison.includes(emdName)) return "comparison";
  return "outside";
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
           amount: fmtKrw(t.amount_krw), details: parts.slice(1),
           masked: !!t.jibun_masked, match, link: t.link_status, linkedAsset: t.asset_id, source: "pc", superseded: false, subject };
}

/**
 * 필지 요약(연도별 표·최근 거래)에 쓰는 항목 (J5-057 리뷰 반영 PR #104). 대상에 확정 연결된 거래(linked)는 그 대상이 PC 에서 이 필지에 연결된 경우만 남긴다.
 * 위치점만 이 필지 안에 있는 대상(근거 inside)은 필지 구성이 확정되지 않았으므로, 그 대상의 거래를 이 필지의 거래로 세지 않는다 (PC parcel_years 의 "연결 물건" 과 같다).
 * 필지 이력의 시간순 목록은 그 대상 이름과 함께 모든 항목을 그대로 보인다.
 */
export function parcelSummaryItems(items, pcLinkedAssetIds = []) {
  const ok = new Set(pcLinkedAssetIds);
  return items.filter((it) => !(it.kind === "transaction" && it.match === "linked" && !ok.has(it.linkedAsset)));
}

/**
 * 필지 카드의 최근 거래 (J5-057): parcelHistory 의 거래 가운데 이 필지와 지번이 같은 거래(exact)와 사람이 확정 연결한 거래(linked)만, 최근 것부터 limit 건.
 * 지번 일부가 가려진 번지대 거래(prefix)는 이 필지의 거래로 확정하지 않으므로 목록에 넣지 않고 수만 센다 (AGENTS "마스킹된 거래를 특정 PNU 에 확정하지 않는다").
 * { rows: [{date, amount, details, match}], total(같은 필지·연결 수), prefix(번지대 수) }
 */
export function recentDeals(items, limit = 3) {
  const tx = items.filter((it) => it.kind === "transaction" && !it.superseded);
  const mine = tx.filter((it) => it.match === "exact" || it.match === "linked").sort(byDateDesc);
  return { rows: mine.slice(0, limit).map((it) => ({ date: it.date, amount: it.amount, details: it.details ?? [], match: it.match })),
           total: mine.length, prefix: tx.filter((it) => it.match === "prefix").length };
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
  // 규칙에 시군구가 있으면 다른 시군구의 같은 이름 법정동은 범위 밖이다 (J5-048), 시군구를 붙인 항목이 먼저 맞는다 (J5-068). PC zones.classify 와 같다
  const inRange = zr ? zoneOfRule(zr, props.emd_name, sgg) !== "outside" : null;
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
                ...(c?.kind_months ? { kindMonths: c.kind_months } : {}),
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
  const only = kindsOnlyText(r);
  if (r.tx === "counted") tx = `같은 필지 ${r.exact} · 번지대 ${r.prefix}` + (r.linked ? ` · 연결 ${r.linked}` : "") + (r.monthsComplete != null ? ` (수집 ${r.monthsComplete}/12개월${only ? `, ${only}` : ""})` : "");
  else tx = TX_STATE_TEXT[r.tx];
  // 소유 변동일 (J5-053): 토지소유 자료가 없으면 변동이 없는 것이 아니라 모름이다 (PC years_text 와 같은 글)
  const own = !r.owner ? "" : !r.owner.known ? " · 소유 자료 없음" : r.owner.dates.length ? ` · 소유 변동 ${r.owner.dates.join(", ")}` : "";
  return [String(r.year), `${price} · ${tx}${own}`];
}

const TX_STATE_SHORT = { not_collected: "미수집", incomplete: "일부만 수집", outside: "범위 밖", unknown: "모름" };

/** 수집 종류 (PC j5.collect.rt.KINDS 와 같은 이름, J5-065) → 짧은 이름 */
export const DEAL_KIND_NAMES = { nrg: "상업·업무", sh: "단독·다가구", land: "토지" };
const knownKind = (k) => typeof k === "string" && Object.keys(DEAL_KIND_NAMES).includes(k);

/** 그해 종류별로 받은 개월이 고르지 않으면 적는다 (0 건이 받은 종류·달 안의 0 건임을 보인다, 리뷰 반영 PR #112):
 *  모든 종류를 같은 개월만큼 받았으면 "", 받은 종류는 모두 같은 개월이면 "상업·업무만", 종류마다 개월이 다르면 "상업·업무 12 · 토지 1개월".
 *  종류 정보가 없는 이전 파일은 "". */
function kindsOnlyText(r) {
  const km = r.kindMonths;
  if (r.tx !== "counted" || !km) return "";
  const kinds = Object.keys(DEAL_KIND_NAMES).filter((k) => km[k] > 0);
  if (!kinds.length) return "";
  const even = kinds.every((k) => km[k] === r.monthsComplete);
  if (even && kinds.length === Object.keys(DEAL_KIND_NAMES).length) return "";
  if (even) return `${kinds.map((k) => DEAL_KIND_NAMES[k]).join("·")}만`;
  return `${kinds.map((k) => `${DEAL_KIND_NAMES[k]} ${km[k]}`).join(" · ")}개월`;
}

/**
 * 연도별 요약 한 줄을 표의 칸으로 (J5-055): 필지 카드·이력 화면의 표가 쓴다. 값·판정은 이 폰의 글 한 줄(yearSummaryRow)과 같고, 규칙은 PC db parcels-years 와 같다.
 * PC 글(years_text)은 같은 필지의 마지막 거래 금액·날짜와 같은 기준연도 값 바뀜을 더 적으므로 글끼리는 같지 않다. 대조는 칸의 값으로 한다.
 * price 는 원/㎡ 글(자료가 없으면 null), delta 는 전년 대비, tx 는 같은 필지 거래 수(수집했을 때) 또는 모르는 사유,
 * txNotes 는 번지대·연결 수와 수집 개월, owner 는 그 연도의 소유 변동일(월-일) 또는 "없음"·"자료 없음"(소유 칸이 없으면 null).
 */
export function yearSummaryCells(r) {
  const counted = r.tx === "counted";
  const txNotes = [];
  if (counted && r.prefix) txNotes.push(`번지대 ${r.prefix}`);
  if (counted && r.linked) txNotes.push(`연결 ${r.linked}`);
  if (counted && r.monthsComplete != null) txNotes.push(`수집 ${r.monthsComplete}/12개월`);
  if (kindsOnlyText(r)) txNotes.push(kindsOnlyText(r));
  if (r.tx === "incomplete" && r.monthsAny) txNotes.push(`${r.monthsAny}개월 시도`);   // PC 의 "거래 수집 실패·부분만 (n개월 시도)" 와 같은 값
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

// ---- 지도의 실거래 표시 (J5-063) ----
// 지번이 온전한 거래는 그 필지 위에, 지번 일부가 가려진 거래는 번지대(같은 법정동·같은 가린 지번)마다 묶어 후보 필지들 가운데에 둔다.
// 번지대 묶음은 어느 필지인지 확정하지 않는다 (AGENTS "마스킹된 거래를 추정으로 특정 PNU 에 확정하지 않는다"). 불러온 필지에 닿지 않는 거래는 수만 센다.

/** 1평 = 3.305785㎡ (단가 표시용). */
export const PYEONG_M2 = 3.305785;
export const DEAL_MODES = Object.freeze({ total: "총액", land: "대지 평당", floor: "연면적 평당" });
export const DEAL_YEARS = Object.freeze([1, 3, 5, 0]);   // 0 = 전체

/** 거래의 수집 종류 (J5-066). 종류 칸이 없는 이전 파일은 상업·업무용만 담았다 (J5-065 전 수집기는 상업·업무용뿐). */
export const dealKindOf = (t) => (knownKind(t?.deal_kind) ? t.deal_kind : "nrg");

/** 고를 수 있는 종류 거르기: all 은 전체, 나머지는 DEAL_KIND_NAMES 의 이름 */
export const DEAL_KIND_FILTERS = Object.freeze(["all", ...Object.keys(DEAL_KIND_NAMES)]);

/** kind 종류의 거래만 (all 이면 전부) */
export function dealsOfKind(transactions, kind = "all") {
  return kind === "all" ? (transactions ?? []) : (transactions ?? []).filter((t) => dealKindOf(t) === kind);
}

/** 거래 목록에 든 종류 (DEAL_KIND_NAMES 순). 둘 이상일 때만 화면에 종류 고르기를 보인다 */
export function dealKindsPresent(transactions) {
  const seen = new Set((transactions ?? []).map(dealKindOf));
  return Object.keys(DEAL_KIND_NAMES).filter((k) => seen.has(k));
}
const DEAL_KIND_RULES = [["근린", "근린"], ["업무", "업무"], ["숙박", "숙박"], ["판매", "판매"], ["의료", "의료"], ["교육", "교육"], ["공장", "공장"], ["창고", "창고"],
  ["다가구", "다가구"], ["단독", "단독"], ["주택", "주택"]];

/** 거래 종류의 짧은 이름 (지도 표시용). 토지 매매(deal_kind land)는 토지, 건물 용도 → 근린·업무·숙박·단독·다가구 등, 건물 정보 없이 대지만 있으면 토지,
 *  단독·다가구 매매인데 주택유형이 없으면 주택, 모르면 건물. */
export function dealKind(t) {
  if (t?.deal_kind === "land") return "토지";
  const s = [t?.building_use, t?.building_kind].filter((x) => typeof x === "string").join(" ");
  for (const [k, label] of DEAL_KIND_RULES) if (s.includes(k)) return label;
  if (t?.deal_kind === "sh") return "주택";
  if (!t?.building_use && !Number.isFinite(t?.building_area_m2) && Number.isFinite(t?.plottage_area_m2)) return "토지";
  return "건물";
}

/** 총액 또는 평당 단가(원). 지분 거래는 단가를 내지 않는다(면적이 거래 지분과 맞지 않을 수 있다). 금액·면적이 없으면 null. */
export function unitPrice(t, mode = "total") {
  if (!Number.isFinite(t?.amount_krw)) return null;
  if (mode === "total") return t.amount_krw;
  if (t.share_deal) return null;
  const a = mode === "land" ? t.plottage_area_m2 : mode === "floor" ? t.building_area_m2 : null;
  if (!Number.isFinite(a) || a <= 0) return null;
  return t.amount_krw / (a / PYEONG_M2);
}

/** 지도·목록의 값 글: 총액 "18.5억", 단가 "4,200만/평". 낼 수 없으면 사유. */
export function dealValueText(t, mode = "total") {
  const v = unitPrice(t, mode);
  if (v !== null) return mode === "total" ? fmtKrw(v) : `${fmtKrw(Math.round(v))}/평`;
  if (!Number.isFinite(t?.amount_krw)) return "금액 미확인";
  if (t.share_deal) return "지분 거래";
  if (mode === "floor" && t.deal_kind === "land") return "토지 거래 (건물 없음)";
  return mode === "land" ? (t.deal_kind === "land" ? "토지 면적 미확인" : "대지 면적 미확인") : "연면적 미확인";
}

const dealDate = (t) => t.deal_date ?? `${t.deal_ymd.slice(0, 4)}-${t.deal_ymd.slice(4, 6)}`;
const byDealDesc = (a, b) => (dealDate(a) < dealDate(b) ? 1 : dealDate(a) > dealDate(b) ? -1 : a.transaction_id < b.transaction_id ? 1 : -1);

/** since(YYYY-MM-DD) 이후 거래만 (월만 아는 거래는 그달 1일로 본다). since 가 없으면 전부. */
export function dealsSince(transactions, since = null) {
  return (transactions ?? []).filter((t) => !since || `${dealDate(t)}-01`.slice(0, 10) >= since);
}

/** years(0=전체)와 기준일(YYYY-MM-DD)로 since 를 만든다. */
export function dealSince(years, today) {
  if (!years || !/^\d{4}-\d{2}-\d{2}$/.test(String(today ?? ""))) return null;
  return `${Number(today.slice(0, 4)) - years}${today.slice(4)}`;
}

/**
 * 거래를 지도에 놓는다. { parcels: [{pnu, feature, txs}], groups: [{key, emd, jibun, features, txs}], outside }.
 * parcels: 지번이 온전히 같은 필지(jibunMatch exact). groups: 가린 지번이 닿는 필지가 하나 이상인 번지대(필지 미확정). outside: 불러온 필지에 닿지 않는 거래 수.
 * 각 txs 는 최근 것부터.
 */
export function placeDeals(transactions, features) {
  const byPnu = new Map(), groups = new Map();
  let outside = 0;
  for (const t of transactions ?? []) {
    let exact = null; const prefix = [];
    for (const f of features ?? []) {
      const m = jibunMatch(t, f.properties);
      if (m === "exact") { exact = f; break; }
      if (m === "prefix") prefix.push(f);
    }
    if (exact) {
      if (!byPnu.has(exact.id)) byPnu.set(exact.id, { pnu: exact.id, feature: exact, txs: [] });
      byPnu.get(exact.id).txs.push(t);
    } else if (prefix.length) {
      const key = `${t.lawd_cd ?? ""}|${t.emd_name}|${t.jibun}`;
      if (!groups.has(key)) groups.set(key, { key, emd: t.emd_name, jibun: t.jibun, features: prefix, txs: [] });
      groups.get(key).txs.push(t);
    } else {
      outside += 1;
    }
  }
  const out = (m) => [...m.values()].map((v) => ({ ...v, txs: v.txs.sort(byDealDesc) }));
  return { parcels: out(byPnu), groups: out(groups), outside };
}

/** 번지대·주변 거래 목록의 둘째 줄: 대지·연면적과 평당 단가, 건축연도, 지분 거래. 값이 없으면 그 칸을 뺀다. */
export function dealDetailText(t) {
  const parts = [];
  const land = t?.deal_kind === "land";
  if (Number.isFinite(t?.plottage_area_m2)) parts.push(`${land ? "토지" : "대지"} ${t.plottage_area_m2}㎡ (${dealValueText(t, "land")})`);
  if (land && t.land_category) parts.push(`지목 ${t.land_category}`);
  if (land && t.land_use) parts.push(t.land_use);
  if (Number.isFinite(t?.building_area_m2)) parts.push(`연면적 ${t.building_area_m2}㎡ (${dealValueText(t, "floor")})`);
  if (Number.isInteger(t?.build_year)) parts.push(`${t.build_year}년 건축`);
  if (t?.share_deal) parts.push("지분 거래");
  return parts.join(" · ");
}

// ---- 주변 사례와 가격 적정성 (J5-064) ----
// 같은 시군구·같은 법정동의 거래를 기간 안에서 모아 평당 단가의 중간값·범위를 보이고, 지번이 온전해 필지가 정해지는 거래는
// 그 거래 연도의 그 필지 공시지가로 "대지 ㎡당 거래가 ÷ 공시지가" 배율을 구한다. 이 필지의 공시지가에 배율 중간값을 곱한 값은 참고 추정이다
// (사례 수·기간을 함께 적고 감정평가가 아니라고 밝힌다. AGENTS: 실거래·추정을 분리하고 근거·시점을 잇는다).

/** 참고 추정을 내는 데 필요한 배율 사례 수 (데이터 사전에 없어 정한 값). */
export const COMP_MIN_RATIOS = 3;
/** 필지 카드에 보이는 주변 사례 수. */
export const COMP_LIST_MAX = 5;

export function median(values) {
  const v = values.filter(Number.isFinite).slice().sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

/** 그 연도의 공시지가(원/㎡)와 기준연도: 기준연도가 year 이하인 값 가운데 가장 늦은 것. 더 늦은 해의 값으로 거슬러 쓰지 않는다. 없으면 null. */
export function priceForYear(feature, year) {
  const p = feature?.properties ?? {};
  const pts = priceTrend(p.attrs, p.attrs_history).filter((q) => Number.isInteger(q.year) && q.year <= year);
  if (!pts.length) return null;
  const q = pts.reduce((a, b) => (b.year > a.year || (b.year === a.year && (b.month ?? 0) >= (a.month ?? 0)) ? b : a));
  return { price: q.price, year: q.year };
}

/** 가장 최근 기준연도의 공시지가. 없으면 null. */
export function latestPrice(feature) {
  return priceForYear(feature, 9999);
}

const dealYear = (t) => Number(String(t.deal_date ?? t.deal_ymd).slice(0, 4));

/**
 * 이 필지의 주변 사례. feature 의 시군구(emd_code 앞 5자리)·법정동 이름과 같은 거래만, since 이후.
 * { comps: [{t, where, landUnit, floorUnit, ratio, priceYear, mine}], count, land: {n, median, min, max}, floor: {n, median, min, max}, ratio: {n, median}, reference }.
 * priceYear 는 배율에 쓴 공시지가의 실제 기준연도(거래 연도와 다를 수 있다, 리뷰 반영 PR #111).
 * landUnit·floorUnit 은 평당 원(지분 거래·면적 없음은 null). ratio 는 지번이 온전히 닿는 필지의 그 거래 연도 공시지가가 있을 때만.
 * reference: { unit(평당 원), perM2, price, priceYear, ratio, n } 또는 { missing: 사유 }.
 */
export function nearbyComps(feature, transactions, features, { since = null } = {}) {
  const p = feature?.properties ?? {};
  const lawd = typeof p.emd_code === "string" ? p.emd_code.slice(0, 5) : null;
  const same = dealsSince(transactions, since).filter((t) => t.emd_name === p.emd_name && (!lawd || !t.lawd_cd || t.lawd_cd === lawd)).sort(byDealDesc);
  const comps = same.map((t) => {
    let exact = null;
    for (const f of features ?? []) if (jibunMatch(t, f.properties) === "exact") { exact = f; break; }
    const pr = exact ? priceForYear(exact, dealYear(t)) : null;
    const perM2 = !t.share_deal && Number.isFinite(t.amount_krw) && Number.isFinite(t.plottage_area_m2) && t.plottage_area_m2 > 0 ? t.amount_krw / t.plottage_area_m2 : null;
    const ok = perM2 !== null && pr && pr.price > 0;
    return { t, where: `${t.emd_name ?? ""} ${t.jibun ?? "지번 미공개"}${t.jibun_masked ? " 번지대" : ""}`.trim(), landUnit: unitPrice(t, "land"), floorUnit: unitPrice(t, "floor"),
             ratio: ok ? perM2 / pr.price : null, priceYear: ok ? pr.year : null, mine: exact?.id === feature?.id };
  });
  const lands = comps.map((c) => c.landUnit).filter(Number.isFinite);
  const floors = comps.map((c) => c.floorUnit).filter(Number.isFinite);
  const ratios = comps.map((c) => c.ratio).filter(Number.isFinite);
  const r = median(ratios);
  const own = latestPrice(feature);
  let reference;
  if (ratios.length < COMP_MIN_RATIOS) reference = { missing: `공시지가와 견줄 수 있는 사례가 ${ratios.length}건이라 참고값을 내지 않습니다 (${COMP_MIN_RATIOS}건부터)` };
  else if (!own) reference = { missing: "이 필지의 공시지가 자료가 없어 참고값을 내지 않습니다" };
  else reference = { perM2: own.price * r, unit: own.price * r * PYEONG_M2, price: own.price, priceYear: own.year, ratio: r, n: ratios.length };
  return { comps, count: comps.length, land: { n: lands.length, median: median(lands), min: lands.length ? Math.min(...lands) : null, max: lands.length ? Math.max(...lands) : null },
           floor: { n: floors.length, median: median(floors), min: floors.length ? Math.min(...floors) : null, max: floors.length ? Math.max(...floors) : null }, ratio: { n: ratios.length, median: r }, reference };
}

// ---- 주변 사례 추이 (J5-067): 같은 동 거래의 대지 평당 단가(점)와 이 필지 공시지가의 평당 환산(계단선)을 한 축(원/평)에 ----

/** 날짜(YYYY-MM-DD) → 연도 소수 (2024-07-01 ≈ 2024.5). 월만 알면 그달 가운데로 둔다. */
export function yearFrac(date) {
  const [y, m = 7, d = 15] = String(date).split("-").map(Number);
  return y + (m - 1) / 12 + (d - 1) / 365;
}

/** lo~hi 를 1·2·5 단계의 눈금 n개 남짓으로 덮는다 → { lo, hi, ticks } (lo 는 0 아래로 내려가지 않는다) */
export function niceTicks(lo, hi, n = 3) {
  const span = hi - lo || Math.abs(hi) || 1;
  const raw = span / n;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((k) => k * p).find((s) => s >= raw);
  const a = Math.max(0, Math.floor(lo / step) * step), b = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = a; v <= b + step / 2; v += step) ticks.push(Math.round(v));
  return { lo: a, hi: Math.max(b, a + step), ticks };
}

/**
 * 주변 사례 추이 그래프의 자료 (J5-067). c 는 nearbyComps 결과, feature 는 이 필지.
 * points: 대지 평당 단가가 있는 사례 [{x, y, date, kind, mine, where, id}] (지분·면적 없음은 nearbyComps 처럼 뺀다).
 * steps: 이 필지 공시지가(원/㎡ × 평)를 기준연도 1월 1일부터 다음 기준연도까지 [{x0, x1, y, year, price}] (기간 안만).
 * 대지 평당 단가가 있는 사례가 없으면 null (공시지가 선만으로는 그리지 않는다: 연도별 표에 있다).
 */
export function compsChartData(c, feature, { since = null, today = null } = {}) {
  const points = (c?.comps ?? []).filter((x) => Number.isFinite(x.landUnit)).map((x) => {
    const date = x.t.deal_date ?? `${x.t.deal_ymd.slice(0, 4)}-${x.t.deal_ymd.slice(4, 6)}`;
    return { x: yearFrac(date), y: x.landUnit, date, kind: dealKind(x.t), mine: !!x.mine, where: x.where, id: x.t.transaction_id };
  }).sort((a, b) => a.x - b.x);
  if (!points.length) return null;
  const xs = points.map((p) => p.x);
  let x0 = since ? yearFrac(since) : Math.floor(Math.min(...xs));
  let x1 = today ? Math.max(yearFrac(today), Math.max(...xs)) : Math.ceil(Math.max(...xs) + 1e-9);
  x0 = Math.min(x0, Math.min(...xs));
  if (x1 - x0 < 1) x0 = x1 - 1;
  const p = feature?.properties ?? {};
  const trend = priceTrend(p.attrs, p.attrs_history).filter((q) => Number.isInteger(q.year) && Number.isFinite(q.price)).sort((a, b) => a.year - b.year);
  const steps = [];
  trend.forEach((q, i) => {
    const a = Math.max(q.year, x0), b = Math.min(trend[i + 1]?.year ?? Infinity, x1);
    if (a < b) steps.push({ x0: a, x1: b, y: q.price * PYEONG_M2, year: q.year, price: q.price });
  });
  const ys = [...points.map((q) => q.y), ...steps.map((s) => s.y)];
  const yt = niceTicks(Math.min(...ys), Math.max(...ys), 4);
  const yearsSpan = Math.floor(x1) - Math.ceil(x0);
  const every = yearsSpan > 12 ? 5 : yearsSpan > 6 ? 2 : 1;
  const xTicks = [];
  for (let y = Math.ceil(x0); y <= x1; y++) if (y % every === 0) xTicks.push(y);
  return { points, steps, x: [x0, x1], y: [yt.lo, yt.hi], yTicks: yt.ticks, xTicks };
}

// ---- 같은 동 연도별 시세 (J5-071): 같은 동 거래를 달력 연도로 묶어 대지 평당 중간값과 거래 수 ----
// 연도별 중간값과 사례 수는 실거래 신고 자료의 집계이며 추세선·예측은 만들지 않는다 (ADR-25 보정).

/** 연도별 시세에서 시작·최근 변동을 적는 데 필요한 그해 대지 평당 사례 수 (데이터 사전에 없어 정한 값, 참고 추정의 배율 사례 수와 같다). */
export const TREND_MIN_CASES = COMP_MIN_RATIOS;

/**
 * 같은 동 거래의 연도별 시세 (J5-071). c 는 nearbyComps 결과(달력 연도 첫날부터 모은 것), txDoc 은 거래 파일(수집 개월), feature 는 이 필지.
 * from 은 첫 연도(주지 않으면 거래의 가장 이른 해), today 의 해까지 줄을 만든다. kind 는 고른 종류(수집 개월을 그 종류로 본다).
 * years: [{year, count, n, median, min, max, months, kinds, partial}] 오래된 해부터. count 는 그해 거래 수(지분·면적 없음 포함), n 은 대지 평당이 있는 사례 수(없으면 median 등 null).
 * counted 는 거래 수를 셀 수 있는지: 그해 거래가 있거나 그 종류를 받은 달이 있으면 "counted", 받은 달이 없으면 "not_collected", 수집 현황이 없는 파일이면 "unknown"
 *   (counted 가 아니면 count 0 은 0 건이 아니다, 리뷰 반영 PR #118).
 * months 는 이 시군구에서 그해 그 종류를 완전히 받은 개월(수집 현황이 없는 파일이면 null), kinds 는 모든 종류일 때 일부 종류만 받은 해의 메모, partial 은 today 의 해(오늘까지).
 * change: 대지 평당 사례가 TREND_MIN_CASES 건 이상인 가장 이른 해와 가장 늦은 해의 중간값 변동 { from, to, pct } 또는 { missing: 사유 }.
 * 거래가 없으면 null.
 */
export function compsByYear(c, txDoc, feature, { from = null, today = null, kind = "all" } = {}) {
  const comps = c?.comps ?? [];
  if (!comps.length) return null;
  const dealYears = comps.map((x) => dealYear(x.t));
  const thisYear = today ? Number(String(today).slice(0, 4)) : null;
  const y0 = Number.isInteger(from) ? Math.min(from, ...dealYears) : Math.min(...dealYears);
  const y1 = Math.max(...dealYears, ...(Number.isInteger(thisYear) ? [thisYear] : []));
  const p = feature?.properties ?? {};
  const lawd = typeof p.emd_code === "string" ? p.emd_code.slice(0, 5) : null;
  const hasCov = Array.isArray(txDoc?.coverage);
  const cov = new Map();
  if (hasCov) for (const r of txDoc.coverage) if (r.lawd_cd === lawd) cov.set(r.year, r);
  const years = [];
  for (let y = y0; y <= y1; y++) {
    const inYear = comps.filter((x) => dealYear(x.t) === y);
    const lands = inYear.map((x) => x.landUnit).filter(Number.isFinite);
    const r = cov.get(y);
    const months = !hasCov ? null : !r ? 0 : kind === "all" || !r.kind_months ? r.months_complete : r.kind_months[kind] ?? 0;
    const kinds = kind === "all" && r?.kind_months ? kindsOnlyText({ tx: "counted", kindMonths: r.kind_months, monthsComplete: r.months_complete }) : "";
    const counted = inYear.length || (months != null && months > 0) ? "counted" : months == null ? "unknown" : "not_collected";
    years.push({ year: y, count: inYear.length, counted, n: lands.length, median: median(lands), min: lands.length ? Math.min(...lands) : null, max: lands.length ? Math.max(...lands) : null,
                 months, kinds, partial: y === thisYear });
  }
  const q = years.filter((r) => r.n >= TREND_MIN_CASES);
  const change = q.length < 2 ? { missing: `대지 평당 사례가 ${TREND_MIN_CASES}건 이상인 해가 ${q.length}개라 시작·최근 변동을 적지 않습니다 (2개 해부터)` }
    : { from: q[0], to: q.at(-1), pct: ((q.at(-1).median - q[0].median) / q[0].median) * 100 };
  return { years, change };
}
