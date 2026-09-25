"""백업·복구·사진 대사 (J5-012). 데이터 사전 §12, ADR E07, AGENTS 정본 절 ("백업은 일관된 DB 사본과 참조 파일로 만들고 빈 폴더 복구를 시험한다").

백업(create_backup): 정본 연결의 읽기 트랜잭션 안에서 버전·행수·첨부 목록을 읽고 같은 스냅샷을 SQLite 백업 API 로 사본에 쓴다
    (그동안 다른 쓰기는 대기한다: "쓰기를 잠시 중단"). 사본을 읽기 전용으로 열어 무결성·외래키·행수·meta 를 원본과 대조하고,
    첨부가 참조한 사진과 거래 수집이 참조한 원본(실행 기록·응답 XML, raw/rt_nrg)을 내용 해시를 검증하며 복사한 뒤 backup_manifest.json(파일 목록·해시) 을 쓴다. 디스크에서 다시 읽어 전부 검증(verify_backup_dir)
    한 뒤에만 최종 폴더 이름으로 바꾸고 backups/latest.json 포인터를 교체한다. 정본은 바꾸지 않는다.
    실패하면 이전 백업·포인터는 그대로고 부분 산출물은 failed-<run8>/ 에 남는다(자동 삭제 없음). 참조 사진이 하나라도 없거나 해시가 다르면
    백업을 완료로 표시하지 않는다(사진 없는 DB 사본을 완전한 백업이라 하지 않는다).
복구(restore_backup): 빈 폴더에만 한다. 백업 폴더를 전부 검증 → 스테이징에 복사(해시 재검증) → 복사한 정본을 열어 무결성·행수·사진 연결 확인
    → db/·photos/·raw/ 를 제자리로 → 최종 확인 → logs/restore.log. 기존 정본·사진이 있는 폴더에는 복구하지 않는다.
사진 대사(check_photos): 정본 attachments 가 참조한 파일의 존재·크기·해시와 photos/ 의 미참조 파일을 보고한다. 아무것도 지우지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

from j5 import APP_VERSION
from j5.db.store import Db, DbError
from j5.db.validate import now_utc
from j5.schemas_loader import schema_errors

BACKUPS_DIR = Path("backups")
LATEST_POINTER = "latest.json"
BACKUP_MANIFEST = "backup_manifest.json"
BACKUP_SCHEMA_VERSION = "1.1.0"  # 1.1.0: 거래 수집 원본(raw/rt_nrg)도 참조 파일로 포함
DB_REL = "db/j5.sqlite3"
PHOTOS_DIR = "photos"
RAW_DIR = "raw"
BACKUP_LOG = Path("logs") / "backup.log"
RESTORE_LOG = Path("logs") / "restore.log"
CHUNK = 1 << 20
EXIT_BY_OUTCOME = {"completed": 0, "failed": 1}


class BackupError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class BackupResult:
    outcome: str = "failed"
    run_id: str | None = None
    created_at: str | None = None
    dataset_version: int = 0
    db_schema_version: int = 0
    backup_dir: str | None = None       # data_home 기준 상대경로, 밖이면 절대경로
    manifest_sha256: str | None = None
    counts: dict = field(default_factory=dict)
    files: int = 0
    photos: int = 0
    raw_files: int = 0
    bytes: int = 0
    same_device: bool | None = None     # 백업 폴더가 정본과 같은 장치인지 (독립 사본 여부는 사용자가 판단)
    findings: list[dict] = field(default_factory=list)
    message: str = ""

    def add(self, level: str, code: str, message: str) -> None:
        self.findings.append({"level": level, "code": code, "message": message})

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, indent=2) + "\n"

    def to_text(self) -> str:
        lines = [f"백업: {'완료' if self.outcome == 'completed' else '실패 (이전 백업·포인터 유지, 정본 그대로)'}"]
        if self.backup_dir:
            lines.append(f"위치: {self.backup_dir} (정본 v{self.dataset_version}, db_schema {self.db_schema_version}, 파일 {self.files}개 {self.bytes} 바이트, 사진 {self.photos}장, 수집 원본 {self.raw_files}개)")
        if self.counts:
            lines.append("행 수: " + ", ".join(f"{k} {v}" for k, v in self.counts.items()))
        if self.message:
            lines.append(self.message)
        for f in self.findings:
            if f["level"] in ("error", "warn"):
                lines.append(f"  [{f['level']}] {f['code']}: {f['message']}")
        if self.outcome == "completed" and self.same_device:
            lines.append("주의: 백업이 정본과 같은 장치에 있다. 다른 장치·장소의 사본은 사용자가 따로 둔다.")
        lines.append("백업 완료는 파생본 게시·외부 사본 보관을 뜻하지 않는다.")
        return "\n".join(lines) + "\n"


@dataclass
class RestoreResult:
    outcome: str = "failed"
    run_id: str | None = None
    backup_dir: str | None = None
    dest_home: str | None = None
    dataset_version: int = 0
    db_schema_version: int = 0
    counts: dict = field(default_factory=dict)
    photos: int = 0
    findings: list[dict] = field(default_factory=list)
    message: str = ""

    def add(self, level: str, code: str, message: str) -> None:
        self.findings.append({"level": level, "code": code, "message": message})

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, indent=2) + "\n"

    def to_text(self) -> str:
        lines = [f"복구: {'완료' if self.outcome == 'completed' else '실패 (대상 폴더에 정본을 두지 않았다)'}", f"백업: {self.backup_dir}", f"대상: {self.dest_home}"]
        if self.outcome == "completed":
            lines.append(f"정본 v{self.dataset_version} (db_schema {self.db_schema_version}) · 행 수: " + ", ".join(f"{k} {v}" for k, v in self.counts.items()) + f" · 사진 {self.photos}장 연결 확인")
        if self.message:
            lines.append(self.message)
        for f in self.findings:
            if f["level"] in ("error", "warn"):
                lines.append(f"  [{f['level']}] {f['code']}: {f['message']}")
        if self.outcome == "completed":
            lines.append("복구한 폴더에는 아직 백업·파생본이 없다. `j5 db backup` 과 `j5 db project` 를 다시 실행한다.")
        return "\n".join(lines) + "\n"


# ---- 공통 ----

def _sha256_file(p: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def _copy_verified(src: Path, dst: Path, *, sha256: str | None = None, nbytes: int | None = None) -> tuple[str, int]:
    """원본을 읽으며 해시하고, 쓴 뒤 fsync 하고, 다시 읽어 같은지 확인한다. 기대 해시·크기가 있으면 대조한다."""
    if not src.is_file():
        raise BackupError("file_missing", f"{src} 가 없다")
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    n = 0
    with open(src, "rb") as fi, open(dst, "xb") as fo:
        for b in iter(lambda: fi.read(CHUNK), b""):
            h.update(b)
            n += len(b)
            fo.write(b)
        fo.flush()
        os.fsync(fo.fileno())
    digest = h.hexdigest()
    if (sha256 is not None and digest != sha256) or (nbytes is not None and n != nbytes):
        raise BackupError("hash_mismatch", f"{src} 의 해시·크기가 기록과 다르다 (기록 {sha256} {nbytes}, 실제 {digest} {n})")
    back, back_n = _sha256_file(dst)
    if back != digest or back_n != n:
        raise BackupError("copy_verify_failed", f"{dst} 를 다시 읽은 해시가 원본과 다르다")
    return digest, n


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _append_log(path: Path, entry: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass  # 로그 실패가 결과를 바꾸지 않는다


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in names}


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(path.resolve().as_posix())}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _referenced_photos(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT DISTINCT rel_path, sha256, bytes FROM attachments ORDER BY rel_path").fetchall()
    return [{"rel_path": r["rel_path"], "sha256": r["sha256"], "bytes": r["bytes"]} for r in rows]


def _referenced_raw(conn: sqlite3.Connection) -> list[dict]:
    """정본이 참조한 거래 수집 원본: collection_runs.run_path(실행 기록, 해시는 정본에 없음)와 collection_pages.raw_path(응답 XML, 해시·크기 있음).
    db_schema 6 이전 정본에는 표가 없으므로 빈 목록."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "collection_runs" not in tables or "collection_pages" not in tables:
        return []
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT DISTINCT run_path FROM collection_runs"):
        out[r[0]] = {"rel_path": r[0], "sha256": None, "bytes": None}
    for r in conn.execute("SELECT DISTINCT raw_path, raw_sha256, bytes FROM collection_pages WHERE raw_path IS NOT NULL"):
        out[r["raw_path"]] = {"rel_path": r["raw_path"], "sha256": r["raw_sha256"], "bytes": r["bytes"]}
    return sorted(out.values(), key=lambda x: x["rel_path"])


