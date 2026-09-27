// 조회 파생본(.j5view.zip) 읽기 (J5-023, ADR-17). PC 의 `j5 db project` 가 만든 ZIP 을 폰에서 읽는다.
// 규칙은 PC 검사기(j5/db/viewpkg.py)와 같다: 허용 이름 목록, 경로 탈출·백슬래시·제어문자·절대경로 거절, 암호화·심볼릭 링크·중복 거절,
// 항목 수·선언 크기·실제 읽은 바이트 한도, manifest 의 해시·크기와 대조. 사진 항목은 읽지 않는다(폰에는 넣지 않음). 외부 통신 없음.
// STORED 는 그대로, DEFLATE 는 브라우저의 DecompressionStream("deflate-raw") 로 푼다.

import { crc32 } from "./zip.js";
import { sha256Hex } from "./hash.js";

export const VIEW_ENTRY = /^(manifest\.json|assets\.seed\.json|assets\.geojson|records\.jsonl|parcels\.geojson|transactions\.json|photos\/[0-9a-f]{64}\.(jpg|png|webp))$/;
export const VIEW_TEXT_FILES = ["assets.seed.json", "assets.geojson", "records.jsonl", "parcels.geojson", "transactions.json"];
/** 폰의 한도. 파생본은 누적 자료라 관측 패키지보다 크지만 폰 메모리에 한 번에 올리므로 PC 검사기보다 좁다. */
export const VIEW_LIMITS = Object.freeze({ file: 256_000_000, entries: 60_000, text: 64_000_000, total: 256_000_000 });

const SIG_LOCAL = 0x04034b50, SIG_CENTRAL = 0x02014b50, SIG_EOCD = 0x06054b50;
const METHOD_STORED = 0, METHOD_DEFLATE = 8;
const DEC = new TextDecoder("utf-8", { fatal: true });

export class UnzipError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

/** 항목 이름 안전성. PC 검사기(_check_view_entry)와 같은 규칙. */
export function checkEntryName(name) {
  if (name.startsWith("/") || /^[A-Za-z]:/.test(name)) throw new UnzipError("entry_absolute", `절대경로 항목: ${name}`);
  if (name.includes("\\") || Array.from(name).some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127)) throw new UnzipError("entry_bad_name", `백슬래시 또는 제어문자 포함 이름: ${name}`);
  const segs = name.split("/");
  if (segs.includes("..") || segs.includes(".")) throw new UnzipError("entry_traversal", `경로 탈출 항목: ${name}`);
  if (name.endsWith("/")) {
    if (name !== "photos/") throw new UnzipError("entry_dir_not_photos", `photos/ 외 디렉터리 항목: ${name}`);
    return;
  }
  if (!VIEW_ENTRY.test(name)) throw new UnzipError("entry_not_allowed", `파생본에 허용되지 않은 파일 이름: ${name}`);
}

