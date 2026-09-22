// 관측 패키지 내보내기 (J5-007). 대상 선택 → 묶음 나누기 → manifest → ZIP.
// 이벤트 행은 저장 시 고정한 record.line 을 그대로 쓴다 (재직렬화 금지). 사진은 참조된 것만, 이름순.
import { LIMITS } from "./limits.js";
import { buildZip, crc32, entryOverhead, nameBytes, EOCD_SIZE } from "./zip.js";
import { uuid4 } from "./uuid.js";
import { isoWithOffset } from "./time.js";
import { sha256Hex } from "./hash.js";

export const PACKAGE_SCHEMA_VERSION = "1.0.0";
const MANIFEST_NAME = "manifest.json";
const OBS_NAME = "observations.jsonl";
const MANIFEST_BASE = 400, MANIFEST_PER_FILE = 200; // manifest 크기 상한 추정
const ENC = new TextEncoder();

function photoPath(sha, ext) {
  return `photos/${sha}.${ext}`;
}

function overhead(name) {
  return entryOverhead(nameBytes(name).length);
}

/** 현재 설정과 같은 study_id·data_mode 의 기록만 고른다. */
export function selectRecords(records, { studyId, dataMode, includeExported = false }) {
  const sameContext = records.filter((r) => r.study_id === studyId && r.data_mode === dataMode);
  const selected = sameContext.filter((r) => includeExported || r.status !== "exported");
  selected.sort((a, b) => (a.saved_at < b.saved_at ? -1 : a.saved_at > b.saved_at ? 1 : a.event_id < b.event_id ? -1 : 1));
  return {
    selected,
    otherContext: records.length - sameContext.length,
    excludedExported: sameContext.length - selected.length,
  };
}

/**
 * 저장 순서대로 first-fit 으로 묶음을 나눈다. photoMeta: Map sha256 → { bytes, ext }.
 * 반환 { batches: [{ eventIds, shas, estBytes }], errors: [{ event_id, reason }] }.
 */
export function planBatches(records, photoMeta, limits = LIMITS) {
  const batches = [];
  const errors = [];
  const fixed = EOCD_SIZE + overhead(MANIFEST_NAME) + overhead(OBS_NAME) + MANIFEST_BASE;
  let cur = null;
  const open = () => { cur = { eventIds: [], shas: new Set(), zip: fixed, declared: MANIFEST_BASE, obs: 0, files: 1 }; };
  const close = () => { if (cur && cur.eventIds.length) batches.push({ eventIds: cur.eventIds, shas: [...cur.shas].sort(), estBytes: cur.zip }); cur = null; };
  for (const r of records) {
    const refs = r.event?.attachment_refs ?? [];
    let bad = null;
    for (const ref of refs) {
      const m = photoMeta.get(ref.sha256);
      if (!m) { bad = `사진 없음 ${ref.sha256.slice(0, 12)}…`; break; }
      if (m.bytes !== ref.bytes) { bad = `사진 크기 불일치 ${ref.sha256.slice(0, 12)}… (${m.bytes} ≠ ${ref.bytes})`; break; }
      if (photoPath(ref.sha256, m.ext) !== ref.path) { bad = `사진 경로 불일치 ${ref.path}`; break; }
    }
    if (bad) { errors.push({ event_id: r.event_id, reason: bad }); continue; }
    const lineLen = r.line.length;
    const tryAdd = (b) => {
      const newShas = refs.map((x) => x.sha256).filter((s, i, a) => !b.shas.has(s) && a.indexOf(s) === i);
      let addZip = lineLen, addDecl = lineLen, addFiles = 0;
      for (const s of newShas) {
        const m = photoMeta.get(s);
        addZip += m.bytes + overhead(photoPath(s, m.ext)) + MANIFEST_PER_FILE;
        addDecl += m.bytes + MANIFEST_PER_FILE;
        addFiles++;
      }
      const obs = b.obs + lineLen;
      const zip = b.zip + addZip + (b.obs === 0 ? MANIFEST_PER_FILE : 0);
      const declared = b.declared + addDecl + (b.obs === 0 ? MANIFEST_PER_FILE : 0);
      const photos = b.shas.size + newShas.length;
      if (obs > limits.photo || zip > limits.compressed || declared > limits.uncompressed || photos > limits.maxPhotos) return false;
      b.obs = obs; b.zip = zip; b.declared = declared; b.eventIds.push(r.event_id);
      for (const s of newShas) b.shas.add(s);
      return true;
    };
    if (!cur) open();
    if (tryAdd(cur)) continue;
    close(); open();
    if (tryAdd(cur)) continue;
    errors.push({ event_id: r.event_id, reason: "빈 묶음에도 들어가지 않음 (한도 초과)" });
    cur = null;
  }
  close();
  return { batches, errors };
}