def check_raw_files(db: Db, data_home: Path) -> dict:
    """정본이 참조한 수집 원본(실행 기록·응답 XML)의 존재·해시·크기. 아무것도 지우지 않는다."""
    data_home = Path(data_home)
    referenced = _referenced_raw(db.conn)
    ok, missing, mismatched = 0, [], []
    for f in referenced:
        p = data_home / f["rel_path"]
        if not p.is_file():
            missing.append(f["rel_path"])
            continue
        digest, n = _sha256_file(p)
        if (f["sha256"] is not None and digest != f["sha256"]) or (f["bytes"] is not None and n != f["bytes"]):
            mismatched.append(f["rel_path"])
        else:
            ok += 1
    return {"referenced": len(referenced), "ok": ok, "missing": missing, "mismatched": mismatched, "ok_all": not missing and not mismatched}


def _check_db_file(path: Path, *, expected_counts: dict | None = None, expected_meta: dict | None = None) -> dict:
    """DB 파일을 읽기 전용으로 열어 무결성·외래키·행수·meta 를 확인한다. 통과하면 {counts, meta, referenced} 를 돌려준다."""
    conn = _open_ro(path)
    try:
        integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise BackupError("db_integrity", "; ".join(integrity[:3]))
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise BackupError("db_foreign_keys", f"외래키 위반 {len(fk)}건")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "meta" not in tables or "attachments" not in tables:
            raise BackupError("db_not_j5", "j5 정본이 아니다")
        counts = _table_counts(conn)
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        meta["db_schema_version"] = int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)
        if expected_counts is not None and counts != expected_counts:
            diff = sorted(k for k in set(counts) | set(expected_counts) if counts.get(k) != expected_counts.get(k))
            raise BackupError("db_counts", f"행 수가 다르다: {diff}")
        if expected_meta is not None:
            for k, v in expected_meta.items():
                if str(meta.get(k)) != str(v):
                    raise BackupError("db_meta", f"meta.{k}: 사본 {meta.get(k)}, 기대 {v}")
        return {"counts": counts, "meta": meta, "referenced": _referenced_photos(conn), "referenced_raw": _referenced_raw(conn)}
    finally:
        conn.close()


