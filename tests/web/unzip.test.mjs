// unzip.js (J5-023, ADR-17): 조회 파생본 ZIP 읽기. 정상 파일(Python zipfile, DEFLATE)과 적대적 ZIP(경로 탈출·허용되지 않은 이름·중복·암호화·manifest 불일치·해시 불일치·손상)을
// Python 으로 만들어 검사한다. python3 이 없으면 건너뛴다. DOM 없음.
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseCentralDirectory, readEntry, readViewZip, checkEntryName, UnzipError, decodeText } from "../../web/app/unzip.js";

const ROOT = new URL("../../", import.meta.url).pathname;
const hasPy = spawnSync("python3", ["--version"]).status === 0;
const tmp = mkdtempSync(join(tmpdir(), "j5-unzip-"));

/** items: [[이름, 내용(문자열), 옵션{stored, encrypted}]] 를 Python zipfile 로 ZIP 으로. manifest 는 넘긴 그대로. */
function makeZip(name, items) {
  const spec = JSON.stringify(items);
  const py = `
import json, sys, zipfile, hashlib
items = json.loads(sys.argv[2])
with zipfile.ZipFile(sys.argv[1], "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for n, data, opt in items:
        info = zipfile.ZipInfo(n, date_time=(2026, 9, 27, 9, 0, 0))
        info.compress_type = zipfile.ZIP_STORED if opt.get("stored") else zipfile.ZIP_DEFLATED
        if opt.get("encrypted"): info.flag_bits |= 0x1
        if opt.get("symlink"): info.external_attr = (0o120777 << 16)
        zf.writestr(info, data.encode("utf-8"))
`;
  const out = join(tmp, name);
  const r = spawnSync("python3", ["-c", py, out, spec], { encoding: "utf8" });
  if (r.status !== 0) throw new Error(r.stderr);
  return new Uint8Array(readFileSync(out));
}

const sha = (s) => spawnSync("python3", ["-c", "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())", s], { encoding: "utf8" }).stdout.trim();
function manifestFor(files) {
  return JSON.stringify({ format: "j5view", projection_schema_version: "1.0.0", study_id: "s", data_mode: "synthetic", source_dataset_version: 1, generated_at: "2026-09-27T00:00:00Z",
    run_id: "00000000-0000-4000-8000-000000000000", scope_ids: [], counts: { assets: 0, located: 0, records: 0, attachments: 0, photos: 0 },
    files: files.map(([p, c]) => ({ path: p, bytes: Buffer.byteLength(c), sha256: sha(c) })) });
}
const BASE = [["assets.seed.json", "[]"], ["assets.geojson", '{"type":"FeatureCollection","features":[]}'], ["records.jsonl", ""]];

test("checkEntryName: 허용 이름만, 경로 탈출·절대경로·백슬래시·디렉터리 거절", () => {
  for (const ok of ["manifest.json", "records.jsonl", "photos/" + "a".repeat(64) + ".jpg", "photos/"]) checkEntryName(ok);
  for (const [bad, code] of [["../x.json", "entry_traversal"], ["/abs.json", "entry_absolute"], ["a\\b.json", "entry_bad_name"], ["x.json", "entry_not_allowed"], ["photos/x.jpg", "entry_not_allowed"], ["dir/", "entry_dir_not_photos"], ["C:evil", "entry_absolute"]]) {
    assert.throws(() => checkEntryName(bad), (e) => e instanceof UnzipError && e.code === code, bad);
  }
});

test("정상 ZIP: DEFLATE·STORED 항목을 읽고 CRC·크기·manifest 해시를 대조한다", { skip: hasPy ? false : "python3 없음" }, async () => {
  const files = [...BASE, ["transactions.json", JSON.stringify({ j5transactions: "1.0.0", count: 0, transactions: [] })]];
  const photo = "photos/" + "b".repeat(64) + ".png";
  const zip = makeZip("ok.zip", [["manifest.json", manifestFor([...files, [photo, "notread"]]), {}], ...files.map(([p, c], i) => [p, c, { stored: i % 2 === 0 }]), [photo, "notread", {}]]);
  const entries = parseCentralDirectory(zip);
  assert.deepEqual(entries.map((e) => e.name), ["manifest.json", ...files.map((f) => f[0]), "photos/" + "b".repeat(64) + ".png"]);
  assert.equal(decodeText(await readEntry(zip, entries[1])), "[]");
  const r = await readViewZip(zip);
  assert.equal(r.manifest.format, "j5view");
  assert.deepEqual([...r.files.keys()].sort(), files.map((f) => f[0]).sort());
  assert.equal(decodeText(r.files.get("transactions.json")), files[3][1]);
  assert.equal(r.photosSkipped, 1, "사진은 세기만 하고 읽지 않는다");
});

