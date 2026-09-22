// STORED ZIP 작성기 검증. 오라클은 Python zipfile 과 j5 inspect 다.
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, readdirSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import zlib from "node:zlib";
import { crc32, crc32Blob, dosDateTime, buildZip, zipSize, entryOverhead } from "../../web/app/zip.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const enc = (s) => new TextEncoder().encode(s);

async function toBytes(parts) {
  return new Uint8Array(await new Blob(parts).arrayBuffer());
}

function pyZipInfo(path) {
  const py = spawnSync("python3", ["-c", `
import json, sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
bad = z.testzip()
out = {"bad": bad, "entries": []}
for i in z.infolist():
    out["entries"].append({"name": i.filename, "size": i.file_size, "csize": i.compress_size, "crc": i.CRC,
                           "method": i.compress_type, "flags": i.flag_bits, "dt": list(i.date_time),
                           "data": z.read(i.filename).decode("utf-8", "replace") if i.file_size < 200 else None})
print(json.dumps(out))
`, path], { encoding: "utf8" });
  assert.equal(py.status, 0, py.stderr);
  return JSON.parse(py.stdout);
}

test("crc32 는 node zlib 과 같다 (조각 누적 포함)", async () => {
  for (const s of ["", "a", "abc", "한글 파일", "x".repeat(70000)]) {
    const b = enc(s);
    assert.equal(crc32(b), zlib.crc32(b) >>> 0, JSON.stringify(s.slice(0, 10)));
  }
  const big = new Uint8Array(1_000_003).map((_, i) => (i * 7) & 0xff);
  assert.equal(crc32(big.subarray(500_000), crc32(big.subarray(0, 500_000))), zlib.crc32(big) >>> 0);
  assert.equal(await crc32Blob(new Blob([big]), 4096), zlib.crc32(big) >>> 0);
});

test("dosDateTime", () => {
  assert.deepEqual(dosDateTime(new Date(2026, 8, 22, 10, 15, 7)), { date: ((46) << 9) | (9 << 5) | 22, time: (10 << 11) | (15 << 5) | 3 });
  assert.deepEqual(dosDateTime(new Date(1975, 0, 1)), { date: (1 << 5) | 1, time: 0 });
});

test("Python zipfile 이 열고 CRC·크기·이름·method·플래그·시각이 맞다", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "j5-zip-"));
  const a = enc('{"k":"v"}\n'), b = enc("한글 내용 ✓"), c = new Uint8Array(70000).map((_, i) => i & 0xff);
  const entries = [
    { name: "manifest.json", size: a.length, crc32: crc32(a), data: a },
    { name: "observations.jsonl", size: b.length, crc32: crc32(b), data: new Blob([b]) },
    { name: "photos/한글이름.png", size: c.length, crc32: crc32(c), data: c },
  ];
  const mtime = new Date(2026, 8, 22, 10, 15, 6);
  const { parts, size } = buildZip(entries, { mtime });
  const bytes = await toBytes(parts);
  assert.equal(bytes.length, size);
  assert.equal(size, zipSize(entries));
  assert.equal(entryOverhead(3), 82);
  const p = join(tmp, "t.zip");
  writeFileSync(p, bytes);
  const info = pyZipInfo(p);
  assert.equal(info.bad, null);
  assert.deepEqual(info.entries.map((e) => e.name), ["manifest.json", "observations.jsonl", "photos/한글이름.png"]);
  for (const [i, e] of info.entries.entries()) {
    assert.equal(e.size, entries[i].size);
    assert.equal(e.csize, entries[i].size);
    assert.equal(e.crc, entries[i].crc32);
    assert.equal(e.method, 0);
    assert.equal(e.flags & 0x800, 0x800);
    assert.equal(e.flags & 0x9, 0);
    assert.deepEqual(e.dt, [2026, 9, 22, 10, 15, 6]);
  }
  assert.equal(info.entries[0].data, '{"k":"v"}\n');
  assert.equal(info.entries[1].data, "한글 내용 ✓");
});

test("잘못된 항목 이름·중복은 거절", () => {
  const d = enc("x");
  for (const name of ["/abs", "a\\b", "../x", "", "photos/../x"]) {
    assert.throws(() => buildZip([{ name, size: 1, crc32: crc32(d), data: d }]), name || "(empty)");
  }
  assert.throws(() => buildZip([{ name: "a", size: 1, crc32: 0, data: d }, { name: "a", size: 1, crc32: 0, data: d }]), /중복/);
});

test("fixture valid 폴더를 ZIP 으로 만들면 j5 inspect 가 ok 를 낸다", async () => {
  const src = join(ROOT, "tests/fixtures/packages/valid");
  const files = [];
  const walk = (dir, rel) => {
    for (const n of readdirSync(dir).sort()) {
      const p = join(dir, n);
      const r = rel ? `${rel}/${n}` : n;
      if (statSync(p).isDirectory()) walk(p, r); else files.push(r);
    }
  };
  walk(src, "");
  const entries = files.map((r) => { const d = new Uint8Array(readFileSync(join(src, r))); return { name: r, size: d.length, crc32: crc32(d), data: d }; });
  const tmp = mkdtempSync(join(tmpdir(), "j5-zip-"));
  const p = join(tmp, "valid.j5field.zip");
  writeFileSync(p, await toBytes(buildZip(entries).parts));
  const py = spawnSync("python3", ["-m", "j5", "inspect", p, "--seed", join(ROOT, "tests/fixtures/assets.seed.synthetic.json"), "--json"], { cwd: ROOT, encoding: "utf8" });
  assert.equal(py.status, 0, py.stdout + py.stderr);
  const rep = JSON.parse(py.stdout);
  assert.equal(rep.verdict, "ok");
  assert.equal(rep.kind, "zip");
  assert.equal(rep.counts.events, 3);
});