/** 중앙 디렉터리를 읽어 항목 목록을 돌려준다. ZIP64·암호화·지원하지 않는 압축·심볼릭 링크·중복·한도 초과는 거절. */
export function parseCentralDirectory(bytes, limits = VIEW_LIMITS) {
  if (!(bytes instanceof Uint8Array)) throw new UnzipError("bad_input", "바이트 배열이 아님");
  if (bytes.length > limits.file) throw new UnzipError("zip_compressed_size", `ZIP 크기 ${bytes.length} 바이트가 한도 ${limits.file}를 넘음`);
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 65535); i--) {
    if (dv.getUint32(i, true) === SIG_EOCD) { eocd = i; break; }
  }
  if (eocd < 0) throw new UnzipError("zip_bad_file", "ZIP 파일이 아니거나 손상됨 (끝 레코드 없음)");
  const disk = dv.getUint16(eocd + 4, true), cdDisk = dv.getUint16(eocd + 6, true);
  const count = dv.getUint16(eocd + 10, true), cdSize = dv.getUint32(eocd + 12, true), cdOffset = dv.getUint32(eocd + 16, true);
  if (disk !== 0 || cdDisk !== 0 || count === 0xffff || cdOffset === 0xffffffff) throw new UnzipError("zip64", "ZIP64 또는 분할 ZIP 은 지원하지 않음");
  if (count > limits.entries) throw new UnzipError("zip_entry_count", `항목 수 ${count}가 한도를 넘음`);
  if (cdOffset + cdSize > eocd) throw new UnzipError("zip_bad_file", "중앙 디렉터리 위치가 파일 범위를 벗어남");
  const entries = [];
  const seen = new Set();
  let off = cdOffset, declared = 0;
  for (let i = 0; i < count; i++) {
    if (off + 46 > eocd || dv.getUint32(off, true) !== SIG_CENTRAL) throw new UnzipError("zip_bad_file", `중앙 디렉터리 항목 ${i} 손상`);
    const flags = dv.getUint16(off + 8, true), method = dv.getUint16(off + 10, true);
    const crc = dv.getUint32(off + 16, true), csize = dv.getUint32(off + 20, true), usize = dv.getUint32(off + 24, true);
    const nameLen = dv.getUint16(off + 28, true), extraLen = dv.getUint16(off + 30, true), commentLen = dv.getUint16(off + 32, true);
    const extAttr = dv.getUint32(off + 38, true), localOffset = dv.getUint32(off + 42, true);
    let name;
    try { name = DEC.decode(bytes.subarray(off + 46, off + 46 + nameLen)); } catch { throw new UnzipError("entry_bad_name", `항목 ${i} 이름이 UTF-8 이 아님`); }
    checkEntryName(name);
    if (flags & 0x1) throw new UnzipError("entry_encrypted", `암호화된 항목: ${name}`);
    if (method !== METHOD_STORED && method !== METHOD_DEFLATE) throw new UnzipError("entry_compression", `지원하지 않는 압축 방식 ${method}: ${name}`);
    if (((extAttr >>> 16) & 0xf000) === 0xa000) throw new UnzipError("entry_symlink", `심볼릭 링크 항목: ${name}`);
    if (csize === 0xffffffff || usize === 0xffffffff || localOffset === 0xffffffff) throw new UnzipError("zip64", `ZIP64 항목: ${name}`);
    if (seen.has(name)) throw new UnzipError("entry_duplicate_name", `중복 항목 이름: ${name}`);
    seen.add(name);
    declared += usize;
    if (declared > limits.total) throw new UnzipError("zip_declared_total", "선언된 해제 크기가 한도를 넘음");
    entries.push({ name, method, crc32: crc, compressedSize: csize, size: usize, localOffset, cdOffset: off, dir: name.endsWith("/") });
    off += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

async function inflateRaw(data, cap) {
  if (typeof DecompressionStream !== "function") throw new UnzipError("no_inflate", "이 브라우저는 DEFLATE 해제를 지원하지 않음");
  const ds = new DecompressionStream("deflate-raw");
  const writer = ds.writable.getWriter();
  const reader = ds.readable.getReader();
  const chunks = [];
  let total = 0;
  const pump = (async () => {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > cap) { await reader.cancel(); throw new UnzipError("entry_size", "항목 해제 크기가 선언보다 큼"); }
      chunks.push(value);
    }
  })();
  try {
    await writer.write(data);
    await writer.close();
  } catch (e) {
    if (e instanceof UnzipError) throw e;
    try { await pump; } catch (e2) { if (e2 instanceof UnzipError) throw e2; }
    throw new UnzipError("inflate", "DEFLATE 해제 실패: " + (e?.message || e));
  }
  await pump;
  const out = new Uint8Array(total);
  let p = 0;
  for (const c of chunks) { out.set(c, p); p += c.byteLength; }
  return out;
}

