import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { selectRecords, planBatches, buildPackage, exportFilename, sanitizeStudyId, buildManifest } from "../../web/app/export.js";
import { buildEvent, lineBytes } from "../../web/app/event.js";
import { uuid4 } from "../../web/app/uuid.js";
import { LIMITS } from "../../web/app/limits.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const sha = (b) => createHash("sha256").update(b).digest("hex");

function png(seed) {
  const d = new Uint8Array(64 + seed);
  d.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  for (let i = 8; i < d.length; i++) d[i] = (i * 31 + seed) & 0xff;
  return d;
}

function makeRecord(i, photos, { studyId = "s1", dataMode = "synthetic", status = "saved" } = {}) {
  const ev = buildEvent({
    eventId: uuid4(), assetId: "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50", observedAt: "2026-09-22", precision: "date",
    deviceCreatedAt: "2026-09-22T10:00:00+09:00", changeStatus: "no_change", note: `기록 ${i}`,
    attachments: photos.map((p) => ({ sha256: sha(p), ext: "png", bytes: p.length, tags: ["front"] })),
  });
  return { event_id: ev.event_id, asset_id: ev.asset_id, event: ev, line: lineBytes(ev), study_id: studyId, data_mode: dataMode,
           status, exported_in: [], saved_at: `2026-09-22T10:00:${String(i).padStart(2, "0")}.000Z` };
}

test("selectRecords: 같은 맥락만, 기본은 미내보냄만, 저장 순서", () => {
  const rs = [makeRecord(3, []), makeRecord(1, [], { status: "exported" }), makeRecord(2, [], { studyId: "other" }), makeRecord(0, [])];
  const r = selectRecords(rs, { studyId: "s1", dataMode: "synthetic" });
  assert.deepEqual(r.selected.map((x) => x.event.payload.note), ["기록 0", "기록 3"]);
  assert.equal(r.otherContext, 1);
  assert.equal(r.excludedExported, 1);
  assert.equal(selectRecords(rs, { studyId: "s1", dataMode: "synthetic", includeExported: true }).selected.length, 3);
});

test("planBatches: 한도로 나뉘고, 공유 사진은 묶음마다 포함되며, 빈 묶음에도 못 들어가면 오류", () => {
  const p1 = png(1), p2 = png(2), p3 = png(3);
  const meta = new Map([[sha(p1), { bytes: p1.length, ext: "png" }], [sha(p2), { bytes: p2.length, ext: "png" }], [sha(p3), { bytes: p3.length, ext: "png" }]]);
  const rs = [makeRecord(0, [p1]), makeRecord(1, [p1, p2]), makeRecord(2, [p3]), makeRecord(3, [])];
  const all = planBatches(rs, meta);
  assert.equal(all.batches.length, 1);
  assert.deepEqual(all.batches[0].shas, [sha(p1), sha(p2), sha(p3)].sort());
  assert.deepEqual(all.errors, []);
  // 사진 2장 한도 → 같은 묶음 안의 공유 사진(p1)은 한 번만 세므로 {r0,r1} / {r2,r3} 두 묶음
  const byPhotos = planBatches(rs, meta, { ...LIMITS, maxPhotos: 2 });
  assert.equal(byPhotos.batches.length, 2);
  assert.deepEqual(byPhotos.batches[0].eventIds, [rs[0].event_id, rs[1].event_id]);
  assert.deepEqual(byPhotos.batches[1].eventIds, [rs[2].event_id, rs[3].event_id]);
  // 묶음이 갈리면 공유 사진은 양쪽에 모두 들어간다: r0[p1], r1[p2], r2[p3], r3[p1] 을 사진 2장 한도로 → {r0,r1} / {r2,r3}
  const shared = planBatches([makeRecord(0, [p1]), makeRecord(1, [p2]), makeRecord(2, [p3]), makeRecord(3, [p1])], meta, { ...LIMITS, maxPhotos: 2 });
  assert.equal(shared.batches.length, 2);
  assert.ok(shared.batches[0].shas.includes(sha(p1)) && shared.batches[1].shas.includes(sha(p1)));
  // ZIP 크기 한도가 아주 작으면 사진 있는 기록은 못 들어간다
  const tiny = planBatches(rs, meta, { ...LIMITS, compressed: 1500 });
  assert.ok(tiny.errors.some((e) => e.reason.includes("한도 초과")));
  // observations.jsonl 상한
  const obsCap = planBatches(rs, meta, { ...LIMITS, photo: 400 });
  assert.ok(obsCap.batches.length >= 2 || obsCap.errors.length > 0);
  // 누락·불일치 사진
  const missing = planBatches([makeRecord(9, [png(9)])], meta);
  assert.equal(missing.batches.length, 0);
  assert.match(missing.errors[0].reason, /사진 없음/);
  const wrong = planBatches([rs[0]], new Map([[sha(p1), { bytes: p1.length + 1, ext: "png" }]]));
  assert.match(wrong.errors[0].reason, /크기 불일치/);
});