# ---- 백업 ----

def create_backup(db: Db, data_home: Path, *, dest_root: Path | None = None) -> BackupResult:
    data_home = Path(data_home)
    started = db.now()
    run_id = str(uuid.uuid4())
    r = BackupResult(run_id=run_id, created_at=started)
    root = Path(dest_root) if dest_root is not None else data_home / BACKUPS_DIR
    tmp = root / f".tmp-{run_id[:8]}"
    final: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(exist_ok=False)
        db_copy = tmp / DB_REL
        db_copy.parent.mkdir(parents=True)
        # 1. 읽기 트랜잭션 안에서 버전·행수·첨부 목록을 읽고 같은 스냅샷을 백업 API 로 사본에 쓴다. 그동안 다른 쓰기는 대기한다.
        with db.snapshot():
            meta = {"study_id": db.meta("study_id"), "data_mode": db.data_mode, "dataset_version": int(db.meta("dataset_version") or 0)}
            schema_version = db.schema_version()
            counts = _table_counts(db.conn)
            referenced = _referenced_photos(db.conn)
            referenced_raw = _referenced_raw(db.conn)
            dst = sqlite3.connect(str(db_copy))
            try:
                db.conn.backup(dst)
            finally:
                dst.close()
        r.dataset_version, r.db_schema_version, r.counts = meta["dataset_version"], schema_version, counts
        # 2. 사본 검사: 무결성·외래키·행수·meta 가 원본 스냅샷과 같다
        copy_info = _check_db_file(db_copy, expected_counts=counts, expected_meta={**meta, "db_schema_version": schema_version})
        if copy_info["referenced"] != referenced or copy_info["referenced_raw"] != referenced_raw:
            raise BackupError("db_copy_refs", "사본의 첨부·원본 목록이 원본 스냅샷과 다르다")
        digest, n = _sha256_file(db_copy)
        files = [{"path": DB_REL, "bytes": n, "sha256": digest}]
        # 3. 참조 사진 복사 (내용 해시 검증). 하나라도 없거나 다르면 백업을 완료로 표시하지 않는다.
        problems = []
        for ph in referenced:
            src = data_home / ph["rel_path"]
            try:
                _copy_verified(src, tmp / ph["rel_path"], sha256=ph["sha256"], nbytes=ph["bytes"])
            except BackupError as e:
                problems.append(f"{ph['rel_path']}: {e.code}")
                continue
            files.append({"path": ph["rel_path"], "bytes": ph["bytes"], "sha256": ph["sha256"]})
        if problems:
            raise BackupError("photos_incomplete", f"참조 사진 {len(problems)}건이 없거나 해시가 다르다: " + "; ".join(problems[:5]) + ". 사진 대사(check-photos)로 확인하고 이전 백업에서 되찾는다")
        # 3b. 거래 수집 원본(실행 기록·응답 XML) 복사. 응답 XML 은 정본의 해시·크기와 대조하고, 실행 기록은 지금 해시를 잰다.
        raw_problems = []
        n_raw = 0
        for f in referenced_raw:
            src = data_home / f["rel_path"]
            try:
                digest_f, n_f = _copy_verified(src, tmp / f["rel_path"], sha256=f["sha256"], nbytes=f["bytes"])
            except BackupError as e:
                raw_problems.append(f"{f['rel_path']}: {e.code}")
                continue
            files.append({"path": f["rel_path"], "bytes": n_f, "sha256": digest_f})
            n_raw += 1
        if raw_problems:
            raise BackupError("raw_incomplete", f"정본이 참조한 수집 원본 {len(raw_problems)}건이 없거나 해시가 다르다: " + "; ".join(raw_problems[:5]) + ". 실데이터 홈의 raw/ 를 확인하고 이전 백업에서 되찾는다")
        manifest = {
            "format": "j5backup", "backup_schema_version": BACKUP_SCHEMA_VERSION, "study_id": meta["study_id"], "data_mode": meta["data_mode"],
            "dataset_version": meta["dataset_version"], "db_schema_version": schema_version, "created_at": started, "run_id": run_id,
            "tool_version": APP_VERSION, "sqlite_version": sqlite3.sqlite_version, "counts": counts,
            "files": sorted(files, key=lambda f: f["path"]),
        }
        errs = schema_errors("backup_manifest.schema.json", manifest)
        if errs:
            raise BackupError("manifest_schema", "; ".join(errs[:3]))
        _write_atomic(tmp / BACKUP_MANIFEST, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        # 4. 디스크에서 다시 읽어 전부 검증한 뒤에만 게시한다
        verified = verify_backup_dir(tmp)
        stamp = started.replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")
        final = root / f"{stamp}-ds{meta['dataset_version']}-{run_id[:8]}"
        os.rename(tmp, final)
        r.backup_dir = _display_path(final, data_home)
        r.manifest_sha256 = verified["manifest_sha256"]
        r.files, r.photos, r.raw_files, r.bytes = len(files), len(files) - 1 - n_raw, n_raw, sum(f["bytes"] for f in files)
        try:
            r.same_device = final.stat().st_dev == db.path.stat().st_dev
        except OSError:
            r.same_device = None
        r.outcome = "completed"
        r.message = f"백업 완료 (정본 v{meta['dataset_version']}). 사본을 다시 읽어 검증했다"
        # 5. 포인터 교체 (실패해도 백업 폴더는 유효하다. 상태 표시는 포인터를 따르므로 다음 백업이 다시 가리킨다)
        pointer = {"run_id": run_id, "created_at": started, "dataset_version": meta["dataset_version"], "db_schema_version": schema_version,
                   "study_id": meta["study_id"], "data_mode": meta["data_mode"], "dir": r.backup_dir, "manifest_sha256": r.manifest_sha256}
        try:
            (data_home / BACKUPS_DIR).mkdir(parents=True, exist_ok=True)
            _write_atomic(data_home / BACKUPS_DIR / LATEST_POINTER, (json.dumps(pointer, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except OSError as e:
            r.add("warn", "pointer_not_updated", f"백업은 완료했지만 latest.json 을 갱신하지 못했다 ({e}). 상태 표시가 이 백업을 가리키지 않는다")
        return r
    except (BackupError, DbError, OSError, ValueError, KeyError, sqlite3.Error) as e:
        code = getattr(e, "code", type(e).__name__)
        msg = getattr(e, "message", str(e))
        r.outcome = "failed"
        r.backup_dir = r.manifest_sha256 = None
        r.add("error", code, msg)
        partial = tmp if tmp.exists() else (final if final is not None and final.exists() else None)
        if partial is not None:
            failed_dir = root / f"failed-{run_id[:8]}"
            try:
                os.rename(partial, failed_dir)
                r.add("warn", "partial_kept", f"부분 산출물을 {_display_path(failed_dir, data_home)} 에 남겼다 (백업 아님, 자동 삭제하지 않음)")
            except OSError:
                pass
        prev, prev_problem = _safe_read_latest(data_home)
        if prev_problem:
            keep = f" 이전 포인터는 읽을 수 없다({prev_problem}). 상태 표시(j5 db status)로 확인한다."
        elif prev:
            keep = f" 이전 백업 v{prev.get('dataset_version')}({prev.get('created_at')}) 과 포인터는 그대로다."
        else:
            keep = " 완료된 백업이 없다."
        r.message = f"백업 실패 [{code}]: {msg}. 정본은 그대로다.{keep}"
        return r
    finally:
        _append_log(data_home / BACKUP_LOG, {"started_at": started, "finished_at": now_utc(), "run_id": run_id, "outcome": r.outcome, "dataset_version": r.dataset_version,
                                             "backup_dir": r.backup_dir, "manifest_sha256": r.manifest_sha256, "files": r.files, "photos": r.photos, "message": r.message})


def _display_path(p: Path, data_home: Path) -> str:
    try:
        return p.resolve().relative_to(data_home.resolve()).as_posix()
    except ValueError:
        return str(p.resolve())


def read_latest(data_home: Path) -> dict | None:
    p = Path(data_home) / BACKUPS_DIR / LATEST_POINTER
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _safe_read_latest(data_home: Path) -> tuple[dict | None, str | None]:
    """(포인터, 문제 코드). 포인터가 없으면 (None, None), 읽을 수 없거나 형식이 아니면 (None, 코드)."""
    try:
        ptr = read_latest(data_home)
    except (OSError, ValueError) as e:
        return None, f"pointer_unreadable:{type(e).__name__}"
    if ptr is not None and not isinstance(ptr, dict):
        return None, "pointer_invalid:type"
    return ptr, None


def resolve_backup_dir(data_home: Path, pointer: dict) -> Path:
    d = Path(pointer["dir"])
    return d if d.is_absolute() else Path(data_home) / d


def verify_backup_dir(bdir: Path) -> dict:
    """백업 폴더를 디스크에서 전부 읽어 검증한다: manifest 스키마, 파일 목록·크기·해시(누락·여분 없음),
    DB 사본의 무결성·외래키·행수·meta, 첨부가 참조한 사진 전부가 목록에 같은 해시로 있음(사진 연결), 목록의 사진이 전부 참조됨."""
    bdir = Path(bdir)
    mpath = bdir / BACKUP_MANIFEST
    if not mpath.is_file():
        raise BackupError("manifest_missing", f"{mpath} 가 없다")
    raw = mpath.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BackupError("manifest_unreadable", str(e)) from e
    errs = schema_errors("backup_manifest.schema.json", manifest)
    if errs:
        raise BackupError("manifest_schema", "; ".join(errs[:3]))
    listed = {f["path"]: f for f in manifest["files"]}
    if DB_REL not in listed:
        raise BackupError("manifest_no_db", "파일 목록에 정본 사본이 없다")
    for path, f in listed.items():
        p = bdir / path
        if not p.is_file():
            raise BackupError("file_missing", path)
        digest, n = _sha256_file(p)
        if digest != f["sha256"] or n != f["bytes"]:
            raise BackupError("file_hash", f"{path} 의 크기·해시가 manifest 와 다르다")
    on_disk = {p.relative_to(bdir).as_posix() for p in bdir.rglob("*") if p.is_file()} - {BACKUP_MANIFEST}
    if on_disk != set(listed):
        raise BackupError("extra_or_missing", f"manifest 밖 파일 또는 누락: {sorted(on_disk ^ set(listed))}")
    info = _check_db_file(bdir / DB_REL, expected_counts=manifest["counts"],
                          expected_meta={"study_id": manifest["study_id"], "data_mode": manifest["data_mode"], "dataset_version": manifest["dataset_version"],
                                         "db_schema_version": manifest["db_schema_version"]})
    raw_paths = {p for p in listed if p.startswith(RAW_DIR + "/")}
    photo_paths = {p for p in listed if p != DB_REL} - raw_paths
    for ph in info["referenced"]:
        f = listed.get(ph["rel_path"])
        if f is None or f["sha256"] != ph["sha256"] or f["bytes"] != ph["bytes"]:
            raise BackupError("photo_link", f"정본 사본이 참조한 {ph['rel_path']} 가 백업에 없거나 해시가 다르다")
    unref = photo_paths - {ph["rel_path"] for ph in info["referenced"]}
    if unref:
        raise BackupError("photo_unreferenced", f"백업에 참조되지 않은 사진이 있다: {sorted(unref)[:3]}")
    for rf in info["referenced_raw"]:
        f = listed.get(rf["rel_path"])
        if f is None or (rf["sha256"] is not None and f["sha256"] != rf["sha256"]) or (rf["bytes"] is not None and f["bytes"] != rf["bytes"]):
            raise BackupError("raw_link", f"정본 사본이 참조한 수집 원본 {rf['rel_path']} 가 백업에 없거나 해시가 다르다")
    unref_raw = raw_paths - {rf["rel_path"] for rf in info["referenced_raw"]}
    if unref_raw:
        raise BackupError("raw_unreferenced", f"백업에 참조되지 않은 수집 원본이 있다: {sorted(unref_raw)[:3]}")
    return {"manifest": manifest, "manifest_sha256": hashlib.sha256(raw).hexdigest(), "counts": info["counts"], "photos": len(photo_paths), "raw_files": len(raw_paths),
            "referenced": len(info["referenced"])}


def backup_status(db: Db, data_home: Path | None) -> dict:
    """정본 버전과 마지막 백업 버전의 차이. 포인터와 manifest 파일을 실제로 확인한다(전체 해시 검증은 backup-verify 가 한다).
    백업 기록은 정본에 두지 않으므로 data_home 없이는 '미확인' 이다."""
    current = int(db.meta("dataset_version") or 0)
    st = {"dataset_version": current, "backup_version": None, "backup_at": None, "backup_dir": None, "checked": data_home is not None, "problem": None, "stale": True}
    if data_home is None:
        st["state"] = "파일 미확인 (data_home 없음)"
        return st
    pointer, problem = _safe_read_latest(data_home)
    if problem:
        st["problem"] = problem
        st["state"] = f"손상 (백업 필요: {problem})"
        return st
    if pointer is None:
        st["state"] = "없음 (백업 필요)"
        return st
    try:
        st["backup_version"], st["backup_at"], st["backup_dir"] = pointer["dataset_version"], pointer["created_at"], pointer["dir"]
        # 포인터의 정체성(study_id·data_mode)이 열린 정본과 다르면 다른 정본의 백업이다. 버전이 같아도 '최신' 이라 하지 않는다.
        if pointer["study_id"] != db.meta("study_id") or pointer["data_mode"] != db.data_mode:
            st["problem"] = "identity_mismatch"
            st["state"] = f"다른 정본의 백업 (백업 study {pointer['study_id']}/{pointer['data_mode']}, 정본 {db.meta('study_id')}/{db.data_mode}). 이 정본의 백업이 필요하다"
            return st
        bdir = resolve_backup_dir(data_home, pointer)
        mpath = bdir / BACKUP_MANIFEST
        if not bdir.is_dir():
            st["problem"] = "backup_dir_unreachable"
            st["state"] = f"접근 불가 (백업 폴더 없음 또는 미연결: {pointer['dir']}, 기록상 v{pointer['dataset_version']})"
            return st
        if not mpath.is_file() or hashlib.sha256(mpath.read_bytes()).hexdigest() != pointer["manifest_sha256"]:
            st["problem"] = "manifest_mismatch"
            st["state"] = f"손상 (백업 필요: manifest 없음 또는 변경, 기록상 v{pointer['dataset_version']})"
            return st
    except (KeyError, TypeError, OSError) as e:
        st["problem"] = f"pointer_invalid:{type(e).__name__}"
        st["state"] = f"손상 (백업 필요: {st['problem']})"
        return st
    v = pointer["dataset_version"]
    if v == current:
        st["stale"] = False
        st["state"] = f"최신 (정본 v{current})"
    elif v < current:
        st["state"] = f"백업 필요 (정본 v{current}, 마지막 백업 v{v})"
    else:
        st["state"] = f"정본보다 새로운 백업 (정본 v{current}, 백업 v{v}). 정본이 복구본이면 백업을 다시 만든다"
    return st


# ---- 사진 대사 ----

def check_photos(db: Db, data_home: Path) -> dict:
    """정본 attachments 가 참조한 파일의 존재·크기·해시와 photos/ 의 미참조 파일. 아무것도 지우지 않는다."""
    data_home = Path(data_home)
    referenced = _referenced_photos(db.conn)
    ok, missing, mismatched = 0, [], []
    for ph in referenced:
        p = data_home / ph["rel_path"]
        if not p.is_file():
            missing.append(ph["rel_path"])
            continue
        digest, n = _sha256_file(p)
        if digest != ph["sha256"] or n != ph["bytes"]:
            mismatched.append(ph["rel_path"])
        else:
            ok += 1
    known = {ph["rel_path"] for ph in referenced}
    root = data_home / PHOTOS_DIR
    unreferenced = sorted(p.relative_to(data_home).as_posix() for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
    unreferenced = [p for p in unreferenced if p not in known]
    return {"referenced": len(referenced), "ok": ok, "missing": missing, "mismatched": mismatched, "unreferenced": unreferenced,
            "ok_all": not missing and not mismatched}


def check_photos_text(c: dict) -> str:
    lines = [f"사진 대사: 참조 {c['referenced']}장, 확인 {c['ok']}장, 누락 {len(c['missing'])}장, 해시 불일치 {len(c['mismatched'])}장, 미참조 파일 {len(c['unreferenced'])}개"]
    for k, label in (("missing", "누락"), ("mismatched", "불일치"), ("unreferenced", "미참조 (정리대기 후보, 자동 삭제 없음)")):
        for p in c[k]:
            lines.append(f"  [{label}] {p}")
    lines.append("정본 참조 사진이 모두 확인됐다." if c["ok_all"] else "누락·불일치 사진은 이전 백업에서 되찾는다. 확인 전에는 백업이 완료되지 않는다.")
    return "\n".join(lines) + "\n"


# ---- 복구 ----

def _is_restorable_dir(p: Path) -> bool:
    """빈 폴더, 또는 이전 복구 시도의 logs/ 만 있는 폴더. db/·photos/·raw/ 등 다른 항목이 있으면 복구하지 않는다."""
    return p.is_dir() and {e.name for e in p.iterdir()} <= {"logs"}


def restore_backup(backup_dir: Path, dest_home: Path) -> RestoreResult:
    backup_dir, dest_home = Path(backup_dir), Path(dest_home)
    started = now_utc()
    run_id = str(uuid.uuid4())
    r = RestoreResult(run_id=run_id, backup_dir=str(backup_dir), dest_home=str(dest_home))
    staging = dest_home / f".restoring-{run_id[:8]}"
    moved: list[str] = []
    try:
        if dest_home.exists() and not _is_restorable_dir(dest_home):
            raise BackupError("dest_not_empty", f"{dest_home} 가 비어 있지 않다. 복구는 빈 폴더에만 한다 (기존 정본·사진을 덮어쓰지 않는다. 이전 복구 시도의 logs/ 만 허용)")
        if not backup_dir.is_dir():
            raise BackupError("backup_missing", f"백업 폴더가 없다: {backup_dir}")
        if dest_home.exists() and backup_dir.resolve() == dest_home.resolve():
            raise BackupError("same_path", "백업 폴더와 대상이 같다")
        # 1. 백업 폴더 전부 검증
        v = verify_backup_dir(backup_dir)
        manifest = v["manifest"]
        # 2. 스테이징에 복사 (해시 재검증)
        dest_home.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        for f in manifest["files"]:
            _copy_verified(backup_dir / f["path"], staging / f["path"], sha256=f["sha256"], nbytes=f["bytes"])
        # 3. 복사한 정본을 열어 확인 (도구가 더 새로우면 마이그레이션이 적용된다. 행수·버전·사진 연결은 그대로여야 한다)
        _verify_restored(staging, manifest)
        # 4. 제자리로
        for name in ("db", PHOTOS_DIR, RAW_DIR):
            src = staging / name
            if src.is_dir():
                os.rename(src, dest_home / name)
                moved.append(name)
        try:
            staging.rmdir()
        except OSError:
            pass
        # 5. 최종 확인 (스테이징에서 마이그레이션이 적용됐을 수 있으므로 파일 그대로의 대조는 3 에서 이미 했다)
        info = _verify_restored(dest_home, manifest, check_copy=False)
        r.dataset_version, r.db_schema_version, r.counts, r.photos = manifest["dataset_version"], info["db_schema_version"], info["counts"], info["photos"]
        r.outcome = "completed"
        r.message = f"복구 완료: 기록 {info['counts'].get('records', 0)}건, 사진 {info['photos']}장 (해시·연결 확인), 정본 v{manifest['dataset_version']}"
        return r
    except (BackupError, DbError, OSError, ValueError, KeyError, sqlite3.Error) as e:
        code = getattr(e, "code", type(e).__name__)
        msg = getattr(e, "message", str(e))
        r.outcome = "failed"
        r.add("error", code, msg)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)  # 백업의 사본일 뿐이므로 지운다. 백업 원본은 손대지 않았다
        if moved:
            r.add("warn", "partial_restore", f"{', '.join(moved)} 를 옮긴 뒤 실패했다. 대상 폴더를 비우고 다시 복구한다")
        r.message = f"복구 실패 [{code}]: {msg}. 백업 원본은 그대로다"
        return r
    finally:
        if dest_home.is_dir():
            _append_log(dest_home / RESTORE_LOG, {"started_at": started, "finished_at": now_utc(), "run_id": run_id, "outcome": r.outcome, "backup_dir": str(backup_dir),
                                                  "dataset_version": r.dataset_version, "counts": r.counts, "photos": r.photos, "message": r.message})


def _verify_restored(home: Path, manifest: dict, *, check_copy: bool = True) -> dict:
    """복구본을 확인한다. 먼저 파일을 읽기 전용으로 열어 manifest 의 행수·meta 와 그대로 대조하고(마이그레이션 전 상태),
    그 다음 정본으로 열어(외래키·마이그레이션 적용) 무결성·버전·사진 연결을 확인한다. 마이그레이션이 적용되면 schema_migrations 행과
    새 테이블이 늘어날 수 있으므로 그 뒤의 대조는 manifest 에 있던 데이터 테이블만 한다(백업이 도구보다 오래돼도 복구된다)."""
    if check_copy:
        _check_db_file(home / DB_REL, expected_counts=manifest["counts"],
                       expected_meta={"study_id": manifest["study_id"], "data_mode": manifest["data_mode"], "dataset_version": manifest["dataset_version"],
                                      "db_schema_version": manifest["db_schema_version"]})
    with Db.open(home / DB_REL) as db:
        st = db.status()
        if not st["ok"]:
            raise BackupError("restored_integrity", f"integrity {st['integrity_check']}, fk {len(st['foreign_key_check'])}")
        if st["dataset_version"] != manifest["dataset_version"] or st["study_id"] != manifest["study_id"] or st["data_mode"] != manifest["data_mode"]:
            raise BackupError("restored_meta", "복구본의 버전·study_id·data_mode 가 manifest 와 다르다")
        if st["db_schema_version"] < manifest["db_schema_version"]:
            raise BackupError("restored_schema", f"복구본 db_schema {st['db_schema_version']} < manifest {manifest['db_schema_version']}")
        counts = _table_counts(db.conn)
        for k, v in manifest["counts"].items():
            if k == "schema_migrations":
                if counts.get(k, 0) < v:
                    raise BackupError("restored_counts", f"{k}: 복구본 {counts.get(k)}, manifest {v}")
            elif counts.get(k) != v:
                raise BackupError("restored_counts", f"{k}: 복구본 {counts.get(k)}, manifest {v}")
        c = check_photos(db, home)
        if not c["ok_all"]:
            raise BackupError("restored_photos", f"누락 {len(c['missing'])}, 불일치 {len(c['mismatched'])}")
        if c["unreferenced"]:
            raise BackupError("restored_unreferenced", f"참조되지 않은 파일: {c['unreferenced'][:3]}")
        rw = check_raw_files(db, home)
        if not rw["ok_all"]:
            raise BackupError("restored_raw", f"수집 원본 누락 {len(rw['missing'])}, 불일치 {len(rw['mismatched'])}")
        return {"counts": counts, "photos": c["ok"], "raw_files": rw["ok"], "db_schema_version": st["db_schema_version"]}
