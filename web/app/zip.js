// STORED 전용 ZIP 작성기 (ADR-11). 외부 코드 없이 순수 JS 로 [local header + data]* → central directory → EOCD 를 만든다.
// 데이터 디스크립터·ZIP64·압축 없음. 헤더는 순수 함수로 계산하고 데이터는 Blob 파츠로 넘겨 메모리를 두 번 쓰지 않는다.
// 검증: tests/web/zip.test.mjs 에서 Python zipfile 과 j5 inspect 로 연다.

const TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();

const ENC = new TextEncoder();
const SIG_LOCAL = 0x04034b50, SIG_CENTRAL = 0x02014b50, SIG_EOCD = 0x06054b50;
const VERSION = 20, FLAG_UTF8 = 0x0800, METHOD_STORED = 0;

/** 조각 누적 CRC32. prev 는 이전 조각까지의 결과. */
export function crc32(bytes, prev = 0) {
  let c = (prev ^ 0xffffffff) >>> 0;
  for (let i = 0; i < bytes.length; i++) c = TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

export async function crc32Blob(blob, chunk = 4 << 20) {
  let c = 0;
  for (let off = 0; off < blob.size; off += chunk) {
    c = crc32(new Uint8Array(await blob.slice(off, off + chunk).arrayBuffer()), c);
  }
  return c;
}

/** DOS 시각 (로컬 구성요소, 2초 단위). 1980년 미만은 1980-01-01 00:00 으로. */
export function dosDateTime(date) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime()) || date.getFullYear() < 1980) return { date: (0 << 9) | (1 << 5) | 1, time: 0 };
  const y = Math.min(date.getFullYear(), 2107);
  return {
    date: ((y - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
    time: (date.getHours() << 11) | (date.getMinutes() << 5) | (date.getSeconds() >> 1),
  };
}

export function nameBytes(name) {
  return ENC.encode(name);
}

/** 항목 하나가 차지하는 헤더 바이트 (local 30 + central 46 + 이름 두 번). */
export function entryOverhead(nameLen) {
  return 76 + 2 * nameLen;
}

export const EOCD_SIZE = 22;

/** 항목 목록으로 최종 ZIP 크기를 계산한다 (STORED 이므로 정확). */
export function zipSize(entries) {
  return entries.reduce((s, e) => s + e.size + entryOverhead(nameBytes(e.name).length), EOCD_SIZE);
}

function u16(dv, off, v) { dv.setUint16(off, v & 0xffff, true); }
function u32(dv, off, v) { dv.setUint32(off, v >>> 0, true); }

/**
 * entries: [{ name, size, crc32, data: Uint8Array | Blob }]. 이름은 상대경로, '/' 구분.
 * 반환 { parts: (Uint8Array|Blob)[], size }. new Blob(parts) 로 파일을 만든다.
 */
export function buildZip(entries, { mtime = new Date() } = {}) {
  if (entries.length > 0xffff) throw new Error("항목 수 초과");
  const { date, time } = dosDateTime(mtime);
  const parts = [];
  const central = [];
  let offset = 0;
  const seen = new Set();
  for (const e of entries) {
    if (typeof e.name !== "string" || !e.name || e.name.startsWith("/") || e.name.includes("\\") || e.name.split("/").includes("..")) {
      throw new Error(`잘못된 항목 이름: ${e.name}`);
    }
    if (seen.has(e.name)) throw new Error(`중복 항목 이름: ${e.name}`);
    seen.add(e.name);
    const size = e.size >>> 0;
    if (size !== e.size || size > 0xffffffff) throw new Error(`항목 크기 범위 초과: ${e.name}`);
    const nb = nameBytes(e.name);
    const local = new Uint8Array(30 + nb.length);
    const dv = new DataView(local.buffer);
    u32(dv, 0, SIG_LOCAL); u16(dv, 4, VERSION); u16(dv, 6, FLAG_UTF8); u16(dv, 8, METHOD_STORED);
    u16(dv, 10, time); u16(dv, 12, date); u32(dv, 14, e.crc32); u32(dv, 18, size); u32(dv, 22, size);
    u16(dv, 26, nb.length); u16(dv, 28, 0);
    local.set(nb, 30);
    parts.push(local, e.data);
    const c = new Uint8Array(46 + nb.length);
    const cv = new DataView(c.buffer);
    u32(cv, 0, SIG_CENTRAL); u16(cv, 4, VERSION); u16(cv, 6, VERSION); u16(cv, 8, FLAG_UTF8); u16(cv, 10, METHOD_STORED);
    u16(cv, 12, time); u16(cv, 14, date); u32(cv, 16, e.crc32); u32(cv, 20, size); u32(cv, 24, size);
    u16(cv, 28, nb.length); u16(cv, 30, 0); u16(cv, 32, 0); u16(cv, 34, 0); u16(cv, 36, 0); u32(cv, 38, 0); u32(cv, 42, offset);
    c.set(nb, 46);
    central.push(c);
    offset += local.length + size;
  }
  const cdSize = central.reduce((s, c) => s + c.length, 0);
  if (offset + cdSize + EOCD_SIZE > 0xffffffff) throw new Error("ZIP64 필요: 4GB 초과");
  const eocd = new Uint8Array(EOCD_SIZE);
  const ev = new DataView(eocd.buffer);
  u32(ev, 0, SIG_EOCD); u16(ev, 4, 0); u16(ev, 6, 0); u16(ev, 8, entries.length); u16(ev, 10, entries.length);
  u32(ev, 12, cdSize); u32(ev, 16, offset); u16(ev, 20, 0);
  parts.push(...central, eocd);
  return { parts, size: offset + cdSize + EOCD_SIZE };
}
