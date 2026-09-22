// node --test tests/web  (개발·CI 전용. 앱 실행에는 Node를 쓰지 않는다)
import test from "node:test";
import assert from "node:assert/strict";
import { sniffImage, SUPPORTED, EXT_MIME } from "../../web/app/sniff.js";

const bytes = (...xs) => new Uint8Array(xs.flatMap((x) => (typeof x === "string" ? [...x].map((c) => c.charCodeAt(0)) : x)));

test("jpeg/png/webp 판정은 j5/package/validate.py 의 sniff_image 와 같다", () => {
  assert.equal(sniffImage(bytes([0xff, 0xd8, 0xff, 0xe0])), "jpg");
  assert.equal(sniffImage(bytes([0x89], "PNG", [0x0d, 0x0a, 0x1a, 0x0a])), "png");
  assert.equal(sniffImage(bytes("RIFF", [0, 0, 0, 0], "WEBPVP8 ")), "webp");
  assert.equal(sniffImage(bytes([0x89], "PNG", [0x0d, 0x0a, 0x1a])), null);
  assert.equal(sniffImage(bytes("RIFF", [0, 0, 0, 0], "WAVE")), null);
  assert.equal(sniffImage(new Uint8Array()), null);
});

test("HEIC/HEIF 는 heic 로 판정하고 지원 목록에 없다", () => {
  for (const brand of ["heic", "heix", "hevc", "mif1", "msf1"]) {
    assert.equal(sniffImage(bytes([0, 0, 0, 0x18], "ftyp", brand)), "heic", brand);
  }
  assert.equal(sniffImage(bytes([0, 0, 0, 0x18], "ftyp", "isom")), null);
  assert.equal(SUPPORTED.has("heic"), false);
  assert.deepEqual([...SUPPORTED].sort(), ["jpg", "png", "webp"]);
  assert.deepEqual(EXT_MIME, { jpg: "image/jpeg", png: "image/png", webp: "image/webp" });
});

test("ArrayBuffer 입력도 받는다", () => {
  assert.equal(sniffImage(bytes([0xff, 0xd8, 0xff]).buffer), "jpg");
});