/** 항목 하나를 읽어 바이트로. 크기·CRC 를 대조한다. */
export async function readEntry(bytes, entry, limits = VIEW_LIMITS) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const off = entry.localOffset;
  if (off + 30 > bytes.length || dv.getUint32(off, true) !== SIG_LOCAL) throw new UnzipError("zip_bad_file", `로컬 헤더 손상: ${entry.name}`);
  const nameLen = dv.getUint16(off + 26, true), extraLen = dv.getUint16(off + 28, true);
  const start = off + 30 + nameLen + extraLen, end = start + entry.compressedSize;
  if (end > bytes.length) throw new UnzipError("zip_truncated", `항목이 파일 끝을 넘음: ${entry.name}`);
  if (entry.size > limits.text) throw new UnzipError("entry_size", `항목 ${entry.name} 의 크기 ${entry.size}가 한도 ${limits.text}를 넘음`);
  const raw = bytes.subarray(start, end);
  const data = entry.method === METHOD_STORED ? raw : await inflateRaw(raw, entry.size);
  if (data.byteLength !== entry.size) throw new UnzipError("entry_size", `항목 ${entry.name} 의 실제 크기(${data.byteLength})가 선언(${entry.size})과 다름`);
  if ((crc32(data) >>> 0) !== (entry.crc32 >>> 0)) throw new UnzipError("zip_crc", `항목 손상(CRC 불일치): ${entry.name}`);
  return data;
}

/**
 * 파생본 ZIP 전체를 읽는다. 반환 { manifest, files: Map(이름 → Uint8Array), photosSkipped }.
 * manifest.json 을 먼저 읽고, manifest 가 나열한 파일(사진 제외)을 읽어 바이트 수·sha256 을 대조한다. manifest 에 없는 항목이 있으면 거절.
 */
export async function readViewZip(bytes, limits = VIEW_LIMITS) {
  const entries = parseCentralDirectory(bytes, limits).filter((e) => !e.dir);
  const byName = new Map(entries.map((e) => [e.name, e]));
  const mEntry = byName.get("manifest.json");
  if (!mEntry) throw new UnzipError("manifest_missing", "manifest.json 이 없음");
  let manifest;
  try { manifest = JSON.parse(DEC.decode(await readEntry(bytes, mEntry, limits))); } catch (e) { if (e instanceof UnzipError) throw e; throw new UnzipError("manifest_json", "manifest.json 을 읽지 못함: " + (e?.message || e)); }
  if (!manifest || typeof manifest !== "object" || !Array.isArray(manifest.files)) throw new UnzipError("manifest_json", "manifest.files 가 없음");
  const listed = new Map();
  for (const f of manifest.files) {
    if (!f || typeof f.path !== "string" || !Number.isInteger(f.bytes) || !/^[0-9a-f]{64}$/.test(f.sha256 ?? "")) throw new UnzipError("manifest_json", "manifest.files 항목 형식");
    if (listed.has(f.path)) throw new UnzipError("manifest_json", `manifest.files 에 같은 경로가 두 번: ${f.path}`);
    listed.set(f.path, f);
  }
  for (const name of byName.keys()) if (name !== "manifest.json" && !listed.has(name)) throw new UnzipError("entry_unlisted", `manifest 에 없는 항목: ${name}`);
  const files = new Map();
  let photosSkipped = 0, total = mEntry.size;
  for (const [name, f] of listed) {
    if (name.startsWith("photos/")) { photosSkipped += 1; continue; } // 사진은 폰에 넣지 않는다 (읽지도 않음)
    const e = byName.get(name);
    if (!e) throw new UnzipError("file_missing", `manifest 가 나열한 파일이 ZIP 에 없음: ${name}`);
    if (e.size !== f.bytes) throw new UnzipError("file_bytes", `${name} 의 크기가 manifest 와 다름`);
    total += e.size;
    if (total > limits.total) throw new UnzipError("total_size_exceeded", "전체 해제 크기 한도 초과");
    const data = await readEntry(bytes, e, limits);
    if ((await sha256Hex(data)) !== f.sha256) throw new UnzipError("file_hash", `${name} 의 해시가 manifest 와 다름`);
    files.set(name, data);
  }
  return { manifest, files, photosSkipped };
}

export function decodeText(data) {
  return DEC.decode(data);
}
