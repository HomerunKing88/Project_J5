"""J5-011: 조회 파생본·버전·게시. 완료 조건 "전량 생성, 실패 시 구본 유지" (릴리스 계획 §10), 데이터 사전 §4, ADR-03.

시험: 전량 생성·검증·게시(파일·manifest·ZIP·포인터·실행 기록), 정본 반영 후 구본 표시와 최신용 내보내기 차단,
DB 반영 후 파생본 실패(이전본·포인터 유지, 부분 산출물 격리), 빈 정본, 사진 포함·누락, 생성 중 정본 변경, 검증기 단독 사용, CLI.
가상자료만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from j5 import cli
from j5.db import projection as P
from j5.db.importer import import_package
from j5.db.projection import ProjectionError, build_projection, copy_latest, projection_status, read_latest, verify_projection_dir
from j5.db.store import Db
from tests.conftest import PACKAGES

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
STUDY = "j5-synthetic-study"
EVENT1 = "1a2b3c4d-0001-4000-8000-000000000001"


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))  # dataset_version 1
    yield d
    d.close()


def runs(db) -> list[tuple]:
    return [tuple(r) for r in db.conn.execute("SELECT status, source_dataset_version, scope_count, record_count FROM projection_runs ORDER BY finished_at, rowid")]


def test_publish_full_projection_and_pointer(db, home):
    r = build_projection(db, home)
    assert r.outcome == "published", r.to_text()
    assert r.source_dataset_version == 1 and r.counts == {"assets": 5, "located": 4, "records": 0, "attachments": 0, "photos": 0, "parcels": 0, "transactions": 0}
    out = home / r.output_dir
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "j5view" and manifest["source_dataset_version"] == 1 and manifest["study_id"] == STUDY and manifest["data_mode"] == "synthetic"
    assert manifest["scope_ids"] == sorted(a["asset_id"] for a in json.loads(SEED_PATH.read_text(encoding="utf-8")))
    assert manifest["generated_at"] == r.generated_at and manifest["run_id"] == r.run_id
    # assets.seed.json 은 시드 스키마 그대로라 폰 앱이 바로 불러올 수 있다
    seed = json.loads((out / "assets.seed.json").read_text(encoding="utf-8"))
    assert seed == json.loads(SEED_PATH.read_text(encoding="utf-8")), "정본에서 복원한 물건 목록이 원 시드와 같다"
    geo = json.loads((out / "assets.geojson").read_text(encoding="utf-8"))
    assert geo["type"] == "FeatureCollection" and len(geo["features"]) == 4 and len(geo["unlocated_asset_ids"]) == 1
    assert geo["source_dataset_version"] == 1 and geo["features"][0]["geometry"]["type"] == "Point"
    assert all(f["properties"]["observation_count"] == 0 for f in geo["features"])
    # 파일 해시 = manifest, ZIP 항목 = 파일, 포인터 = 게시본
    for f in manifest["files"]:
        assert hashlib.sha256((out / f["path"]).read_bytes()).hexdigest() == f["sha256"]
    with zipfile.ZipFile(out / r.zip_name) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == sorted(["manifest.json"] + [f["path"] for f in manifest["files"]])
        assert json.loads(zf.read("manifest.json")) == manifest
    assert hashlib.sha256((out / r.zip_name).read_bytes()).hexdigest() == r.zip_sha256
    ptr = read_latest(home)
    assert ptr["source_dataset_version"] == 1 and ptr["dir"] == r.output_dir and ptr["zip"] == r.zip_name and ptr["zip_sha256"] == r.zip_sha256
    assert runs(db) == [("published", 1, 5, 0)]
    st = projection_status(db, home)
    assert st["stale"] is False and st["state"] == "최신" and st["published_version"] == 1
    assert db.status(home)["projection"]["state"] == "최신" and db.status()["dataset_version"] == 1, "파생본 생성은 dataset_version 을 바꾸지 않는다"
    assert db.status()["projection"]["state"].startswith("기록상 v1") and db.status()["projection"]["pointer_checked"] is False, "data_home 없이는 '최신' 이라고 하지 않는다"
    # 검증기는 게시된 폴더에도 그대로 쓸 수 있다
    assert verify_projection_dir(out, expected_version=1)["counts"]["assets"] == 5


def test_records_and_photos_after_import(db, home):
    assert import_package(db, PACKAGES / "valid", home).outcome == "applied"
    r = build_projection(db, home, photos=True)
    assert r.outcome == "published" and r.counts == {"assets": 5, "located": 4, "records": 3, "attachments": 3, "photos": 2, "parcels": 0, "transactions": 0}
    out = home / r.output_dir
    rows = [json.loads(l) for l in (out / "records.jsonl").read_bytes().split(b"\n") if l]
    assert [x["record_id"] for x in rows] == sorted((x["record_id"] for x in rows), key=lambda i: [y["record_id"] for y in rows].index(i))
    e1 = next(x for x in rows if x["record_id"] == EVENT1)
    assert e1["payload"]["change_status"] == "change_observed" and e1["attachments"][0]["path"].startswith("photos/") and e1["recorded_at"].endswith("Z")
    assert all((out / a["path"]).is_file() for x in rows for a in x["attachments"])
    geo = json.loads((out / "assets.geojson").read_text(encoding="utf-8"))
    p1 = next(f["properties"] for f in geo["features"] if f["properties"]["asset_id"] == "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50")
    assert p1["observation_count"] == 1 and p1["last_observed_at"] == "2026-09-22T10:15:00+09:00" and p1["last_change_status"] == "change_observed"
    # 사진을 빼면 path 없이 참조만 남고 ZIP 이 작다
    r2 = build_projection(db, home)
    rows2 = [json.loads(l) for l in (home / r2.output_dir / "records.jsonl").read_bytes().split(b"\n") if l]
    assert all("path" not in a and a["sha256"] for x in rows2 for a in x["attachments"]) and r2.counts["photos"] == 0


def test_stale_after_new_import_blocks_export_until_regenerated(db, home, tmp_path):
    r1 = build_projection(db, home)
    assert import_package(db, PACKAGES / "valid", home).outcome == "applied"
    st = projection_status(db, home)
    assert st["stale"] and st["state"] == "구본 (정본 v2, 파생본 v1)" and st["published_version"] == 1
    assert "구본" in db.status()["projection"]["state"]
    with pytest.raises(ProjectionError) as e:
        copy_latest(db, home, tmp_path)
    assert e.value.code == "stale"
    assert not list(tmp_path.glob("*.j5view.zip"))
    c = copy_latest(db, home, tmp_path, allow_stale=True)
    assert c["stale"] and c["source_dataset_version"] == 1 and Path(c["dest"]).name == r1.zip_name
    assert (tmp_path / (r1.zip_name + ".sha256")).is_file()
    r2 = build_projection(db, home)
    assert r2.outcome == "published" and r2.source_dataset_version == 2 and read_latest(home)["source_dataset_version"] == 2
    assert projection_status(db, home)["stale"] is False
    dest2 = tmp_path / "d2"; dest2.mkdir()
    assert copy_latest(db, home, dest2)["stale"] is False
    assert [x[0:2] for x in runs(db)] == [("published", 1), ("published", 2)]


def test_failure_keeps_previous_projection_and_pointer(db, home, monkeypatch):
    """R1b 인수: DB 반영 후 파생본 실패. 정본은 반영된 채, 이전 파생본과 포인터는 그대로, 부분 산출물은 격리."""
    r1 = build_projection(db, home)
    assert import_package(db, PACKAGES / "valid", home).outcome == "applied"
    before = (read_latest(home), sorted(p.relative_to(home).as_posix() for p in (home / r1.output_dir).rglob("*")))

    def boom(*a, **k):
        raise ProjectionError("verify_hash", "시험용 검증 실패")
    monkeypatch.setattr(P, "verify_projection_dir", boom)
    r = build_projection(db, home)
    assert r.outcome == "failed" and any(f["code"] == "verify_hash" for f in r.findings)
    assert "정본은 반영된 그대로" in r.message and "이전 파생본 v1" in r.message
    assert read_latest(home) == before[0], "포인터 유지"
    assert sorted(p.relative_to(home).as_posix() for p in (home / r1.output_dir).rglob("*")) == before[1], "이전 파생본 유지"
    failed_dirs = list((home / P.PROJECTIONS_DIR).glob("failed-*"))
    assert len(failed_dirs) == 1 and (failed_dirs[0] / "manifest.json").is_file(), "부분 산출물은 failed- 폴더에 격리, 자동 삭제 없음"
    assert not list((home / P.PROJECTIONS_DIR).glob("ds2-*")) and not list((home / P.PROJECTIONS_DIR).glob(".tmp-*"))
    st = projection_status(db, home)
    assert st["stale"] and st["published_version"] == 1 and st["last_failed_at"] and "verify_hash" in st["last_failed_message"]
    assert db.status()["dataset_version"] == 2 and db.status()["counts"]["records"] == 3, "정본은 손대지 않는다"
    assert [x[0:2] for x in runs(db)] == [("published", 1), ("failed", 2)]
    # 원인 제거 후 재시도는 파생본만 다시 만든다 (정본 재입력 없음)
    monkeypatch.undo()
    r2 = build_projection(db, home)
    assert r2.outcome == "published" and r2.source_dataset_version == 2 and db.status()["counts"]["records"] == 3


def test_empty_db_and_missing_photo_fail_without_publishing(home, tmp_path):
    with Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic") as empty:
        r = build_projection(empty, home)
        assert r.outcome == "failed" and any(f["code"] == "no_assets" for f in r.findings)
        assert read_latest(home) is None and "게시된 파생본이 없다" in r.message
        assert runs(empty) == [("failed", 0, 0, 0)]
    with Db.open(home / "db" / "j5.sqlite3") as db:
        db.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
        assert import_package(db, PACKAGES / "valid", home).outcome == "applied"
        victim = next(home.joinpath("photos").rglob("*.png"))
        victim.write_bytes(b"corrupted")
        r = build_projection(db, home, photos=True)
        assert r.outcome == "failed" and any(f["code"] == "photo_hash_mismatch" for f in r.findings)
        assert read_latest(home) is None
        victim.unlink()
        r = build_projection(db, home, photos=True)
        assert r.outcome == "failed" and any(f["code"] == "photo_missing" for f in r.findings)
        assert build_projection(db, home).outcome == "published", "사진을 빼면 게시된다"


def test_version_change_during_generation_is_not_published(db, home, monkeypatch):
    orig = P._write_zip

    def bump_then_zip(*a, **k):
        with db.transaction():
            db.bump_dataset_version()  # 생성 중 다른 배치가 정본을 바꿈 (시험)
        return orig(*a, **k)
    monkeypatch.setattr(P, "_write_zip", bump_then_zip)
    r = build_projection(db, home)
    assert r.outcome == "failed" and any(f["code"] == "version_changed" for f in r.findings)
    assert read_latest(home) is None and db.status()["dataset_version"] == 2


def test_verifier_catches_tampering(db, home):
    r = build_projection(db, home)
    out = home / r.output_dir
    assert verify_projection_dir(out, expected_version=1)
    with pytest.raises(ProjectionError) as e:
        verify_projection_dir(out, expected_version=2)
    assert e.value.code == "verify_version"
    seed_path = out / "assets.seed.json"
    original = seed_path.read_bytes()
    seed_path.write_bytes(original.replace(b"\xea\xb0\x80\xec\x83\x81 \xeb\xac\xbc\xea\xb1\xb4 1", b"\xea\xb0\x80\xec\x83\x81 \xeb\xac\xbc\xea\xb1\xb4 X"))  # '가상 물건 1' → '가상 물건 X'
    with pytest.raises(ProjectionError) as e:
        verify_projection_dir(out)
    assert e.value.code == "verify_hash"
    seed_path.write_bytes(original)
    (out / "extra.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ProjectionError) as e:
        verify_projection_dir(out)
    assert e.value.code == "verify_extra_or_missing"


class _Conn:
    """sqlite3.Connection 대리자. 지정한 SQL 을 지정 횟수째에 실패시켜 잠금·디스크 오류를 흉내 낸다."""

    def __init__(self, real, fail_sql: str, fail_on_nth: int, message: str):
        self._real, self._fail_sql, self._nth, self._msg, self._count = real, fail_sql, fail_on_nth, message, 0

    def execute(self, sql, *args):
        if sql == self._fail_sql:
            self._count += 1
            if self._count == self._nth:
                raise __import__("sqlite3").OperationalError(self._msg)
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_bookkeeping_failure_keeps_or_restores_previous_pointer(db, home, monkeypatch):
    """게시 기록 행 삽입 실패(잠금) 와 커밋 실패(디스크) 모두에서 포인터는 이전 상태를 유지한다 (Codex P1)."""
    r1 = build_projection(db, home)
    prev = (home / P.PROJECTIONS_DIR / P.LATEST_POINTER).read_bytes()
    assert import_package(db, PACKAGES / "valid", home).outcome == "applied"
    # (a) 기록 행 삽입이 실패: 포인터 교체 전이라 그대로
    real_conn = db.conn
    db.conn = _Conn(real_conn, "INSERT INTO projection_runs (run_id, source_dataset_version, projection_schema_version, data_mode, status, output_dir, zip_name, zip_sha256, scope_count, record_count, started_at, finished_at, message, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 1, "database is locked (시험)")
    r = build_projection(db, home)
    db.conn = real_conn
    assert r.outcome == "failed" and any(f["code"] == "OperationalError" for f in r.findings)
    assert (home / P.PROJECTIONS_DIR / P.LATEST_POINTER).read_bytes() == prev
    assert not list((home / P.PROJECTIONS_DIR).glob("ds2-*")) and len(list((home / P.PROJECTIONS_DIR).glob("failed-*"))) == 1
    assert projection_status(db, home)["published_version"] == 1 and (home / r1.output_dir / r1.zip_name).is_file()
    # (b) 커밋이 실패: 포인터는 이미 바뀌었으므로 이전 내용으로 되돌린다. COMMIT 은 스냅샷 읽기(1) → 게시(2) 순
    db.conn = _Conn(real_conn, "COMMIT", 2, "disk I/O error (시험)")
    r = build_projection(db, home)
    db.conn = real_conn
    assert r.outcome == "failed" and any(f["code"] == "OperationalError" for f in r.findings)
    assert (home / P.PROJECTIONS_DIR / P.LATEST_POINTER).read_bytes() == prev, "커밋 실패 뒤 포인터 복구"
    assert not list((home / P.PROJECTIONS_DIR).glob("ds2-*")) and len(list((home / P.PROJECTIONS_DIR).glob("failed-*"))) == 2
    st = projection_status(db, home)
    assert st["published_version"] == 1 and st["pointer_version"] == 1 and st["stale"]
    # 정상 재시도
    r2 = build_projection(db, home)
    assert r2.outcome == "published" and read_latest(home)["source_dataset_version"] == 2


def test_status_verifies_pointer_and_files(db, home, tmp_path):
    r = build_projection(db, home)
    zip_path = home / r.output_dir / r.zip_name
    zip_path.write_bytes(b"damaged")
    st = projection_status(db, home)
    assert st["stale"] and st["pointer_problem"] == "zip_hash_mismatch" and st["state"].startswith("손상")
    with pytest.raises(ProjectionError) as e:
        copy_latest(db, home, tmp_path)
    assert e.value.code == "projection_damaged"
    zip_path.unlink()
    assert projection_status(db, home)["pointer_problem"] == "zip_missing"
    (home / P.PROJECTIONS_DIR / P.LATEST_POINTER).unlink()
    st = projection_status(db, home)
    assert st["pointer_problem"] == "pointer_missing" and st["state"].startswith("없음") and st["published_version"] == 1, "기록만 남고 파일이 없으면 없음으로 표시"
    assert db.status(home)["projection"]["state"].startswith("없음")
    assert build_projection(db, home).outcome == "published" and projection_status(db, home)["state"] == "최신"


def test_sqlite_errors_become_failed_results(db, home, capsys, monkeypatch):
    real_conn = db.conn
    db.conn = _Conn(real_conn, "BEGIN IMMEDIATE", 1, "database is locked (시험)")
    r = build_projection(db, home)
    db.conn = real_conn
    assert r.outcome == "failed" and any(f["code"] == "OperationalError" and "locked" in f["message"] for f in r.findings)
    assert read_latest(home) is None and runs(db)[-1][0] == "failed"
    # CLI 는 SQLite 오류를 종료 코드 1 과 안내로 끝낸다 (역추적 없음)
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    monkeypatch.setattr(cli, "build_projection", lambda *a, **k: (_ for _ in ()).throw(__import__("sqlite3").OperationalError("database is locked")))
    assert cli.main(["db", "--db", str(db.path), "project"]) == 1
    assert "SQLite 오류" in capsys.readouterr().err


def test_cli_project_status_and_copy(home, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "init", "--study-id", STUDY, "--data-mode", "synthetic"]) == 0
    assert cli.main(["db", "load-seed", str(SEED_PATH)]) == 0
    capsys.readouterr()
    assert cli.main(["db", "project"]) == 0
    out = capsys.readouterr().out
    assert "파생본: 게시됨" in out and "source_dataset_version 1" in out and "백업 완료를 뜻하지 않는다" in out
    assert cli.main(["db", "status"]) == 0
    assert "파생본: 최신" in capsys.readouterr().out
    assert cli.main(["db", "import", str(PACKAGES / "valid")]) == 0
    capsys.readouterr()
    assert cli.main(["db", "status"]) == 0
    assert "파생본: 구본 (정본 v2, 파생본 v1)" in capsys.readouterr().out
    dest = tmp_path / "usb"; dest.mkdir()
    assert cli.main(["db", "project-copy", str(dest)]) == 1
    assert "구본" in capsys.readouterr().err and not list(dest.glob("*.zip"))
    assert cli.main(["db", "project-copy", str(dest), "--allow-stale"]) == 0
    assert "구본" in capsys.readouterr().out and len(list(dest.glob("*.j5view.zip"))) == 1
    assert cli.main(["db", "project", "--photos", "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["outcome"] == "published" and j["source_dataset_version"] == 2 and j["counts"]["photos"] == 2
    monkeypatch.delenv("J5_DATA_HOME")
    assert cli.main(["db", "--db", str(home / "db" / "j5.sqlite3"), "project"]) == cli.USAGE_ERROR
    assert "data-home" in capsys.readouterr().err
