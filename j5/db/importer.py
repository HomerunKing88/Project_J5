"""단방향 반영기 (J5-010). 관측 패키지(.j5field.zip 또는 폴더)를 정본에 반영한다. 데이터 사전 §3.2.

흐름: 검사(j5 inspect 와 같은 규칙, 정본의 물건 목록을 시드로) → 패키지 중복·대장 충돌·정정 대상 확인
      → 사진을 내용 해시 경로에 보관 → 기록·근거·첨부·수입 기록·dataset_version 을 한 트랜잭션으로 반영.
판정: applied(반영) / duplicate(새 이벤트 없음, 버전 유지) / held(같은 ID·다른 내용 또는 정정 대상 없음: 전체 보류)
      / rejected(검사 거절) / failed(파일·DB 실패). 어느 경우에도 입력 파일은 바꾸지 않는다.
DB 실패 시 이미 보관한 사진은 지우지 않고 '정리대기' 로 결과·로그에 남긴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from j5.db.store import Db
from j5.db.validate import RECORD_PAYLOAD_SCHEMAS, ValidationError, now_utc
from j5.package.limits import DEFAULT_LIMITS, Limits
from j5.package.reader import ContainerError, open_package
from j5.package.report import Report
from j5.package.validate import MANIFEST, OBSERVATIONS, event_hash, inspect_source

PHOTOS_DIR = "photos"
LOG_FILE = Path("logs") / "import.log"
EXIT_BY_OUTCOME = {"applied": 0, "duplicate": 0, "held": 2, "rejected": 1, "failed": 1}
_NS = uuid.UUID("6f1b8c2e-5a4d-4e3f-9b8a-7c6d5e4f3a2b")  # 결정적 ID 이름공간 (첨부·출처 문서)


@dataclass
class ImportResult:
    package: str
    package_sha256: str
    outcome: str = "failed"
    run_id: str | None = None
    package_id: str | None = None
    events_total: int = 0
    events_new: int = 0
    events_skipped: int = 0
    photos_stored: int = 0
    photos_reused: int = 0
    dataset_version_before: int = 0
    dataset_version_after: int = 0
    orphan_photos: list[str] = field(default_factory=list)  # 보관했으나 DB 미반영 (정리대기)
    findings: list[dict] = field(default_factory=list)      # 검사 결과 + 반영기 판단
    message: str = ""

    def add(self, level: str, code: str, path: str, message: str) -> None:
        self.findings.append({"level": level, "code": code, "path": path, "message": message})

    def has(self, code: str) -> bool:
        return any(f["code"] == code for f in self.findings)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, indent=2) + "\n"

    def to_text(self) -> str:
        label = {"applied": "반영됨", "duplicate": "새 이벤트 없음 (중복). 정본·버전 유지", "held": "보류. 정본 유지, 입력 원본 그대로",
                 "rejected": "거절. 정본 유지, 입력 원본 그대로", "failed": "실패. 정본 롤백, 입력 원본 그대로"}[self.outcome]
        lines = [f"패키지: {self.package}", f"sha256: {self.package_sha256}", f"판정: {self.outcome} - {label}",
                 f"이벤트 {self.events_total} (신규 {self.events_new}, 건너뜀 {self.events_skipped}) · 사진 보관 {self.photos_stored}, 재사용 {self.photos_reused}",
                 f"dataset_version {self.dataset_version_before} → {self.dataset_version_after}"]
        if self.run_id:
            lines.append(f"run_id: {self.run_id}")
        if self.message:
            lines.append(self.message)
        for f in self.findings:
            if f["level"] in ("reject", "hold", "warn", "error"):
                lines.append(f"  [{f['level']}] {f['code']} {f['path']}: {f['message']}")
        if self.orphan_photos:
            lines.append(f"정리대기 사진 {len(self.orphan_photos)}장 (DB 미반영, 자동 삭제하지 않음): " + ", ".join(self.orphan_photos))
        lines.append("이 결과는 백업·파생본 생성 완료를 뜻하지 않는다.")
        return "\n".join(lines) + "\n"


class ImportFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _zip_sha256(path: Path, limits: Limits) -> str:
    """ZIP 파일 바이트의 sha256. 한도(§3.3 compressed)를 넘는 파일은 읽지 않는다."""
    size = path.stat().st_size
    if size > limits.compressed:
        raise ContainerError("zip_compressed_size", str(path), f"ZIP 크기 {size} 바이트가 한도 {limits.compressed}를 넘음")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(limits.chunk), b""):
            h.update(b)
    return h.hexdigest()


def dir_identifier(digests: dict[str, str]) -> str:
    """풀어 놓은 폴더 패키지의 식별자: 항목 이름·내용 해시 목록의 sha256."""
    h = hashlib.sha256()
    for name in sorted(digests):
        h.update(name.encode("utf-8") + b"\0" + digests[name].encode() + b"\n")
    return h.hexdigest()


def _dir_metadata_id(path: Path) -> str:
    """내용을 읽지 않은 임시 식별자(이름·크기 목록). 검사에서 거절된 폴더 패키지의 수입 기록에만 쓴다."""
    h = hashlib.sha256(b"unverified-dir\n")
    for p in sorted(x for x in path.rglob("*") if x.is_file() or x.is_symlink()):
        h.update(p.relative_to(path).as_posix().encode("utf-8") + b"\0" + str(p.lstat().st_size).encode() + b"\n")
    return h.hexdigest()


def package_sha256(path: Path, limits: Limits = DEFAULT_LIMITS) -> str:
    """패키지 식별자. ZIP 은 파일 바이트, 폴더는 허용 항목만 한도 안에서 읽은 내용 해시 목록의 해시 (검사기의 항목 규칙·한도를 그대로 쓴다)."""
    path = Path(path)
    if path.is_file():
        return _zip_sha256(path, limits)
    with open_package(path, limits) as src:
        digests = {}
        for e in src.entries():
            cap = limits.manifest if e.name == MANIFEST else limits.photo if e.name.startswith("photos/") else limits.uncompressed
            digests[e.name] = src.read(e.name, cap)[1]
    return dir_identifier(digests)


def photo_rel_path(sha: str, ext: str) -> str:
    return f"{PHOTOS_DIR}/{sha[:2]}/{sha}.{ext}"


def _store_photo(data_home: Path, sha: str, ext: str, data: bytes) -> tuple[str, bool]:
    """내용 해시 경로에 보관한다. (rel_path, 새로 보관했는지). 같은 내용이 이미 있으면 재사용, 다른 내용이면 실패."""
    rel = photo_rel_path(sha, ext)
    final = data_home / rel
    if final.exists():
        if hashlib.sha256(final.read_bytes()).hexdigest() == sha:
            return rel, False
        raise ImportFailure("photo_store_conflict", f"{rel} 에 다른 내용의 파일이 있다. 손대지 않았다")
    part = final.with_name(final.name + ".part")
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        with open(part, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if hashlib.sha256(part.read_bytes()).hexdigest() != sha:
            raise ImportFailure("photo_store_verify", f"{rel} 를 다시 읽은 해시가 다르다")
        os.replace(part, final)
    except OSError as e:
        raise ImportFailure("photo_store_failed", f"{rel}: {e}") from e
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
    return rel, True


def _order_events(events: list[dict]) -> list[dict]:
    """정정 대상이 먼저 오도록 정렬한다 (같은 패키지 안의 supersedes 연결)."""
    by_id = {e["ev"]["event_id"]: e for e in events}
    ordered: list[dict] = []
    seen: set[str] = set()

    def visit(e: dict, stack: tuple[str, ...]) -> None:
        eid = e["ev"]["event_id"]
        if eid in seen:
            return
        c = e["ev"]["corrects_event_id"]
        if c in by_id and c not in seen:
            if c in stack:
                raise ImportFailure("corrects_cycle", f"정정 연결이 순환한다: {' -> '.join(stack + (c,))}")
            visit(by_id[c], stack + (eid,))
        seen.add(eid)
        ordered.append(e)

    for e in events:
        visit(e, ())
    return ordered


def _append_log(data_home: Path, entry: dict) -> None:
    try:
        p = data_home / LOG_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass  # 로그 실패가 반영 결과를 바꾸지 않는다. 화면 출력이 남는다.


def import_package(db: Db, path: Path, data_home: Path, *, limits: Limits = DEFAULT_LIMITS) -> ImportResult:
    path = Path(path)
    data_home = Path(data_home)
    started = db.now()
    result = ImportResult(package=path.name, package_sha256="")
    result.dataset_version_before = result.dataset_version_after = int(db.meta("dataset_version") or 0)
    report_dict: dict = {}
    stored_now: list[str] = []
    try:
        # ---- 0. 패키지를 한 번만 연다. 검사와 읽기가 같은 핸들·같은 스냅샷을 쓰고, 다시 읽는 항목은 검사 때의 해시와 대조한다 ----
        try:
            result.package_sha256 = _zip_sha256(path, limits) if path.is_file() else _dir_metadata_id(path)
            src = open_package(path, limits)
        except ContainerError as e:
            result.add("reject", e.code, e.path, e.message)
            if not result.package_sha256:
                result.package_sha256 = _dir_metadata_id(path) if path.is_dir() else hashlib.sha256(b"unhashed:" + path.name.encode("utf-8")).hexdigest()
            return _finish(db, data_home, result, "rejected", started, {"findings": result.findings}, "패키지를 열 수 없거나 한도를 넘어 거절했다")
        with src:
            return _import_open(db, src, path, data_home, limits, result, started, stored_now)
    except (ImportFailure, ContainerError) as e:
        result.add("error", e.code, path.name, e.message)
        result.orphan_photos = list(stored_now)
        result.dataset_version_after = result.dataset_version_before
        return _finish(db, data_home, result, "failed", started, report_dict,
                       f"실패 [{e.code}]: {e.message}. 정본은 되돌렸고 입력 원본은 그대로다" + (". 보관한 사진은 정리대기로 남겼다" if stored_now else ""))


def _same_snapshot(report: Report, name: str, digest: str) -> None:
    if report.file_digests.get(name) != digest:
        raise ImportFailure("package_changed_during_import", f"{name} 의 내용이 검사 때와 다르다. 패키지가 바뀌는 중이면 완료 후 다시 넣는다")


def _import_open(db: Db, src, path: Path, data_home: Path, limits: Limits, result: ImportResult, started: str, stored_now: list[str]) -> ImportResult:
    report_dict: dict = {}
    try:
        # ---- 1. 검사 (정본 물건 목록을 시드로) ----
        seed = [{"asset_id": a["asset_id"], "data_mode": a["data_mode"]} for a in db.list_assets()]
        report = Report(source=str(path))
        report.kind = src.kind
        inspect_source(src, report, seed=seed, study_id=db.meta("study_id"), limits=limits)
        report_dict = report.to_dict()
        if path.is_dir() and report.verdict() != "reject":
            result.package_sha256 = dir_identifier(report.file_digests)
        result.package_id = report.package_id
        for f in report.sorted_findings():
            result.add(f.level, f.code, f.path, f.message)
        result.events_total = report.counts.get("events", 0)
        result.events_skipped = report.counts.get("duplicates", 0)
        if not seed:
            result.add("reject", "no_assets_in_db", "assets", "정본에 물건이 없다. 시드를 먼저 승계한다 (j5 db load-seed)")
        if report.data_mode is not None and report.data_mode != db.data_mode:
            result.add("reject", "data_mode_mismatch", "manifest.json", f"패키지 data_mode {report.data_mode}, 정본 {db.data_mode}")
        verdict = report.verdict()
        if verdict == "reject" or result.has("no_assets_in_db") or result.has("data_mode_mismatch"):
            return _finish(db, data_home, result, "rejected", started, report_dict, "검사에서 거절됐다")
        if verdict == "hold":
            return _finish(db, data_home, result, "held", started, report_dict, "패키지 안에 같은 event_id·다른 내용이 있다")

        # ---- 2. 패키지 중복 (같은 파일을 다시 넣음) ----
        prev = db.conn.execute("SELECT run_id, outcome FROM import_runs WHERE package_sha256 = ? AND outcome IN ('applied', 'duplicate') ORDER BY finished_at LIMIT 1",
                               (result.package_sha256,)).fetchone()
        if prev is not None:
            result.add("info", "package_already_imported", path.name, f"같은 파일이 이미 처리됐다 (run {prev[0]}, {prev[1]})")
            return _finish(db, data_home, result, "duplicate", started, report_dict, "같은 파일을 다시 넣었다. 아무것도 바꾸지 않았다")

        # ---- 3. 이벤트 행·해시, 대장 대조, 정정 대상 (검사한 스냅샷과 같은지 항목마다 대조) ----
        events: list[dict] = []
        raw, digest = src.read(MANIFEST, limits.manifest)
        _same_snapshot(report, MANIFEST, digest)
        manifest = json.loads(raw.decode("utf-8"))
        obs_raw, digest = src.read(OBSERVATIONS, limits.uncompressed)
        _same_snapshot(report, OBSERVATIONS, digest)
        seen: set[str] = set()
        for seg in obs_raw.split(b"\n"):
            if not seg:
                continue
            ev = json.loads(seg.decode("utf-8"))
            if ev["event_id"] in seen:
                continue  # 같은 바이트 반복은 검사에서 확인됐다
            seen.add(ev["event_id"])
            line = seg + b"\n"
            events.append({"ev": ev, "line": line, "hash": event_hash(line)})
        ledger = {r[0]: (r[1], r[2]) for r in db.conn.execute("SELECT event_id, event_hash, record_id FROM import_events WHERE outcome = 'inserted'")}
        new_events: list[dict] = []
        for e in events:
            eid = e["ev"]["event_id"]
            if eid in ledger:
                if ledger[eid][0] != e["hash"]:
                    result.add("hold", "event_id_conflict_with_canonical", eid, "정본에 같은 event_id 가 다른 내용으로 이미 반영되어 있다")
                else:
                    e["skip"] = True
            else:
                new_events.append(e)
        new_ids = {e["ev"]["event_id"] for e in new_events}
        for e in new_events:
            c = e["ev"]["corrects_event_id"]
            if c and c not in ledger and c not in new_ids:
                result.add("hold", "corrects_target_missing", e["ev"]["event_id"], f"정정 대상 {c} 가 정본에도 이 패키지에도 없다. 대상이 담긴 패키지를 먼저 반영한다")
        if any(f["level"] == "hold" for f in result.findings):
            return _finish(db, data_home, result, "held", started, report_dict, "전체 입력을 보류했다. 정본과 입력 원본은 그대로다")
        result.events_new = len(new_events)
        result.events_skipped += len(events) - len(new_events)
        if not new_events:
            return _finish(db, data_home, result, "duplicate", started, report_dict, "새 이벤트가 없다. dataset_version 을 올리지 않는다")
        new_events = _order_events(new_events)

        # ---- 4. 사진 보관 (새 이벤트가 참조하는 것만) ----
        needed: dict[str, tuple[str, str]] = {}
        for e in new_events:
            for ref in e["ev"]["attachment_refs"]:
                needed[ref["sha256"]] = (ref["path"], ref["path"].rsplit(".", 1)[-1])
        for sha, (pkg_path, ext) in sorted(needed.items()):
            data, digest = src.read(pkg_path, limits.photo, code="photo_size_exceeded")
            _same_snapshot(report, pkg_path, digest)
            if digest != sha:
                raise ImportFailure("photo_hash_changed", f"{pkg_path} 의 해시가 첨부 참조와 다르다")
            rel, new = _store_photo(data_home, sha, ext, data)
            if new:
                stored_now.append(rel)
                result.photos_stored += 1
            else:
                result.photos_reused += 1

        # ---- 5. 정본 반영 (한 트랜잭션) ----
        run_id = str(uuid.uuid4())
        doc_id = str(uuid.uuid5(_NS, f"package:{result.package_sha256}"))
        try:
            with db.transaction():
                location = path.relative_to(data_home).as_posix() if _is_under(path, data_home) else path.name
                db.add_source_document({
                    "document_id": doc_id, "document_kind": "field_package", "title": path.name, "terms": "자체 현장 기록 (사용자 소유)",
                    "location": location, "sha256": result.package_sha256 if path.is_file() else None,
                    "collected_at": manifest["created_at"], "notes": f"package_id {manifest['package_id']}, study_id {manifest['study_id']}",
                })
                for e in new_events:
                    ev = e["ev"]
                    rt = ev["record_type"]
                    rec = {
                        "record_id": ev["event_id"], "subject_id": ev["asset_id"], "subject_type": "asset", "record_type": rt,
                        "source_kind": "field_observation", "schema_version": RECORD_PAYLOAD_SCHEMAS[rt][2], "payload": ev["payload"],
                        "observed_at": ev["observed_at"], "observed_at_precision": ev["observed_at_precision"],
                        "device_created_at": ev["device_created_at"], "collected_at": manifest["created_at"], "supersedes_id": ev["corrects_event_id"],
                    }
                    atts = [{
                        "attachment_id": str(uuid.uuid5(_NS, f"attachment:{ev['event_id']}:{ref['sha256']}")),
                        "rel_path": photo_rel_path(ref["sha256"], ref["path"].rsplit(".", 1)[-1]), "sha256": ref["sha256"], "mime": ref["mime"],
                        "bytes": ref["bytes"], "tags": list(ref["tags"]), "taken_at": ref.get("taken_at"),  # 촬영 시각은 폰이 기록한 값을 그대로 보존
                        "viewpoint_id": ref.get("viewpoint_id"), "heading_deg": ref.get("heading_deg"), "previous_photo_sha256": ref.get("previous_photo_sha256"),  # 연차 비교 (J5-015B)
                    } for ref in ev["attachment_refs"]]
                    db.insert_record(rec, evidence=[{"document_id": doc_id, "locator": f"{OBSERVATIONS}#event_id={ev['event_id']}"}], attachments=atts)
                    if ev.get("route_version_id") or ev.get("frame_version_id"):
                        # 경로·표본틀 참조는 records 밖(record_survey_refs)에 둔다. 외래키 없음: 폰이 먼저 적은 ID 가 정본에 아직 없을 수 있다 (J5-013A)
                        db.conn.execute("INSERT INTO record_survey_refs (record_id, route_version_id, frame_version_id, recorded_at) VALUES (?, ?, ?, ?)",
                                        (ev["event_id"], ev.get("route_version_id"), ev.get("frame_version_id"), db.now()))
                    db.conn.execute("INSERT INTO import_events (run_id, event_id, event_hash, line, outcome, record_id) VALUES (?, ?, ?, ?, 'inserted', ?)",
                                    (run_id, ev["event_id"], e["hash"], e["line"], ev["event_id"]))
                for e in events:
                    if e.get("skip"):
                        db.conn.execute("INSERT INTO import_events (run_id, event_id, event_hash, line, outcome, record_id) VALUES (?, ?, ?, ?, 'skipped_duplicate', NULL)",
                                        (run_id, e["ev"]["event_id"], e["hash"], e["line"]))
                for rel in stored_now + [photo_rel_path(s, ext) for s, (_, ext) in needed.items()]:
                    if not (data_home / rel).is_file():
                        raise ImportFailure("photo_missing_after_store", f"{rel} 가 없다. DB 는 참조 파일 존재를 확인한다")
                result.dataset_version_after = db.bump_dataset_version()
                result.run_id = run_id
                result.message = "반영됐다 (정본). 백업·파생본은 별도 명령으로 만든다"
                _insert_run(db, run_id, result, "applied", started, report_dict, doc_id)
        except ImportFailure:
            raise
        except (ValidationError, Exception) as e:  # sqlite3 오류·검증 오류 포함. 트랜잭션은 이미 되돌려졌다
            raise ImportFailure("db_apply_failed", f"{type(e).__name__}: {e}") from e
        result.outcome = "applied"
        _append_log(data_home, _log_entry(result, started))
        return result
    except (ImportFailure, ContainerError) as e:
        result.add("error", e.code, path.name, e.message)
        result.orphan_photos = list(stored_now)
        result.dataset_version_after = result.dataset_version_before
        return _finish(db, data_home, result, "failed", started, report_dict,
                       f"실패 [{e.code}]: {e.message}. 정본은 되돌렸고 입력 원본은 그대로다" + (". 보관한 사진은 정리대기로 남겼다" if stored_now else ""))


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _insert_run(db: Db, run_id: str, r: ImportResult, outcome: str, started: str, report_dict: dict, doc_id: str | None) -> None:
    db.conn.execute(
        "INSERT INTO import_runs (run_id, package_name, package_sha256, package_id, study_id, data_mode, package_schema_version, source_document_id, outcome,"
        " events_total, events_new, events_skipped, photos_stored, photos_reused, dataset_version_before, dataset_version_after, started_at, finished_at, report_json, message)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, r.package, r.package_sha256, r.package_id, report_dict.get("study_id"), report_dict.get("data_mode"), report_dict.get("schema_version"), doc_id, outcome,
         r.events_total, r.events_new, r.events_skipped, r.photos_stored, r.photos_reused, r.dataset_version_before, r.dataset_version_after,
         started, db.now(), json.dumps({"inspect": report_dict, "findings": r.findings, "orphan_photos": r.orphan_photos}, ensure_ascii=False, sort_keys=True), r.message or None))


def _finish(db: Db, data_home: Path, result: ImportResult, outcome: str, started: str, report_dict: dict, message: str) -> ImportResult:
    """반영하지 않은 결과(거절·보류·중복·실패)를 수입 기록과 로그에 남긴다. 기록 실패는 결과를 바꾸지 않는다."""
    result.outcome = outcome
    result.message = message
    run_id = str(uuid.uuid4())
    try:
        with db.transaction():
            _insert_run(db, run_id, result, outcome, started, report_dict, None)
        result.run_id = run_id
    except Exception as e:  # noqa: BLE001 - 기록 실패는 화면·로그로만 알린다
        result.add("warn", "run_log_failed", "import_runs", f"수입 기록을 남기지 못했다: {type(e).__name__}: {e}")
    _append_log(data_home, _log_entry(result, started))
    return result


def _log_entry(r: ImportResult, started: str) -> dict:
    return {"started_at": started, "finished_at": now_utc(), "package": r.package, "package_sha256": r.package_sha256, "outcome": r.outcome, "run_id": r.run_id,
            "package_id": r.package_id, "events_total": r.events_total, "events_new": r.events_new, "events_skipped": r.events_skipped,
            "photos_stored": r.photos_stored, "photos_reused": r.photos_reused, "dataset_version": [r.dataset_version_before, r.dataset_version_after],
            "orphan_photos": r.orphan_photos, "message": r.message}
