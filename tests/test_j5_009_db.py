"""J5-009: 정본 SQLite. 완료 조건 "FK·타입·시점·JSON 검증" (릴리스 계획 §10), ADR-09 참조 무결성, records 불변.

가상 시드·가상 fixture 이벤트만 쓴다.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.store import Db, DbError, default_db_path
from j5.db.validate import (RECORD_TYPE_SUBJECTS, ValidationError, is_utc_iso, now_utc, observed_at_errors, parse_date,
                            parse_datetime, record_errors, to_utc_iso)

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / "tests/fixtures/assets.seed.synthetic.json"
EVENTS_PATH = ROOT / "tests/fixtures/packages/valid/observations.jsonl"
DOC_ID = "9d2e6b1c-5f4a-4c3b-9e8d-7a6b5c4d3e2f"
A1 = "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50"
U = lambda n: f"1a2b3c4d-{n:04d}-4000-8000-000000000001"  # noqa: E731


def seed() -> list[dict]:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


def events() -> list[dict]:
    return [json.loads(l) for l in EVENTS_PATH.read_text(encoding="utf-8").splitlines() if l]


FIXED_NOW = "2026-09-22T01:20:00Z"


@pytest.fixture
def db(tmp_path):
    d = Db.create(tmp_path / "data" / "db" / "j5.sqlite3", study_id="j5-synthetic-study", data_mode="synthetic")
    d.now = lambda: FIXED_NOW  # 정본 시각은 저장소의 시계가 부여한다. 테스트는 고정한다
    yield d
    d.close()


@pytest.fixture
def seeded(db):
    db.load_seed(seed())
    with db.transaction():
        db.add_source_document({"document_id": DOC_ID, "document_kind": "field_package", "title": "valid.j5field.zip (가상)",
                                "terms": "가상자료", "location": "inbox/valid.j5field.zip", "sha256": "0" * 64, "collected_at": "2026-09-22T10:20:00+09:00"})
    return db


def field_record(**over) -> dict:
    base = {"record_id": U(1), "subject_id": A1, "subject_type": "asset", "record_type": "field_observation", "source_kind": "field_observation",
            "schema_version": "1.0.0", "payload": {"change_status": "no_change", "note": None},
            "observed_at": "2026-09-22T10:15:00+09:00", "observed_at_precision": "datetime", "device_created_at": "2026-09-22T10:16:30+09:00"}
    base.update(over)
    return base


def event_to_record(ev: dict) -> tuple[dict, list[dict]]:
    """R1a 이벤트 → records 행 (J5-010 반영기의 매핑을 미리 시험한다). event_id 를 record_id 로 승계한다."""
    rec = {"record_id": ev["event_id"], "subject_id": ev["asset_id"], "subject_type": "asset", "record_type": ev["record_type"],
           "source_kind": "field_observation", "schema_version": "1.0.0", "payload": ev["payload"],
           "observed_at": ev["observed_at"], "observed_at_precision": ev["observed_at_precision"], "device_created_at": ev["device_created_at"],
           "supersedes_id": ev["corrects_event_id"]}
    # 시험용 첨부 ID: 이벤트 ID 조각 + 순번 + 사진 해시 조각으로 만든 UUID 모양 (반영기의 실제 규칙은 J5-010 에서 정한다)
    atts = [{"attachment_id": f"aa{ev['event_id'][2:8]}-{i:04x}-4000-8000-{ref['sha256'][:12]}",
             "rel_path": ref["path"], "sha256": ref["sha256"], "mime": ref["mime"], "bytes": ref["bytes"], "tags": ref["tags"]}
            for i, ref in enumerate(ev["attachment_refs"])]
    return rec, atts


# ---- 생성·열기·마이그레이션 ----

def test_create_open_status_and_foreign_keys(db):
    st = db.status()
    assert st["ok"] and st["foreign_keys"] == 1 and st["db_schema_version"] == S.DB_SCHEMA_VERSION == 3
    assert st["study_id"] == "j5-synthetic-study" and st["data_mode"] == "synthetic" and st["dataset_version"] == 0
    assert is_utc_iso(st["created_at"])
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == S.DB_SCHEMA_VERSION
    for t in ("subjects", "assets", "source_documents", "records", "record_evidence", "attachments", "meta", "schema_migrations"):
        assert "STRICT" in db.conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()[0]
    db.close()
    with Db.open(db.path) as again:
        assert again.status()["ok"] and again.schema_version() == S.DB_SCHEMA_VERSION
        assert again.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_create_refuses_overwrite_and_bad_inputs(tmp_path, db):
    with pytest.raises(DbError) as e:
        Db.create(db.path, study_id="x", data_mode="synthetic")
    assert e.value.code == "exists"
    with pytest.raises(DbError) as e:
        Db.create(tmp_path / "a.sqlite3", study_id=" x", data_mode="synthetic")
    assert e.value.code == "bad_study_id"
    with pytest.raises(DbError) as e:
        Db.create(tmp_path / "b.sqlite3", study_id="x", data_mode="real")
    assert e.value.code == "bad_data_mode"
    assert not (tmp_path / "a.sqlite3").exists() and not (tmp_path / "b.sqlite3").exists()


def test_open_rejects_non_j5_and_newer_schema(tmp_path, db):
    other = tmp_path / "other.sqlite3"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(DbError) as e:
        Db.open(other)
    assert e.value.code == "not_j5"
    with pytest.raises(DbError) as e:
        Db.open(tmp_path / "none.sqlite3")
    assert e.value.code == "missing"
    with db.transaction():
        db.conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (99, 'future', ?)", (now_utc(),))
    db.close()
    with pytest.raises(DbError) as e:
        Db.open(db.path)
    assert e.value.code == "tool_too_old"


def test_writes_require_transaction_and_rollback(seeded):
    with pytest.raises(DbError) as e:
        seeded.insert_record(field_record())
    assert e.value.code == "no_transaction"
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.insert_record(field_record(record_id=U(1)))
            seeded.insert_record(field_record(record_id=U(1)))  # PK 중복 → 전체 롤백
    assert seeded.get_record(U(1)) is None
    assert seeded.conn.in_transaction is False


def test_nested_failure_is_rolled_back_even_if_caught(db):
    """중첩 단위(load_seed)가 실패했는데 바깥이 예외를 삼켜도, 실패한 단위의 쓰기는 커밋되지 않는다 (전체 성공/전체 거절)."""
    s = seed()
    s.append(s.pop(0))  # 충돌 물건(A1)을 마지막에 두어 앞의 4개가 먼저 써지게 한다
    with db.transaction():
        db.conn.execute("INSERT INTO subjects (subject_id, subject_type, recorded_at) VALUES (?, 'parcel', ?)", (A1, now_utc()))
    with db.transaction():
        db.set_meta("note", "outer")
        try:
            db.load_seed(s)
        except DbError as e:
            assert e.code == "subject_type_conflict"
    st = db.status()
    assert st["counts"]["assets"] == 0 and st["counts"]["subjects"] == 1, "실패한 시드 단위의 쓰기는 되돌려졌다"
    assert st["dataset_version"] == 0 and db.meta("note") == "outer", "바깥 단위의 쓰기는 남는다"
    assert db.conn.in_transaction is False
    # 중첩이 성공하면 바깥과 함께 커밋된다
    with db.transaction():
        db.conn.execute("DELETE FROM subjects WHERE subject_id = ?", (A1,))
        r = db.load_seed(seed())
    assert r.inserted == 5 and db.status()["counts"]["assets"] == 5 and db.status()["dataset_version"] == 1


# ---- 시드 승계 ----

def test_load_seed_inherits_ids_and_is_idempotent(db):
    s = seed()
    r = db.load_seed(s)
    assert (r.inserted, r.updated, r.unchanged, r.dataset_version) == (5, 0, 0, 1)
    assert r.asset_ids == [a["asset_id"] for a in s]
    assets = {a["asset_id"]: a for a in db.list_assets()}
    assert assets[A1]["lon"] == 126.9986 and assets[A1]["lat"] == 37.5702 and assets[A1]["address"] is None
    assert assets[s[2]["asset_id"]]["lon"] is None and assets[s[2]["asset_id"]]["address"].startswith("서울 종로구 가상로 3")
    assert all(a["tracking_status"] == "unreviewed" and a["resolution_status"] == "confirmed" for a in assets.values())
    r2 = db.load_seed(s)
    assert (r2.inserted, r2.updated, r2.unchanged, r2.dataset_version) == (0, 0, 5, 1), "변화 없음만이면 dataset_version 유지"
    s[0]["label"] = "가상 물건 1 (이름 변경)"
    with db.transaction():
        db.conn.execute("UPDATE assets SET tracking_status = 'watch' WHERE asset_id = ?", (A1,))
    r3 = db.load_seed(s)
    assert (r3.inserted, r3.updated, r3.unchanged, r3.dataset_version) == (0, 1, 4, 2), "정본이 바뀐 배치는 dataset_version 을 올린다"
    a1 = {a["asset_id"]: a for a in db.list_assets()}[A1]
    assert a1["label"] == "가상 물건 1 (이름 변경)" and a1["tracking_status"] == "watch", "관심 단계는 시드로 바뀌지 않는다"
    assert db.status()["dataset_version"] == 2


def test_load_seed_rejects_whole_batch(db):
    s = seed()
    s[3]["data_mode"] = "private_real"
    with pytest.raises(ValidationError) as e:
        db.load_seed(s)
    assert any("data_mode" in m for m in e.value.errors)
    assert db.status()["counts"]["assets"] == 0 and db.status()["dataset_version"] == 0, "일부만 반영하지 않고 버전도 올리지 않는다"
    bad = seed()
    bad[0]["extra"] = 1
    with pytest.raises(ValidationError):
        db.load_seed(bad)
    bad = seed()
    bad[1]["created_at"] = "2026-02-30"
    with pytest.raises(ValidationError) as e:
        db.load_seed(bad)
    assert any("created_at" in m for m in e.value.errors), "스키마의 date 형식 검사 또는 달력 검사가 잡는다"
    assert db.status()["counts"]["subjects"] == 0


def test_load_seed_rejects_subject_type_conflict(db):
    with db.transaction():
        db.conn.execute("INSERT INTO subjects (subject_id, subject_type, recorded_at) VALUES (?, 'parcel', ?)", (A1, now_utc()))
    with pytest.raises(DbError) as e:
        db.load_seed(seed())
    assert e.value.code == "subject_type_conflict"
    assert db.status()["counts"]["assets"] == 0


# ---- FK (ADR-09) ----

def test_fk_record_needs_existing_subject_with_matching_type(seeded):
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.insert_record(field_record(subject_id=U(77)))  # 없는 대상
    with pytest.raises(ValidationError) as e:
        with seeded.transaction():
            seeded.insert_record(field_record(subject_type="parcel"))  # 타입 조합은 공통 검증이 먼저 잡는다
    assert any("대상은 asset" in m for m in e.value.errors)
    # 검증을 우회해 직접 넣어도 복합 FK 가 막는다: A1 은 asset 으로만 존재
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute(
                "INSERT INTO records (record_id, subject_id, subject_type, record_type, source_kind, schema_version, payload_json, observed_at, observed_at_precision, recorded_at)"
                " VALUES (?, ?, 'parcel', 'field_observation', 'field_observation', '1.0.0', '{}', '2026-09-22', 'date', ?)", (U(2), A1, now_utc()))
    assert seeded.status()["counts"]["records"] == 0


def test_fk_asset_row_needs_asset_subject_and_children_need_parents(seeded):
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("INSERT INTO assets (asset_id, label, address, data_mode, seed_created_at, recorded_at, updated_at) VALUES (?, 'x', 'addr', 'synthetic', '2026-09-22', ?, ?)",
                                (U(9), now_utc(), now_utc()))
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("INSERT INTO assets (asset_id, subject_type, label, address, data_mode, seed_created_at, recorded_at, updated_at) VALUES (?, 'parcel', 'x', 'addr', 'synthetic', '2026-09-22', ?, ?)",
                                (A1, now_utc(), now_utc()))
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.add_evidence(U(5), {"document_id": DOC_ID})  # 없는 기록
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.insert_record(field_record(), evidence=[{"document_id": U(6)}])  # 없는 출처 문서
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.insert_record(field_record(supersedes_id=U(8)))  # 없는 이전 기록
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("DELETE FROM subjects WHERE subject_id = ?", (A1,))  # 자식(assets) 이 있는 부모
    assert seeded.status()["ok"] and seeded.status()["counts"]["records"] == 0


# ---- 타입 (STRICT·CHECK) ----

def test_strict_types_and_enums(seeded):
    now = now_utc()
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("UPDATE assets SET lon = 'east' WHERE asset_id = ?", (A1,))
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("UPDATE assets SET lon = 181.0 WHERE asset_id = ?", (A1,))
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("UPDATE assets SET lon = NULL WHERE asset_id = ?", (A1,))  # lon 만 NULL (lat 는 남음)
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("UPDATE assets SET tracking_status = 'hot' WHERE asset_id = ?", (A1,))
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("UPDATE assets SET lon = NULL, lat = NULL, address = NULL WHERE asset_id = ?", (A1,))  # 위치점·주소 둘 다 없음
    with seeded.transaction():
        seeded.insert_record(field_record())
    for bad in ({"bytes": "ten"}, {"bytes": 0}, {"mime": "image/heic"}, {"sha256": "zz"}, {"rel_path": "/abs/x.png"}, {"rel_path": "photos/../x.png"},
                {"rel_path": "photos\\x.png"}, {"attachment_id": "not-uuid"}):
        att = {"attachment_id": U(3), "rel_path": "photos/" + "b" * 64 + ".png", "sha256": "b" * 64, "mime": "image/png", "bytes": 10, "tags": ["front"], **bad}
        with pytest.raises((sqlite3.IntegrityError, ValidationError)):
            with seeded.transaction():
                seeded.add_attachment(U(1), att)
    with pytest.raises(ValidationError):
        with seeded.transaction():
            seeded.add_attachment(U(1), {"attachment_id": U(3), "rel_path": "p.png", "sha256": "b" * 64, "mime": "image/png", "bytes": 1, "tags": "front"})
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("INSERT INTO record_evidence (record_id, field_path, document_id, recorded_at) VALUES (?, 'payload.note', ?, ?)", (U(1), DOC_ID, now))  # '$' 로 시작해야 함
    assert seeded.status()["counts"]["attachments"] == 0 and seeded.status()["counts"]["record_evidence"] == 0


# ---- 시점 ----

def test_time_validators():
    assert parse_date("2026-09-22") is not None and parse_date("2026-02-30") is None and parse_date("2026-9-2") is None
    assert parse_datetime("2026-09-22T10:15:00+09:00") is not None
    assert parse_datetime("2026-09-22T10:15:00Z") is not None
    assert parse_datetime("2026-09-22T10:15:00") is None, "오프셋 없는 시각은 거절"
    assert parse_datetime("2026-09-22T25:15:00+09:00") is None and parse_datetime("2026-13-01T00:00:00Z") is None
    assert is_utc_iso("2026-09-22T01:15:00Z") and not is_utc_iso("2026-09-22T10:15:00+09:00") and not is_utc_iso("2026-09-22T01:15:00.5Z")
    assert to_utc_iso(parse_datetime("2026-09-22T10:15:00.250+09:00")) == "2026-09-22T01:15:00Z"
    with pytest.raises(ValueError):
        to_utc_iso(__import__("datetime").datetime(2026, 9, 22))
    assert observed_at_errors("2026-09-22", "date") == [] and observed_at_errors("2026-09-22T10:15:00+09:00", "datetime") == []
    assert observed_at_errors("2026-09-22T10:15:00+09:00", "date"), "날짜 정밀도에 시각을 넣지 않는다"
    assert observed_at_errors("2026-09-22", "datetime"), "시각 정밀도에 날짜만 넣지 않는다"
    assert observed_at_errors("2026-09-22", "hour")


def test_record_time_rules_in_validation_and_db(seeded):
    cases = [
        ({"observed_at": "2026-09-22", "observed_at_precision": "datetime"}, "observed_at"),
        ({"observed_at": "2026-09-22T10:15:00+09:00", "observed_at_precision": "date"}, "observed_at"),
        ({"observed_at": "2026-02-30", "observed_at_precision": "date"}, "observed_at"),
        ({"observed_at": "2026-09-22T10:15:00", "observed_at_precision": "datetime"}, "observed_at"),
        ({"device_created_at": "2026-09-22 10:16"}, "device_created_at"),
        ({"effective_from": "yesterday"}, "effective_from"),
    ]
    for over, key in cases:
        errs = record_errors(field_record(recorded_at=FIXED_NOW, **over))
        assert any(key in m for m in errs), (over, errs)
    for bad in ("2026-09-22T10:20:00+09:00", "2026-09-22T01:20:00.123Z", "2026-09-22"):
        assert any("recorded_at" in m for m in record_errors(field_record(recorded_at=bad))), bad
    # 입력이 recorded_at 을 가져오면 거절한다 (기기 입력 시각·관측 시각과 정본 반영 시각의 분리)
    for fn in (lambda: seeded.insert_record(field_record(recorded_at=FIXED_NOW)),
               lambda: seeded.add_evidence(U(1), {"document_id": DOC_ID, "recorded_at": FIXED_NOW}),
               lambda: seeded.add_source_document({"document_id": U(9), "document_kind": "manual_entry", "title": "x", "collected_at": FIXED_NOW, "recorded_at": FIXED_NOW})):
        with pytest.raises(ValidationError) as e:
            with seeded.transaction():
                fn()
        assert any("입력이 정하지 않는다" in m for m in e.value.errors)
    # DB 쪽 모양 검사도 독립적으로 막는다 (검증 함수를 우회한 삽입)
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute(
                "INSERT INTO records (record_id, subject_id, subject_type, record_type, source_kind, schema_version, payload_json, observed_at, observed_at_precision, recorded_at)"
                " VALUES (?, ?, 'asset', 'field_observation', 'field_observation', '1.0.0', '{}', '2026-09-22T10:15:00+09:00', 'date', ?)", (U(2), A1, now_utc()))
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute(
                "INSERT INTO records (record_id, subject_id, subject_type, record_type, source_kind, schema_version, payload_json, observed_at, observed_at_precision, recorded_at)"
                " VALUES (?, ?, 'asset', 'field_observation', 'field_observation', '1.0.0', '{}', '2026-09-22', 'date', '2026-09-22T10:20:00+09:00')", (U(2), A1))
    with seeded.transaction():
        seeded.insert_record(field_record(record_id=U(2), observed_at="2026-09-22", observed_at_precision="date"))
    got = seeded.get_record(U(2))
    assert got["observed_at"] == "2026-09-22" and got["observed_at_precision"] == "date" and got["device_created_at"] == "2026-09-22T10:16:30+09:00"
    assert got["recorded_at"] == FIXED_NOW, "정본 시각은 저장소 시계가 부여한다"
    # 시계를 바꾸면 그 값이 들어간다 (실제 운영은 now_utc)
    seeded.now = now_utc
    with seeded.transaction():
        seeded.insert_record(field_record(record_id=U(3)))
    assert is_utc_iso(seeded.get_record(U(3))["recorded_at"])


# ---- JSON·payload ----

def test_json_payload_validation(seeded):
    for payload, key in [({"change_status": "maybe", "note": None}, "change_status"), ({"change_status": "no_change"}, "note"),
                         ({"change_status": "no_change", "note": None, "x": 1}, "x"), ({"change_status": "no_change", "note": "a" * 2001}, "note"),
                         ([], "payload"), ("{}", "payload")]:
        errs = record_errors(field_record(payload=payload))
        assert any(key in m for m in errs), (payload, errs)
    assert any("schema_version" in m for m in record_errors(field_record(schema_version="0.9.0")))
    assert any("source_kind" in m for m in record_errors(field_record(source_kind="rumor")))
    assert any("record_type" in m for m in record_errors(field_record(record_type="lease")))
    # DB 는 JSON 유효성·객체 여부·태그 배열을 독립적으로 검사한다
    for pj in ("{not json", "[]", "1"):
        with pytest.raises(sqlite3.IntegrityError):
            with seeded.transaction():
                seeded.conn.execute(
                    "INSERT INTO records (record_id, subject_id, subject_type, record_type, source_kind, schema_version, payload_json, observed_at, observed_at_precision, recorded_at)"
                    " VALUES (?, ?, 'asset', 'field_observation', 'field_observation', '1.0.0', ?, '2026-09-22', 'date', ?)", (U(2), A1, pj, now_utc()))
    with seeded.transaction():
        seeded.insert_record(field_record(payload={"note": "임대 광고", "change_status": "change_observed"}))
    raw = seeded.conn.execute("SELECT payload_json FROM records WHERE record_id = ?", (U(1),)).fetchone()[0]
    assert raw == '{"change_status":"change_observed","note":"임대 광고"}', "정규화 JSON (키 정렬·압축·비ASCII 보존)"
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("INSERT INTO attachments (attachment_id, record_id, rel_path, sha256, mime, bytes, tags_json, recorded_at) VALUES (?, ?, 'p.png', ?, 'image/png', 1, '{}', ?)",
                                (U(4), U(1), "c" * 64, now_utc()))


def test_record_type_subject_matrix_is_single_source():
    assert RECORD_TYPE_SUBJECTS == {"field_observation": frozenset({"asset"})}
    assert set(RECORD_TYPE_SUBJECTS) == set(S.RECORD_TYPES)
    assert all(t in S.SUBJECT_TYPES for ts in RECORD_TYPE_SUBJECTS.values() for t in ts)


# ---- 불변·정정·근거·첨부 ----

def test_records_are_immutable_and_corrections_chain(seeded):
    with seeded.transaction():
        seeded.insert_record(field_record(record_id=U(1)))
    for sql in ("UPDATE records SET observed_at = '2026-01-01' WHERE record_id = ?", "DELETE FROM records WHERE record_id = ?"):
        with pytest.raises(sqlite3.IntegrityError) as e:
            with seeded.transaction():
                seeded.conn.execute(sql, (U(1),))
        assert "불변" in str(e.value) or "삭제하지" in str(e.value)
    with pytest.raises(ValidationError):
        with seeded.transaction():
            seeded.insert_record(field_record(record_id=U(2), supersedes_id=U(2)))
    with seeded.transaction():
        seeded.insert_record(field_record(record_id=U(2), supersedes_id=U(1), payload={"change_status": "hard_to_confirm", "note": "정정"}))
    recs = seeded.list_records(A1)
    assert [r["record_id"] for r in recs] == [U(1), U(2)] and recs[1]["supersedes_id"] == U(1)
    assert recs[0]["payload"] == {"change_status": "no_change", "note": None}, "원 기록은 그대로"


def test_evidence_and_attachments(seeded):
    with seeded.transaction():
        seeded.insert_record(field_record(),
                             evidence=[{"document_id": DOC_ID, "locator": "observations.jsonl#event_id=" + U(1)},
                                       {"document_id": DOC_ID, "field_path": "$.payload.note", "verification_status": "verified"}],
                             attachments=[{"attachment_id": U(3), "rel_path": "photos/" + "b" * 64 + ".png", "sha256": "b" * 64, "mime": "image/png", "bytes": 69, "tags": ["front"]}])
    ev = [dict(r) for r in seeded.conn.execute("SELECT field_path, document_id, locator, verification_status FROM record_evidence ORDER BY evidence_id")]
    assert ev[0]["field_path"] == "$" and ev[0]["verification_status"] == "unverified" and ev[1]["field_path"] == "$.payload.note" and ev[1]["verification_status"] == "verified"
    att = dict(seeded.conn.execute("SELECT * FROM attachments").fetchone())
    assert att["tags_json"] == '["front"]' and att["bytes"] == 69 and att["record_id"] == U(1)
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.add_evidence(U(1), {"document_id": DOC_ID})  # 같은 기록·같은 경로·같은 문서 중복
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.add_attachment(U(1), {"attachment_id": U(4), "rel_path": "photos/x.png", "sha256": "b" * 64, "mime": "image/png", "bytes": 69, "tags": []})  # 같은 기록·같은 해시
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.conn.execute("DELETE FROM source_documents WHERE document_id = ?", (DOC_ID,))  # 근거가 참조 중
    for bad in ({"document_kind": "rumor"}, {"collected_at": "yesterday"}, {"collected_at": "2026-09-22"}, {"collected_at": "2026-09-22T10:00:00"},
                {"source_published_at": "last week"}, {"sha256": "xyz"}, {"title": ""}):
        with pytest.raises(ValidationError):
            with seeded.transaction():
                seeded.add_source_document({"document_id": U(5), "document_kind": "manual_entry", "title": "x", "collected_at": "2026-09-22T00:00:00Z", **bad})
    with seeded.transaction():
        seeded.add_source_document({"document_id": U(5), "document_kind": "official_file", "title": "고시 (가상)", "collected_at": "2026-09-22T00:00:00Z", "source_published_at": "2026-09-01"})
    with pytest.raises(ValidationError):
        with seeded.transaction():
            seeded.add_attachment(U(1), {"attachment_id": U(6), "rel_path": "photos/y.png", "sha256": "d" * 64, "mime": "image/png", "bytes": 1, "tags": [], "taken_at": "noon"})
    assert seeded.status()["ok"] and seeded.status()["counts"]["source_documents"] == 2


def test_fixture_events_map_to_records(seeded):
    """가상 fixture 의 정상 이벤트가 그대로 정본에 들어간다 (asset_id 승계·정정 연결·첨부)."""
    with seeded.transaction():
        for ev in events():
            rec, atts = event_to_record(ev)
            seeded.insert_record(rec, evidence=[{"document_id": DOC_ID, "locator": f"observations.jsonl#event_id={ev['event_id']}"}], attachments=atts)
    st = seeded.status()
    assert st["ok"] and st["counts"]["records"] == len(events()) and st["counts"]["record_evidence"] == len(events())
    assert st["counts"]["attachments"] == sum(len(e["attachment_refs"]) for e in events())
    for ev in events():
        got = seeded.get_record(ev["event_id"])
        assert got["subject_id"] == ev["asset_id"] and got["payload"] == ev["payload"] and got["observed_at"] == ev["observed_at"]
        assert got["supersedes_id"] == ev["corrects_event_id"]
    # 같은 이벤트를 다시 넣으면 PK 가 막는다 (멱등 처리는 J5-010 의 반영기가 해시로 판단)
    with pytest.raises(sqlite3.IntegrityError):
        with seeded.transaction():
            seeded.insert_record(event_to_record(events()[0])[0])


# ---- CLI ----

def test_cli_db_init_status_load_seed(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.delenv("J5_DATA_HOME", raising=False)
    assert cli.main(["db", "status"]) == cli.USAGE_ERROR
    assert "J5_DATA_HOME" in capsys.readouterr().err
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "init"]) == cli.USAGE_ERROR  # study_id·data_mode 없음
    assert cli.main(["db", "init", "--study-id", "j5-synthetic-study", "--data-mode", "synthetic"]) == 0
    assert default_db_path(home).is_file() and "정본 생성" in capsys.readouterr().out
    assert cli.main(["db", "init", "--study-id", "j5-synthetic-study", "--data-mode", "synthetic"]) == 1
    assert "exists" in capsys.readouterr().err
    assert cli.main(["db", "load-seed", str(SEED_PATH)]) == 0
    out = capsys.readouterr().out
    assert "신규 5" in out and "dataset_version 1" in out
    assert cli.main(["db", "load-seed", str(SEED_PATH)]) == 0
    out = capsys.readouterr().out
    assert "변화 없음 5" in out and "dataset_version 1" in out
    assert cli.main(["db", "status", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["ok"] and st["counts"]["assets"] == 5 and st["study_id"] == "j5-synthetic-study" and st["dataset_version"] == 1
    assert cli.main(["db", "status"]) == 0
    out = capsys.readouterr().out
    assert "외래키 켜짐" in out and "integrity_check: ok" in out
    # data_mode 가 다른 시드는 거절
    bad = tmp_path / "bad.json"
    s = seed(); s[0]["data_mode"] = "private_real"
    bad.write_text(json.dumps(s), encoding="utf-8")
    assert cli.main(["db", "load-seed", str(bad)]) == 1
    assert "data_mode" in capsys.readouterr().err
    # --db 로 명시 경로
    assert cli.main(["db", "--db", str(tmp_path / "x" / "y.sqlite3"), "init", "--study-id", "s", "--data-mode", "private_real"]) == 0
    assert cli.main(["db", "--db", str(tmp_path / "x" / "y.sqlite3"), "status"]) == 0
    capsys.readouterr()
