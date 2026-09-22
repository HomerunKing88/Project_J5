import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { createHash } from "node:crypto";
import { canonicalize, lineBytes, buildEvent, validateEvent, PHOTO_TAGS, CHANGE_STATUS } from "../../web/app/event.js";
import { uuid4, isUuid } from "../../web/app/uuid.js";
import { isoWithOffset, fromDatetimeLocal, localDate } from "../../web/app/time.js";

const FIX = new URL("../fixtures/packages/", import.meta.url);

test("정규화 바이트가 fixture 의 행 바이트와 완전히 같다 (make_packages.py line_bytes 규약)", () => {
  let checked = 0;
  for (const dir of readdirSync(FIX)) {
    const raw = readFileSync(new URL(`${dir}/observations.jsonl`, FIX));
    for (const line of raw.toString("utf8").split("\n").filter(Boolean)) {
      const ev = JSON.parse(line);
      const bytes = lineBytes(ev);
      assert.equal(Buffer.from(bytes).toString("utf8"), line + "\n", dir);
      assert.equal(createHash("sha256").update(bytes).digest("hex"), createHash("sha256").update(line + "\n").digest("hex"));
      checked++;
    }
  }
  assert.ok(checked >= 9);
});

test("canonicalize 는 키를 정렬하고 비ASCII 를 보존한다", () => {
  assert.equal(canonicalize({ b: 1, a: { d: "한글", c: [null, true] } }), '{"a":{"c":[null,true],"d":"한글"},"b":1}');
});

test("buildEvent + validateEvent: 정상 이벤트는 오류가 없다", () => {
  const ev = buildEvent({
    eventId: uuid4(), assetId: uuid4(), observedAt: isoWithOffset(new Date(2026, 8, 22, 10, 15, 0)), precision: "datetime",
    deviceCreatedAt: isoWithOffset(), changeStatus: "change_observed", note: "1층 임대 광고",
    attachments: [{ sha256: "a".repeat(64), ext: "jpg", bytes: 1234, tags: ["front"] }],
  });
  assert.deepEqual(validateEvent(ev), []);
  assert.equal(ev.attachment_refs[0].path, `photos/${"a".repeat(64)}.jpg`);
  assert.equal(ev.attachment_refs[0].mime, "image/jpeg");
  assert.equal(ev.route_version_id, null);
  assert.deepEqual(Object.keys(ev).sort(), ["asset_id", "attachment_refs", "corrects_event_id", "device_created_at", "event_id",
    "frame_version_id", "observed_at", "observed_at_precision", "payload", "record_type", "route_version_id"]);
});

test("validateEvent 거절 규칙", () => {
  const base = () => buildEvent({
    eventId: uuid4(), assetId: uuid4(), observedAt: "2026-09-22", precision: "date",
    deviceCreatedAt: isoWithOffset(), changeStatus: "no_change", note: null, attachments: [],
  });
  assert.deepEqual(validateEvent(base()), []);
  const cases = [
    [(e) => { e.payload.change_status = "change_observed"; }, "변화 확인"],
    [(e) => { e.payload.change_status = "change_observed"; e.payload.note = "   "; }, "변화 확인"],
    [(e) => { e.observed_at = "2026-09-22T00:00:00+09:00"; }, "observed_at"],
    [(e) => { e.observed_at_precision = "datetime"; }, "observed_at"],
    [(e) => { e.device_created_at = "2026-09-22T10:00:00"; }, "device_created_at"],
    [(e) => { e.route_version_id = "route-1"; }, "route_version_id"],
    [(e) => { e.corrects_event_id = e.event_id; }, "자기 자신"],
    [(e) => { e.payload.note = "x".repeat(2001); }, "note"],
    [(e) => { e.attachment_refs = [{ sha256: "z".repeat(64), path: "photos/x.jpg", mime: "image/jpeg", bytes: 1, tags: [] }]; }, "attachment"],
    [(e) => { e.attachment_refs = [{ sha256: "a".repeat(64), path: `photos/${"a".repeat(64)}.heic`, mime: "image/heic", bytes: 1, tags: [] }]; }, "attachment"],
    [(e) => { e.attachment_refs = [{ sha256: "a".repeat(64), path: `photos/${"a".repeat(64)}.png`, mime: "image/png", bytes: 20000001, tags: [] }]; }, "attachment bytes"],
    [(e) => { e.attachment_refs = [{ sha256: "a".repeat(64), path: `photos/${"a".repeat(64)}.png`, mime: "image/png", bytes: 1, tags: ["selfie"] }]; }, "attachment tags"],
  ];
  for (const [mutate, expect] of cases) {
    const e = base();
    mutate(e);
    const errs = validateEvent(e);
    assert.ok(errs.some((m) => m.includes(expect)), `${expect}: ${errs}`);
  }
  assert.deepEqual(CHANGE_STATUS, ["change_observed", "no_change", "hard_to_confirm"]);
  assert.equal(PHOTO_TAGS.length, 7);
});

test("uuid4 형식", () => {
  for (let i = 0; i < 50; i++) {
    const u = uuid4();
    assert.ok(isUuid(u), u);
    assert.equal(u[14], "4");
    assert.ok("89ab".includes(u[19]));
  }
  assert.equal(isUuid("7C1F4A0E-3B2D-4E5F-8A9B-0C1D2E3F4A50"), false, "소문자만");
});

test("시각 형식", () => {
  const d = new Date(2026, 8, 22, 10, 15, 7);
  const iso = isoWithOffset(d);
  assert.match(iso, /^2026-09-22T10:15:07[+-]\d{2}:\d{2}$/);
  assert.equal(localDate(d), "2026-09-22");
  assert.match(fromDatetimeLocal("2026-09-22T10:15"), /^2026-09-22T10:15:00[+-]\d{2}:\d{2}$/);
  assert.equal(fromDatetimeLocal("bad"), null);
  assert.equal(fromDatetimeLocal("2026-13-40T10:15"), null);
});