test("buildPackage: j5 inspect 통과, 재내보내기 바이트 동일, 사진 검증", async () => {
  const p1 = png(1), p2 = png(2);
  const store = new Map([[sha(p1), { blob: new Blob([p1]), ext: "png" }], [sha(p2), { blob: new Blob([p2]), ext: "png" }]]);
  const meta = new Map([...store].map(([k, v]) => [k, { bytes: v.blob.size, ext: v.ext }]));
  const rs = [makeRecord(0, [p1]), makeRecord(1, [p1, p2]), makeRecord(2, [])];
  const byId = new Map(rs.map((r) => [r.event_id, r]));
  const { batches, errors } = planBatches(rs, meta);
  assert.deepEqual(errors, []);
  const now = new Date(2026, 8, 22, 10, 15, 6);
  const pkg = await buildPackage(batches[0], byId, { loadPhoto: (s) => store.get(s), studyId: "j5-synthetic-study", dataMode: "synthetic", now, k: 1, n: 1 });
  assert.equal(pkg.blob.size, pkg.bytes);
  assert.equal(pkg.photoCount, 2);
  assert.match(pkg.filename, /^j5-synthetic-study-20260922-101506-1of1-[0-9a-f]{8}\.j5field\.zip$/);
  assert.ok(pkg.bytes <= batches[0].estBytes, "실제 크기는 추정 이하");
  const tmp = mkdtempSync(join(tmpdir(), "j5-exp-"));
  const p = join(tmp, pkg.filename);
  writeFileSync(p, new Uint8Array(await pkg.blob.arrayBuffer()));
  const py = spawnSync("python3", ["-m", "j5", "inspect", p, "--seed", join(ROOT, "tests/fixtures/assets.seed.synthetic.json"), "--study-id", "j5-synthetic-study", "--json"], { cwd: ROOT, encoding: "utf8" });
  assert.equal(py.status, 0, py.stdout + py.stderr);
  const rep = JSON.parse(py.stdout);
  assert.equal(rep.verdict, "ok");
  assert.equal(rep.counts.events, 3);
  assert.equal(rep.counts.photos, 2);
  // 재내보내기: 같은 기록 → observations.jsonl 바이트 동일 (package_id 는 다름)
  const again = await buildPackage(batches[0], byId, { loadPhoto: (s) => store.get(s), studyId: "j5-synthetic-study", dataMode: "synthetic", now: new Date() });
  assert.notEqual(again.packageId, pkg.packageId);
  assert.equal(again.files[0].sha256, pkg.files[0].sha256, "observations.jsonl 해시 동일");
  // 사진 내용이 바뀌었으면 거절
  const tampered = new Map(store); tampered.set(sha(p1), { blob: new Blob([png(7)]), ext: "png" });
  await assert.rejects(buildPackage(batches[0], byId, { loadPhoto: (s) => tampered.get(s), studyId: "s", dataMode: "synthetic" }), /해시 불일치/);
  await assert.rejects(buildPackage(batches[0], byId, { loadPhoto: () => undefined, studyId: "s", dataMode: "synthetic" }), /사진 없음/);
});

test("파일명·manifest", () => {
  assert.equal(sanitizeStudyId("종로 5가/study id!"), "5-study-id");
  assert.equal(sanitizeStudyId(""), "study");
  assert.equal(sanitizeStudyId("a".repeat(60)).length, 40);
  const m = buildManifest({ studyId: "s", dataMode: "synthetic", packageId: "p", createdAt: "c", files: [] });
  assert.deepEqual(Object.keys(m), ["format", "schema_version", "study_id", "package_id", "created_at", "data_mode", "files"]);
  assert.equal(exportFilename({ studyId: "s", createdAt: new Date(2026, 0, 2, 3, 4, 5), k: 2, n: 3, packageId: "abcdef12-0000" }), "s-20260102-030405-2of3-abcdef12.j5field.zip");
});
