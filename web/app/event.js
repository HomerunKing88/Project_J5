// R1 관측 이벤트 작성·정규화·검증. schemas/observation_event.schema.json 과 같은 규칙을 앱 쪽에서 미리 적용한다.
// 정규화 바이트 = 키 정렬·압축 구분자·비ASCII 보존 JSON + LF. tests/fixtures/make_packages.py 의 line_bytes 와 동일하다.
import { isUuid } from "./uuid.js";
import { EXT_MIME, SUPPORTED } from "./sniff.js";

export const CHANGE_STATUS = ["change_observed", "no_change", "hard_to_confirm"];
export const CHANGE_STATUS_LABEL = { change_observed: "변화 확인", no_change: "변화 없음", hard_to_confirm: "확인 어려움" };
export const PHOTO_TAGS = ["front", "ground_floor", "lease_ad", "construction", "road", "parking", "adjacency"];
export const PHOTO_TAG_LABEL = { front: "전면", ground_floor: "1층", lease_ad: "임대 광고", construction: "공사", road: "도로", parking: "주차", adjacency: "인접 관계" };
export const PHOTO_LIMIT = 20_000_000;
export const NOTE_MAX = 2000;
export const VIEWPOINT_MAX = 100;

const DT_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const SHA_RE = /^[0-9a-f]{64}$/;

export function canonicalize(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonicalize).join(",") + "]";
  return "{" + Object.keys(value).sort().map((k) => JSON.stringify(k) + ":" + canonicalize(value[k])).join(",") + "}";
}

export function lineBytes(event) {
  return new TextEncoder().encode(canonicalize(event) + "\n");
}

/** 관측 이벤트 객체를 만든다. 선택 필드는 null 로 명시한다. */
export function buildEvent({ eventId, assetId, observedAt, precision, deviceCreatedAt, routeVersionId = null,
                             frameVersionId = null, correctsEventId = null, changeStatus, note, attachments }) {
  return {
    event_id: eventId,
    record_type: "field_observation",
    asset_id: assetId,
    observed_at: observedAt,
    observed_at_precision: precision,
    device_created_at: deviceCreatedAt,
    route_version_id: routeVersionId,
    frame_version_id: frameVersionId,
    corrects_event_id: correctsEventId,
    payload: { change_status: changeStatus, note: note ?? null },
    attachment_refs: attachments.map((a) => {
      const ref = { sha256: a.sha256, path: `photos/${a.sha256}.${a.ext}`, mime: EXT_MIME[a.ext], bytes: a.bytes, tags: [...a.tags] };
      if (a.taken_at) ref.taken_at = a.taken_at;
      // 반복 촬영(연차 비교)용 선택 입력: 촬영 지점·방향·이전 사진 (데이터 사전 §6). 없으면 키를 넣지 않아 기존 이벤트 바이트가 바뀌지 않는다
      if (a.viewpoint_id) ref.viewpoint_id = a.viewpoint_id;
      if (a.heading_deg !== null && a.heading_deg !== undefined && a.heading_deg !== "") ref.heading_deg = Number(a.heading_deg);
      if (a.previous_photo_sha256) ref.previous_photo_sha256 = a.previous_photo_sha256;
      return ref;
    }),
  };
}

/** 스키마와 같은 규칙으로 검증. 문제 목록(빈 배열이면 유효)을 돌려준다. */
export function validateEvent(ev) {
  const errs = [];
  if (!isUuid(ev.event_id)) errs.push("event_id");
  if (ev.record_type !== "field_observation") errs.push("record_type");
  if (!isUuid(ev.asset_id)) errs.push("asset_id");
  if (ev.observed_at_precision === "datetime") {
    if (!DT_RE.test(ev.observed_at)) errs.push("observed_at: 시간대 있는 ISO 8601 필요");
  } else if (ev.observed_at_precision === "date") {
    if (!DATE_RE.test(ev.observed_at)) errs.push("observed_at: YYYY-MM-DD 필요");
  } else errs.push("observed_at_precision");
  if (!DT_RE.test(ev.device_created_at)) errs.push("device_created_at");
  for (const k of ["route_version_id", "frame_version_id", "corrects_event_id"]) {
    if (ev[k] !== null && !isUuid(ev[k])) errs.push(k);
  }
  if (ev.corrects_event_id && ev.corrects_event_id === ev.event_id) errs.push("corrects_event_id: 자기 자신");
  const p = ev.payload || {};
  if (!CHANGE_STATUS.includes(p.change_status)) errs.push("change_status");
  if (p.note !== null && (typeof p.note !== "string" || p.note.length > NOTE_MAX)) errs.push("note");
  if (!Array.isArray(ev.attachment_refs)) errs.push("attachment_refs");
  else {
    const seen = new Set();
    for (const r of ev.attachment_refs) {
      if (!SHA_RE.test(r.sha256)) errs.push("attachment sha256");
      if (r.path !== `photos/${r.sha256}.${extOf(r.path)}` || !SUPPORTED.has(extOf(r.path))) errs.push("attachment path");
      if (EXT_MIME[extOf(r.path)] !== r.mime) errs.push("attachment mime");
      if (!Number.isInteger(r.bytes) || r.bytes < 1 || r.bytes > PHOTO_LIMIT) errs.push("attachment bytes");
      if (!Array.isArray(r.tags) || r.tags.some((t) => !PHOTO_TAGS.includes(t)) || new Set(r.tags).size !== r.tags.length) errs.push("attachment tags");
      if ("viewpoint_id" in r && (typeof r.viewpoint_id !== "string" || !/\S/.test(r.viewpoint_id) || r.viewpoint_id.length > VIEWPOINT_MAX)) errs.push("attachment viewpoint_id");
      if ("heading_deg" in r && (typeof r.heading_deg !== "number" || !Number.isFinite(r.heading_deg) || r.heading_deg < 0 || r.heading_deg >= 360)) errs.push("attachment heading_deg");
      if ("previous_photo_sha256" in r && (!SHA_RE.test(r.previous_photo_sha256) || r.previous_photo_sha256 === r.sha256)) errs.push("attachment previous_photo_sha256");
      const key = canonicalize(r);
      if (seen.has(key)) errs.push("attachment 중복");
      seen.add(key);
    }
  }
  if (p.change_status === "change_observed") {
    const hasNote = typeof p.note === "string" && /\S/.test(p.note);
    if (!(ev.attachment_refs?.length > 0) && !hasNote) errs.push("변화 확인에는 사진 또는 설명이 필요");
  }
  return errs;
}

function extOf(path) {
  return String(path || "").split(".").pop();
}
