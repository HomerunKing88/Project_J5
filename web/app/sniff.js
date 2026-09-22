// 이미지 형식 판정 (매직바이트). j5/package/validate.py 의 sniff_image 와 같은 규칙에 HEIC 감지를 더했다.
// 반환: "jpg" | "png" | "webp" | "heic" | null

const HEIF_BRANDS = new Set(["heic", "heix", "hevc", "hevx", "mif1", "msf1", "heif"]);

function ascii(bytes, start, end) {
  let s = "";
  for (let i = start; i < end && i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return s;
}

export function sniffImage(bytes) {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "jpg";
  if (b.length >= 8 && b[0] === 0x89 && ascii(b, 1, 4) === "PNG" && b[4] === 0x0d && b[5] === 0x0a && b[6] === 0x1a && b[7] === 0x0a) return "png";
  if (b.length >= 12 && ascii(b, 0, 4) === "RIFF" && ascii(b, 8, 12) === "WEBP") return "webp";
  if (b.length >= 12 && ascii(b, 4, 8) === "ftyp" && HEIF_BRANDS.has(ascii(b, 8, 12))) return "heic";
  return null;
}

export const SUPPORTED = new Set(["jpg", "png", "webp"]);
export const EXT_MIME = { jpg: "image/jpeg", png: "image/png", webp: "image/webp" };
