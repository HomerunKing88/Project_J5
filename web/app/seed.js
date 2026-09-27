// assets.seed.json 검증. schemas/assets_seed.schema.json 과 같은 규칙 (필수 필드·형식·좌표 범위·추가 필드 금지·ID 중복).
import { isUuid } from "./uuid.js";

const ALLOWED = new Set(["asset_id", "label", "location_point", "address", "data_mode", "created_at", "notes", "tracking_status", "resolution_status"]);
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// 관심 단계 (데이터 사전 §2, J5-029 ADR-22). 파생본 시드가 정본 출력값으로 싣는다. 폰은 표시·필터만 하고 바꾸지 못한다 (PC `db asset-track`).
export const TRACKING_STATUSES = ["unreviewed", "background", "watch", "detailed_review", "purchase_ready", "hold", "excluded", "archived"];
export const TRACKING_LABEL = { unreviewed: "미검토", background: "배경", watch: "관찰", detailed_review: "상세 검토", purchase_ready: "매입 준비", hold: "보류", excluded: "제외", archived: "보관" };
/** 관찰목록: 정기적으로 다시 보는 단계 (릴리스 계획 §11 "배경 목록과 30~50개 정기 관찰목록 구분"). PC 의 `db watchlist` 와 같은 정의. */
export const WATCHLIST_STATUSES = ["watch", "detailed_review", "purchase_ready"];
export const RESOLUTION_STATUSES = ["pending", "confirmed"];
export const ASSET_FILTERS = ["all", "watchlist"];

export function isWatchlist(a) {
  return WATCHLIST_STATUSES.includes(a?.tracking_status);
}

/** 목록·지도에 보일 물건. 관심 단계가 없는 시드(수동 시드·기기 물건)는 "전체" 에서만 보인다. */
export function filterAssets(assets, filter) {
  return filter === "watchlist" ? assets.filter(isWatchlist) : assets;
}

/** 시드에 관심 단계가 하나라도 있으면 필터를 보일 만하다 (파생본 시드). */
export function hasTracking(assets) {
  return assets.some((a) => typeof a.tracking_status === "string");
}

function validDate(s) {
  if (typeof s !== "string" || !DATE_RE.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/** 문제 목록을 돌려준다. 빈 배열이면 유효. */
export function validateSeed(seed) {
  const errs = [];
  if (!Array.isArray(seed)) return ["배열이 아님"];
  if (seed.length === 0) return ["비어 있음"];
  const ids = new Set();
  seed.forEach((a, i) => {
    const at = `[${i}]`;
    if (a === null || typeof a !== "object" || Array.isArray(a)) return errs.push(`${at} 객체가 아님`);
    for (const k of Object.keys(a)) if (!ALLOWED.has(k)) errs.push(`${at} 허용되지 않은 필드 ${k}`);
    for (const k of ["asset_id", "label", "data_mode", "created_at", "notes"]) if (!(k in a)) errs.push(`${at} ${k} 누락`);
    if ("asset_id" in a) {
      if (!isUuid(a.asset_id)) errs.push(`${at} asset_id 가 UUID 가 아님`);
      else if (ids.has(a.asset_id)) errs.push(`${at} asset_id 중복 ${a.asset_id}`);
      ids.add(a.asset_id);
    }
    if ("label" in a && (typeof a.label !== "string" || a.label.length < 1)) errs.push(`${at} label`);
    if ("data_mode" in a && !["synthetic", "private_real"].includes(a.data_mode)) errs.push(`${at} data_mode`);
    if ("created_at" in a && !validDate(a.created_at)) errs.push(`${at} created_at 은 YYYY-MM-DD`);
    if ("notes" in a && a.notes !== null && typeof a.notes !== "string") errs.push(`${at} notes 는 문자열 또는 null`);
    if ("location_point" in a) {
      const p = a.location_point;
      const ok = Array.isArray(p) && p.length === 2 && typeof p[0] === "number" && typeof p[1] === "number"
        && Number.isFinite(p[0]) && Number.isFinite(p[1]) && p[0] >= -180 && p[0] <= 180 && p[1] >= -90 && p[1] <= 90;
      if (!ok) errs.push(`${at} location_point 는 [경도, 위도]`);
    }
    if ("address" in a && (typeof a.address !== "string" || a.address.length < 1)) errs.push(`${at} address`);
    if ("tracking_status" in a && !TRACKING_STATUSES.includes(a.tracking_status)) errs.push(`${at} tracking_status`);
    if ("resolution_status" in a && !RESOLUTION_STATUSES.includes(a.resolution_status)) errs.push(`${at} resolution_status`);
    if (!("location_point" in a) && !("address" in a)) errs.push(`${at} 위치점 또는 주소 필요`);
  });
  return errs;
}
