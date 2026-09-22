import test from "node:test";
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { sha256Pure } from "../../web/app/sha256.js";

const ref = (b) => createHash("sha256").update(b).digest("hex");

test("표준 벡터", () => {
  assert.equal(sha256Pure(new Uint8Array()), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  assert.equal(sha256Pure(new TextEncoder().encode("abc")), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
});

test("길이 0..300 과 블록 경계, 무작위 입력이 node:crypto 와 같다", () => {
  for (let n = 0; n <= 300; n++) {
    const b = randomBytes(n);
    assert.equal(sha256Pure(new Uint8Array(b)), ref(b), `len ${n}`);
  }
  for (const n of [55, 56, 63, 64, 65, 119, 120, 128, 1000, 70000]) {
    const b = randomBytes(n);
    assert.equal(sha256Pure(new Uint8Array(b)), ref(b), `len ${n}`);
  }
});

test("ArrayBuffer 입력", () => {
  const b = randomBytes(77);
  assert.equal(sha256Pure(new Uint8Array(b).buffer), ref(b));
});
