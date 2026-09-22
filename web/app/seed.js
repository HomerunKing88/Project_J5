// assets.seed.json 검증. schemas/assets_seed.schema.json 과 같은 규칙 (필수 필드·형식·좌표 범위·추가 필드 금지·ID 중복).
import { isUuid } from "./uuid.js";

const ALLOWED = new Set(["asset_id", "label", "location_point", "address", "data_mode", "created_at", "notes"]);
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

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
    if (!("location_point" in a) && !("address" in a)) errs.push(`${at} 위치점 또는 주소 필요`);
  });
  return errs;
}
