"""정본 SQLite 저장소 (J5-009). 열기·마이그레이션·상태·시드 승계·기록/근거/첨부 삽입.

규칙 (AGENTS 정본 절, 데이터 사전 §2·§3.2):
- 모든 연결에서 외래키를 활성화하고 실제로 켜졌는지 확인한다.
- 쓰기는 `with db.transaction():` 안에서만 한다. 실패하면 전부 되돌린다.
- 이 모듈은 파일을 검사하지 않는다 (패키지 검사는 j5.package, 반영기는 J5-010).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from j5.db import schema as S
from j5.db.validate import ValidationError, is_sha256, is_uuid, now_utc, parse_date, parse_datetime, record_errors, seed_asset_errors
from j5.schemas_loader import schema_errors

DB_FILENAME = "j5.sqlite3"


class DbError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SeedResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    dataset_version: int = 0  # 반영 후 값. 신규·갱신이 있으면 1 올라간다 (데이터 사전 §4)
    asset_ids: list[str] = field(default_factory=list)


def default_db_path(data_home: Path) -> Path:
    return Path(data_home) / "db" / DB_FILENAME


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)  # 자동 커밋 없음: 트랜잭션은 명시적으로 연다
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise DbError("foreign_keys_off", "이 SQLite 는 외래키를 켤 수 없다. 정본을 열지 않는다")
    return conn


class Db:
    """정본 연결. `Db.create()` 로 새로 만들고 `Db.open()` 으로 연다."""

    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.conn = conn
        self.path = path
        self._depth = 0
        # 정본 시각(recorded_at)의 시계. 입력이 정본 시각을 정하지 못하게 저장소가 항상 부여한다. 테스트는 이 속성으로 고정한다.
        self.now = now_utc

    # ---- 열기·만들기 ----
    @staticmethod
    def check_sqlite_version() -> None:
        v = sqlite3.sqlite_version_info
        if v < S.MIN_SQLITE_VERSION:
            raise DbError("sqlite_too_old", f"SQLite {sqlite3.sqlite_version} 는 지원하지 않는다 (최소 {'.'.join(map(str, S.MIN_SQLITE_VERSION))})")

    @classmethod
    def create(cls, path: Path, *, study_id: str, data_mode: str) -> "Db":
        cls.check_sqlite_version()
        path = Path(path)
        if path.exists():
            raise DbError("exists", f"이미 있다: {path}. 기존 정본은 덮어쓰지 않는다")
        if not study_id or study_id != study_id.strip() or len(study_id) > 100:
            raise DbError("bad_study_id", "study_id 는 1~100자, 앞뒤 공백 없음")
        if data_mode not in S.DATA_MODES:
            raise DbError("bad_data_mode", f"data_mode 는 {'/'.join(S.DATA_MODES)} 중 하나")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(path)
        db = cls(conn, path)
        try:
            with db.transaction():
                db._apply_migrations()
                now = now_utc()
                for k, v in (("study_id", study_id), ("data_mode", data_mode), ("dataset_version", "0"), ("created_at", now)):
                    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (k, v))
        except BaseException:
            conn.close()
            path.unlink(missing_ok=True)
            raise
        return db

    @classmethod
    def open(cls, path: Path) -> "Db":
        cls.check_sqlite_version()
        path = Path(path)
        if not path.is_file():
            raise DbError("missing", f"정본 파일이 없다: {path}")
        conn = _connect(path)
        db = cls(conn, path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "schema_migrations" not in tables or "meta" not in tables:
                raise DbError("not_j5", f"j5 정본이 아니다: {path}")
            current = db.schema_version()
            if current > S.DB_SCHEMA_VERSION:
                raise DbError("tool_too_old", f"정본 스키마 {current} 이 도구가 아는 {S.DB_SCHEMA_VERSION} 보다 새롭다. 도구를 갱신한다")
            if current < S.DB_SCHEMA_VERSION:
                with db.transaction():
                    db._apply_migrations()
            for key in ("study_id", "data_mode", "dataset_version"):
                if db.meta(key) is None:
                    raise DbError("meta_missing", f"meta.{key} 가 없다")
        except BaseException:
            conn.close()
            raise
        return db

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Db":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 트랜잭션 ----
    @contextmanager
    def transaction(self):
        """바깥은 BEGIN IMMEDIATE, 중첩은 SAVEPOINT. 예외면 그 단위의 쓰기를 되돌린다.
        중첩 단위의 실패를 바깥에서 잡더라도 실패한 단위의 쓰기는 이미 되돌려져 있어 부분 반영이 커밋되지 않는다."""
        depth = self._depth
        if depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT sp{depth}")
        self._depth = depth + 1
        try:
            yield
        except BaseException:
            self._depth = depth
            if depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO sp{depth}")
                self.conn.execute(f"RELEASE sp{depth}")
            raise
        else:
            self._depth = depth
            if depth == 0:
                try:
                    self.conn.execute("COMMIT")
                except sqlite3.Error:
                    # COMMIT 이 실패(잠금·디스크)하면 트랜잭션이 열린 채 남을 수 있다. 되돌려서 미커밋 쓰기가 보이지 않게 한다.
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            else:
                self.conn.execute(f"RELEASE sp{depth}")

    def _require_tx(self) -> None:
        if self._depth == 0:
            raise DbError("no_transaction", "쓰기는 transaction() 안에서만 한다")

    # ---- 스키마·메타 ----
    def _apply_migrations(self) -> None:
        applied = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")} if self._has_table("schema_migrations") else set()
        for version, name, sql in S.MIGRATIONS:
            if version in applied:
                continue
            # executescript 는 열린 트랜잭션을 커밋해 버리므로 문장 단위로 실행한다.
            for stmt in _split_statements(sql):
                self.conn.execute(stmt)
            self.conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)", (version, name, now_utc()))
        self.conn.execute(f"PRAGMA user_version = {S.DB_SCHEMA_VERSION}")

    def _has_table(self, name: str) -> bool:
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None

    def schema_version(self) -> int:
        row = self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self._require_tx()
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    @property
    def data_mode(self) -> str:
        return self.meta("data_mode") or ""

    def status(self, data_home: Path | None = None) -> dict:
        """정본 상태. data_home 을 주면 파생본 포인터·ZIP 파일까지 실제로 확인한다(없으면 '파일 미확인' 으로 표시)."""
        counts = {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("subjects", "assets", "source_documents", "records", "record_evidence", "attachments")}
        integrity = [r[0] for r in self.conn.execute("PRAGMA integrity_check")]
        fk = [dict(zip(("table", "rowid", "parent", "fkid"), r)) for r in self.conn.execute("PRAGMA foreign_key_check")]
        projection = None
        if self._has_table("projection_runs"):
            from j5.db.projection import projection_status  # 순환 import 방지
            projection = projection_status(self, data_home)
        return {
            "projection": projection,
            "path": str(self.path), "sqlite_version": sqlite3.sqlite_version,
            "db_schema_version": self.schema_version(), "tool_schema_version": S.DB_SCHEMA_VERSION,
            "study_id": self.meta("study_id"), "data_mode": self.data_mode, "dataset_version": int(self.meta("dataset_version") or 0),
            "created_at": self.meta("created_at"), "foreign_keys": self.conn.execute("PRAGMA foreign_keys").fetchone()[0],
            "counts": counts, "integrity_check": integrity, "foreign_key_check": fk,
            "ok": integrity == ["ok"] and not fk,
        }

    # ---- 시드 승계 (데이터 사전 §2: asset_id 를 그대로 승계) ----
    def bump_dataset_version(self) -> int:
        """정본 데이터가 바뀐 배치의 끝에서 호출한다. 중복 입력만 처리한 배치는 호출하지 않는다 (데이터 사전 §4)."""
        self._require_tx()
        v = int(self.meta("dataset_version") or 0) + 1
        self.set_meta("dataset_version", str(v))
        return v

    def load_seed(self, seed: list[dict]) -> SeedResult:
        """assets.seed.json 배열을 subjects/assets 에 반영한다. 전체 성공 또는 전체 거절.
        신규·갱신이 하나라도 있으면 dataset_version 을 1 올린다(정본이 바뀌었으므로 이전 파생본은 구본이 된다). 변화 없음만이면 올리지 않는다."""
        errs = schema_errors("assets_seed.schema.json", seed)
        for a in seed if not errs else []:
            errs += seed_asset_errors(a, self.data_mode)
        if errs:
            raise ValidationError(errs)
        result = SeedResult()
        now = self.now()
        with self.transaction():
            for a in seed:
                aid = a["asset_id"]
                subj = self.conn.execute("SELECT subject_type FROM subjects WHERE subject_id = ?", (aid,)).fetchone()
                if subj is None:
                    self.conn.execute("INSERT INTO subjects (subject_id, subject_type, recorded_at) VALUES (?, 'asset', ?)", (aid, now))
                elif subj[0] != "asset":
                    raise DbError("subject_type_conflict", f"{aid} 는 이미 {subj[0]} 로 등록되어 있다")
                lon, lat = (a["location_point"] if "location_point" in a else (None, None))
                new = {"label": a["label"], "lon": lon, "lat": lat, "address": a.get("address"), "seed_created_at": a["created_at"], "notes": a.get("notes")}
                cur = self.conn.execute("SELECT label, lon, lat, address, seed_created_at, notes FROM assets WHERE asset_id = ?", (aid,)).fetchone()
                if cur is None:
                    self.conn.execute(
                        "INSERT INTO assets (asset_id, label, lon, lat, address, data_mode, seed_created_at, notes, recorded_at, updated_at)"
                        " VALUES (:asset_id, :label, :lon, :lat, :address, :data_mode, :seed_created_at, :notes, :now, :now)",
                        {**new, "asset_id": aid, "data_mode": a["data_mode"], "now": now})
                    result.inserted += 1
                elif dict(cur) == new:
                    result.unchanged += 1
                else:
                    # 물건의 서술 필드만 갱신한다. 관심 단계·확인 상태·ID 는 시드로 바꾸지 않는다.
                    self.conn.execute(
                        "UPDATE assets SET label = :label, lon = :lon, lat = :lat, address = :address, seed_created_at = :seed_created_at, notes = :notes, updated_at = :now WHERE asset_id = :asset_id",
                        {**new, "asset_id": aid, "now": now})
                    result.updated += 1
                result.asset_ids.append(aid)
            result.dataset_version = self.bump_dataset_version() if (result.inserted or result.updated) else int(self.meta("dataset_version") or 0)
        return result

    # ---- 출처·기록·근거·첨부 ----
    def add_source_document(self, doc: dict) -> str:
        self._require_tx()
        errs = []
        if not is_uuid(doc.get("document_id")):
            errs.append("document_id 는 UUID")
        if doc.get("document_kind") not in S.DOCUMENT_KINDS:
            errs.append(f"document_kind 는 {'/'.join(S.DOCUMENT_KINDS)} 중 하나")
        if not doc.get("title"):
            errs.append("title 필요")
        if doc.get("sha256") is not None and not is_sha256(doc["sha256"]):
            errs.append("sha256 형식")
        if parse_datetime(doc.get("collected_at")) is None:
            errs.append("collected_at: 시간대 오프셋이 있는 ISO 8601 (자료 확보 시각)")
        sp = doc.get("source_published_at")
        if sp is not None and parse_date(sp) is None and parse_datetime(sp) is None:
            errs.append("source_published_at: 날짜 또는 시간대 있는 시각 또는 null")
        errs += _no_caller_recorded_at(doc)
        if errs:
            raise ValidationError(errs)
        self.conn.execute(
            "INSERT INTO source_documents (document_id, document_kind, title, terms, location, sha256, source_published_at, collected_at, recorded_at, notes)"
            " VALUES (:document_id, :document_kind, :title, :terms, :location, :sha256, :source_published_at, :collected_at, :recorded_at, :notes)",
            {"terms": None, "location": None, "sha256": None, "source_published_at": None, "notes": None, **doc, "recorded_at": self.now()})
        return doc["document_id"]

    def insert_record(self, rec: dict, *, evidence: list[dict] = (), attachments: list[dict] = ()) -> str:
        """records 한 행과 그 근거·첨부. rec 의 payload 는 딕셔너리로 받고 정규화 JSON 으로 저장한다.
        검증 실패는 ValidationError, 제약 위반은 sqlite3.IntegrityError 로 올라오며 트랜잭션은 호출자가 되돌린다."""
        self._require_tx()
        # recorded_at(정본 최초 반영 시각)은 PC 가 부여한다. 입력이 가져온 값은 거절해 기기 입력 시각·관측 시각과 섞이지 않게 한다 (데이터 사전 §1).
        errs = _no_caller_recorded_at(rec)
        rec = {"device_created_at": None, "source_published_at": None, "collected_at": None,
               "effective_from": None, "effective_to": None, "supersedes_id": None, **rec, "recorded_at": self.now()}
        errs += record_errors(rec)
        if errs:
            raise ValidationError(errs)
        payload_json = json.dumps(rec["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.conn.execute(
            "INSERT INTO records (record_id, subject_id, subject_type, record_type, source_kind, schema_version, payload_json, observed_at, observed_at_precision,"
            " device_created_at, source_published_at, collected_at, recorded_at, effective_from, effective_to, supersedes_id)"
            " VALUES (:record_id, :subject_id, :subject_type, :record_type, :source_kind, :schema_version, :payload_json, :observed_at, :observed_at_precision,"
            " :device_created_at, :source_published_at, :collected_at, :recorded_at, :effective_from, :effective_to, :supersedes_id)",
            {**rec, "payload_json": payload_json})
        for ev in evidence:
            self.add_evidence(rec["record_id"], ev)
        for att in attachments:
            self.add_attachment(rec["record_id"], att)
        return rec["record_id"]

    def add_evidence(self, record_id: str, ev: dict) -> None:
        self._require_tx()
        errs = _no_caller_recorded_at(ev)
        if errs:
            raise ValidationError(errs)
        self.conn.execute(
            "INSERT INTO record_evidence (record_id, field_path, document_id, locator, verification_status, recorded_at)"
            " VALUES (:record_id, :field_path, :document_id, :locator, :verification_status, :recorded_at)",
            {"field_path": "$", "locator": None, "verification_status": "unverified", **ev, "record_id": record_id, "recorded_at": self.now()})

    def add_attachment(self, record_id: str, att: dict) -> None:
        self._require_tx()
        errs = _no_caller_recorded_at(att)
        tags = att.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            errs.append("attachment.tags 는 문자열 배열")
        taken = att.get("taken_at")
        if taken is not None and parse_date(taken) is None and parse_datetime(taken) is None:
            errs.append("attachment.taken_at: 날짜 또는 시간대 있는 시각 또는 null")
        if errs:
            raise ValidationError(errs)
        self.conn.execute(
            "INSERT INTO attachments (attachment_id, record_id, rel_path, sha256, mime, bytes, taken_at, tags_json, original_ref, recorded_at)"
            " VALUES (:attachment_id, :record_id, :rel_path, :sha256, :mime, :bytes, :taken_at, :tags_json, :original_ref, :recorded_at)",
            {"taken_at": None, "original_ref": None, **att, "record_id": record_id, "tags_json": json.dumps(tags, ensure_ascii=False), "recorded_at": self.now()})

    # ---- 조회 ----
    def get_record(self, record_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM records WHERE record_id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def list_records(self, subject_id: str) -> list[dict]:
        ids = [r[0] for r in self.conn.execute("SELECT record_id FROM records WHERE subject_id = ? ORDER BY observed_at, record_id", (subject_id,))]
        return [self.get_record(i) for i in ids]

    def list_assets(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM assets ORDER BY label, asset_id")]


def _no_caller_recorded_at(d: dict) -> list[str]:
    return ["recorded_at 은 입력이 정하지 않는다. 정본 반영 시 PC 가 부여한다"] if "recorded_at" in d else []


def _split_statements(sql: str) -> list[str]:
    """마이그레이션 SQL 을 문장 단위로 나눈다. 트리거 본문(BEGIN ... END;) 은 한 문장으로 유지한다."""
    out: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        s = line.strip()
        if not s and not buf:
            continue
        buf.append(line)
        if s.upper().startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if s.upper().endswith("END;"):
                out.append("\n".join(buf)); buf = []; in_trigger = False
        elif s.endswith(";"):
            out.append("\n".join(buf)); buf = []
    if "".join(buf).strip():
        out.append("\n".join(buf))
    return out