test("적대적 ZIP: 경로 탈출·허용되지 않은 이름·중복·암호화·심볼릭 링크·manifest 밖 항목·해시 불일치·손상", { skip: hasPy ? false : "python3 없음" }, async () => {
  const m = manifestFor(BASE);
  const expect = async (name, items, code) => {
    const zip = makeZip(name, items);
    await assert.rejects(readViewZip(zip), (e) => e instanceof UnzipError && e.code === code, `${name}: ${code}`);
  };
  await expect("trav.zip", [["manifest.json", m, {}], ["../evil.json", "{}", {}]], "entry_traversal");
  await expect("notallowed.zip", [["manifest.json", m, {}], ["evil.js", "x", {}]], "entry_not_allowed");
  // 암호화 플래그: Python writestr 은 플래그를 지우므로 중앙 디렉터리의 플래그 바이트를 직접 세운다
  const encZip = makeZip("enc.zip", [["manifest.json", m, {}], ...BASE.map(([p, c]) => [p, c, {}])]);
  const encEntry = parseCentralDirectory(encZip).find((e) => e.name === "records.jsonl");
  encZip[encEntry.cdOffset + 8] |= 0x1;
  await assert.rejects(readViewZip(encZip), (e) => e instanceof UnzipError && e.code === "entry_encrypted");
  await expect("link.zip", [["manifest.json", m, {}], ["records.jsonl", "", { symlink: true }]], "entry_symlink");
  await expect("unlisted.zip", [["manifest.json", m, {}], ...BASE.map(([p, c]) => [p, c, {}]), ["parcels.geojson", "{}", {}]], "entry_unlisted");
  await expect("missing.zip", [["manifest.json", m, {}], ["assets.seed.json", "[]", {}]], "file_missing");
  await expect("badhash.zip", [["manifest.json", m, {}], ["assets.seed.json", "[ ]", {}], ["assets.geojson", BASE[1][1], {}], ["records.jsonl", "", {}]], "file_bytes");
  const tampered = manifestFor([["assets.seed.json", "[1]"], BASE[1], BASE[2]]);
  await expect("hash.zip", [["manifest.json", tampered, {}], ["assets.seed.json", "[2]", {}], ["assets.geojson", BASE[1][1], {}], ["records.jsonl", "", {}]], "file_hash");
  await expect("nomanifest.zip", [["assets.seed.json", "[]", {}]], "manifest_missing");
  // 중복 항목: Python 은 경고만 내고 쓴다
  const dupPy = `
import zipfile, sys
with zipfile.ZipFile(sys.argv[1], "w") as zf:
    zf.writestr("manifest.json", "{}"); zf.writestr("manifest.json", "{}")
`;
  spawnSync("python3", ["-c", dupPy, join(tmp, "dup.zip")]);
  await assert.rejects(readViewZip(new Uint8Array(readFileSync(join(tmp, "dup.zip")))), (e) => e.code === "entry_duplicate_name");
  // 손상: 바이트 하나를 바꾸면 CRC 또는 해제 실패
  const good = makeZip("good.zip", [["manifest.json", m, {}], ...BASE.map(([p, c]) => [p, c, {}])]);
  const broken = new Uint8Array(good);
  const entries = parseCentralDirectory(broken);
  const e0 = entries.find((e) => e.name === "manifest.json");
  broken[e0.localOffset + 30 + "manifest.json".length + 3] ^= 0xff;
  await assert.rejects(readViewZip(broken), (e) => e instanceof UnzipError, "손상된 항목은 거절");
  assert.throws(() => parseCentralDirectory(new Uint8Array([1, 2, 3, 4])), (e) => e.code === "zip_bad_file");
  assert.throws(() => parseCentralDirectory(good, { file: 10, entries: 10, text: 10, total: 10 }), (e) => e.code === "zip_compressed_size");
  assert.throws(() => parseCentralDirectory(good, { file: 1e9, entries: 1, text: 1e9, total: 1e9 }), (e) => e.code === "zip_entry_count");
});

test("실제 파생본(가상 정본에서 j5 db project 로 만든 것)을 읽는다", { skip: hasPy ? false : "python3 없음" }, async () => {
  const r = spawnSync("python3", ["tests/fixtures/make_view.py", tmp], { cwd: ROOT, encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  const zip = new Uint8Array(readFileSync(join(tmp, "synthetic.j5view.zip")));
  const v = await readViewZip(zip);
  assert.equal(v.manifest.study_id, "j5-synthetic-study");
  assert.deepEqual([...v.files.keys()].sort(), ["assets.geojson", "assets.seed.json", "parcels.geojson", "records.jsonl", "transactions.json"]);
  assert.equal(JSON.parse(decodeText(v.files.get("assets.seed.json"))).length, 5);
});