export function buildManifest({ studyId, dataMode, packageId, createdAt, files }) {
  return {
    format: "j5field",
    schema_version: PACKAGE_SCHEMA_VERSION,
    study_id: studyId,
    package_id: packageId,
    created_at: createdAt,
    data_mode: dataMode,
    files,
  };
}

export function sanitizeStudyId(studyId) {
  const s = String(studyId ?? "").replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  return s || "study";
}

const pad = (n) => String(n).padStart(2, "0");

export function exportFilename({ studyId, createdAt, k, n, packageId }) {
  const d = createdAt;
  const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  return `${sanitizeStudyId(studyId)}-${stamp}-${k}of${n}-${packageId.slice(0, 8)}.j5field.zip`;
}

/**
 * 묶음 하나를 ZIP Blob 으로 만든다.
 * batch: planBatches 의 항목. recordsById: Map event_id → record. loadPhoto(sha) → { blob, ext } | undefined.
 * 사진은 다시 읽어 sha256·크기를 대조한다. 불일치면 예외 (부분 패키지를 만들지 않음).
 */
export async function buildPackage(batch, recordsById, { loadPhoto, studyId, dataMode, now = new Date(), packageId = uuid4(), k = 1, n = 1, limits = LIMITS }) {
  const lines = batch.eventIds.map((id) => {
    const r = recordsById.get(id);
    if (!r) throw new Error(`기록 없음 ${id}`);
    return r.line;
  });
  const obsLen = lines.reduce((s, l) => s + l.length, 0);
  const obs = new Uint8Array(obsLen);
  let off = 0;
  for (const l of lines) { obs.set(l, off); off += l.length; }
  const files = [{ path: OBS_NAME, bytes: obs.length, sha256: await sha256Hex(obs) }];
  const photoEntries = [];
  for (const sha of batch.shas) {
    const p = await loadPhoto(sha);
    if (!p) throw new Error(`사진 없음 ${sha}`);
    const buf = new Uint8Array(await p.blob.arrayBuffer());
    const digest = await sha256Hex(buf);
    if (digest !== sha) throw new Error(`사진 해시 불일치 ${sha.slice(0, 12)}…`);
    const name = photoPath(sha, p.ext);
    photoEntries.push({ name, size: buf.length, crc32: crc32(buf), data: buf });
    files.push({ path: name, bytes: buf.length, sha256: sha });
  }
  photoEntries.sort((a, b) => (a.name < b.name ? -1 : 1));
  files.sort((a, b) => (a.path === OBS_NAME ? -1 : b.path === OBS_NAME ? 1 : a.path < b.path ? -1 : 1));
  const createdAt = isoWithOffset(now);
  const manifestBytes = ENC.encode(JSON.stringify(buildManifest({ studyId, dataMode, packageId, createdAt, files }), null, 2) + "\n");
  if (manifestBytes.length > limits.photo || obs.length > limits.photo) throw new Error("manifest 또는 observations.jsonl 크기 초과");
  const entries = [
    { name: MANIFEST_NAME, size: manifestBytes.length, crc32: crc32(manifestBytes), data: manifestBytes },
    { name: OBS_NAME, size: obs.length, crc32: crc32(obs), data: obs },
    ...photoEntries,
  ];
  const { parts, size } = buildZip(entries, { mtime: now });
  if (size > limits.compressed) throw new Error(`ZIP 크기 ${size} 가 한도 ${limits.compressed} 초과`);
  const declared = entries.reduce((s, e) => s + e.size, 0);
  if (declared > limits.uncompressed) throw new Error("해제 크기 한도 초과");
  const blob = new Blob(parts, { type: "application/zip" });
  return {
    blob, bytes: size, packageId, createdAt,
    filename: exportFilename({ studyId, createdAt: now, k, n, packageId }),
    eventIds: [...batch.eventIds], photoCount: photoEntries.length, files,
  };
}
