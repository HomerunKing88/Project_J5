"""J5-017A: 운영 점검·휴면 재개. 릴리스 계획 §8 R6 (반기 운영 점검, 정본·사진 복구, 새 PC 이전, 마지막 정상 앱·DB·스키마·도구 버전 저장),
§10 J5-017 완료 조건 "새 기기·구버전·백업 재개", 데이터 사전 §12.

시험: 할 일 목록과 통과 판정(백업·파생본 없음 → 있음), 마지막 정상 상태 파일은 통과할 때만 쓰고 실패 시 이전 것을 유지, 결과가 새로 쓴 파일을 돌려줌,
백업 내용 검증(manifest 만 남고 사진이 지워진 백업은 정상이 아님), 마지막 정상 파일을 쓸 수 없을 때의 조치 항목(트레이스백 없음), 읽기 전용 열기가
마이그레이션을 적용하지 않음(구버전 정본: 대기 목록만 보고, 열면 적용), 도구보다 새로운 정본 거절, 사진 누락, 새 PC 이전(빈 폴더 복구 → 점검 → 백업 재개),
반기 점검 기한 초과, 정본 없음, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.backup import create_backup, restore_backup
from j5.db.importer import import_package
from j5.db.ops import CHECK_INTERVAL_DAYS, LAST_GOOD, OPS_DIR, OPS_LOG, ops_check, ops_text, read_last_good
from j5.db.projection import LATEST_POINTER, PROJECTIONS_DIR, build_projection
from j5.db.store import Db, DbError
from tests.conftest import PACKAGES

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
STUDY = "j5-synthetic-study"


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    assert import_package(d, PACKAGES / "valid", home).outcome == "applied"  # dataset_version 2, 기록 3, 사진 2
    yield d
    d.close()


def _log(home: Path) -> list[dict]:
    return [json.loads(l) for l in (home / OPS_LOG).read_text(encoding="utf-8").splitlines()]


def test_actions_then_pass_and_last_good_written_only_on_pass(db, home):
    r = ops_check(home)
    assert r["ok"] is False and [a["code"] for a in r["actions"]] == ["backup_stale", "projection_stale"]
    assert r["schema"]["state"] == "same" and r["db"]["ok"] and r["db"]["dataset_version"] == 2 and r["photos"]["ok_all"] and r["raw"]["ok_all"]
    assert r["last_activity"]["import_applied"]["at"] and r["last_activity"]["collection_loaded"]["reason"] == "기록 없음"
    assert r["overdue"]["overdue"] is None and r["last_good"] is None and r["last_good_updated"] is False
    assert not (home / OPS_DIR / LAST_GOOD).exists(), "통과하지 못하면 마지막 정상 상태를 쓰지 않는다"
    assert _log(home)[-1]["ok"] is False and _log(home)[-1]["actions"] == ["backup_stale", "projection_stale"]
    t = ops_text(r)
    assert "조치 필요 2건" in t and "1. [backup_stale]" in t and "마지막 정상 상태: 기록 없음" in t
    # 백업·파생본을 만들면 통과. 마지막 정상 상태에 앱·도구·스키마·SQLite·Python 버전과 백업·파생본 위치가 남는다
    b = create_backup(db, home)
    assert b.outcome == "completed" and build_projection(db, home).outcome == "published"
    r2 = ops_check(home, now="2026-09-26T00:00:00Z")
    assert r2["ok"] and r2["actions"] == [] and r2["last_good_updated"] is True and r2["backup"]["verified"] is True
    lg, problem = read_last_good(home)
    assert problem is None and lg["format"] == "j5lastgood" and lg["checked_at"] == "2026-09-26T00:00:00Z"
    assert r2["last_good"] == lg and r2["previous_last_good_at"] is None, "결과는 이번에 쓴 파일을 돌려준다"
    assert lg["db_schema_version"] == S.DB_SCHEMA_VERSION == lg["db_schema_version_of_store"] and lg["dataset_version"] == 2 and lg["study_id"] == STUDY
    assert lg["backup"]["dir"] == b.backup_dir and lg["backup"]["dataset_version"] == 2 and lg["projection"]["dataset_version"] == 2
    for k in ("app_version", "package_schema_versions", "projection_schema_version", "backup_schema_version", "sqlite_version", "python_version", "platform"):
        assert lg[k]
    assert "점검 통과" in ops_text(r2) and "마지막 정상 상태: 2026-09-26T00:00:00Z" in ops_text(r2) and "(이번 점검으로 갱신, 이전 없음)" in ops_text(r2) and "내용 검증 통과" in ops_text(r2)
    r2b = ops_check(home, now="2026-09-26T01:00:00Z")
    assert r2b["last_good"]["checked_at"] == "2026-09-26T01:00:00Z" and r2b["previous_last_good_at"] == "2026-09-26T00:00:00Z" and "이전 2026-09-26T00:00:00Z" in ops_text(r2b)
    # 정본이 바뀌면(반영) 백업 필요 → 실패하지만 이전 정상 상태는 그대로 보여 준다
    with db.transaction():
        db.bump_dataset_version()
    r3 = ops_check(home, now="2026-09-27T00:00:00Z")
    assert not r3["ok"] and r3["actions"][0]["code"] == "backup_stale" and "백업 필요 (정본 v3, 마지막 백업 v2)" in r3["actions"][0]["text"]
    assert r3["last_good"]["checked_at"] == "2026-09-26T01:00:00Z" and r3["last_good_updated"] is False
    assert read_last_good(home)[0]["checked_at"] == "2026-09-26T01:00:00Z"
    assert "마지막 정상 상태: 2026-09-26T01:00:00Z" in ops_text(r3) and "갱신하지 않았다" in ops_text(r3)


def test_backup_contents_verified_not_just_manifest(db, home):
    """포인터·manifest 가 그대로여도 백업 안의 사진·DB 사본이 지워지거나 바뀌면 정상이 아니다(backup-verify·restore 가 실패할 백업을 '최신' 이라 하지 않는다)."""
    b = create_backup(db, home)
    build_projection(db, home)
    assert ops_check(home, now="2026-09-26T00:00:00Z")["ok"]
    bdir = home / b.backup_dir
    photo = next(p for p in (bdir / "photos").rglob("*") if p.is_file())
    data = photo.read_bytes()
    photo.unlink()
    r = ops_check(home, now="2026-09-27T00:00:00Z")
    assert not r["ok"] and [a["code"] for a in r["actions"]] == ["backup_verify_failed"] and r["backup"]["verify_problem"] == "file_missing"
    assert r["backup"]["verified"] is False and r["backup"]["stale"] and r["backup"]["state"].startswith("손상")
    assert read_last_good(home)[0]["checked_at"] == "2026-09-26T00:00:00Z" and r["last_good_updated"] is False
    assert "[backup_verify_failed]" in ops_text(r) and "새 백업" in ops_text(r)
    photo.write_bytes(data + b"x")  # 같은 이름, 다른 내용
    assert ops_check(home)["backup"]["verify_problem"] == "file_hash"
    photo.write_bytes(data)
    assert ops_check(home, now="2026-09-28T00:00:00Z")["ok"]
    # DB 사본 변조
    dbcopy = bdir / "db" / "j5.sqlite3"
    raw = dbcopy.read_bytes()
    dbcopy.write_bytes(raw[:-1])
    assert ops_check(home)["actions"][0]["code"] == "backup_verify_failed"
    dbcopy.write_bytes(raw)
    assert ops_check(home)["ok"]


def test_last_good_write_failure_is_an_action_not_a_traceback(db, home):
    create_backup(db, home)
    build_projection(db, home)
    (home / OPS_DIR).write_text("not a directory", encoding="utf-8")  # ops/ 자리를 파일이 차지: mkdir 이 OSError
    r = ops_check(home)
    assert not r["ok"] and [a["code"] for a in r["actions"]] == ["last_good_write_failed"] and r["last_good_updated"] is False and r["last_good"] is None
    assert _log(home)[-1]["ok"] is False and _log(home)[-1]["actions"] == ["last_good_write_failed"]
    assert "[last_good_write_failed]" in ops_text(r)
    (home / OPS_DIR).unlink()
    assert ops_check(home)["ok"]


def test_readonly_open_does_not_migrate_old_store_and_rejects_newer(db, home):
    """휴면 뒤 도구가 더 새로운 경우: 점검은 대기 마이그레이션만 보고하고 정본을 바꾸지 않는다. 쓰기 명령으로 열 때 적용된다."""
    create_backup(db, home)
    build_projection(db, home)
    db.close()
    path = home / "db" / "j5.sqlite3"
    import sqlite3
    conn = sqlite3.connect(str(path))
    # 마지막 마이그레이션이 없던 구버전처럼: 그 마이그레이션이 만든 표를 지우고 적용 기록을 뺀다 (표 재작성 부분은 다시 실행해도 같은 결과)
    for t in ("readiness_rechecks",):  # 마지막 마이그레이션(14)이 만든 표
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("DELETE FROM schema_migrations WHERE version = ?", (S.DB_SCHEMA_VERSION,))
    conn.commit()
    conn.close()
    r = ops_check(home)
    assert r["schema"]["state"] == "db_older" and r["schema"]["pending"] == [{"version": S.DB_SCHEMA_VERSION, "name": S.MIGRATIONS[-1][1]}]
    codes = [a["code"] for a in r["actions"]]
    assert "migrations_pending" in codes and not r["ok"] and "백업" in next(a["text"] for a in r["actions"] if a["code"] == "migrations_pending")
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == S.DB_SCHEMA_VERSION - 1, "점검은 마이그레이션하지 않는다"
    conn.close()
    with Db.open_readonly(path) as ro:
        assert ro.schema_version() == S.DB_SCHEMA_VERSION - 1
        with pytest.raises(sqlite3.OperationalError):
            with ro.transaction():
                ro.bump_dataset_version()
    # 쓰기 명령으로 열면 적용된다
    with Db.open(path) as d:
        assert d.schema_version() == S.DB_SCHEMA_VERSION
    assert ops_check(home)["schema"]["state"] == "same"
    # 도구보다 새로운 정본은 열지 않는다
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)", (S.DB_SCHEMA_VERSION + 1, "future", "2030-01-01T00:00:00Z"))
    conn.commit()
    conn.close()
    r = ops_check(home)
    assert r["db"]["problem"] == "tool_too_old" and r["actions"][0]["code"] == "tool_too_old" and not r["ok"]
    with pytest.raises(DbError) as e:
        Db.open_readonly(path)
    assert e.value.code == "tool_too_old"
    assert "열 수 없음 [tool_too_old]" in ops_text(r)


def test_missing_photo_and_missing_store(db, home):
    create_backup(db, home)
    build_projection(db, home)
    assert ops_check(home)["ok"]
    photo = next(p for p in (home / "photos").rglob("*") if p.is_file())
    photo.unlink()
    r = ops_check(home)
    assert not r["ok"] and [a["code"] for a in r["actions"]] == ["photos"] and r["photos"]["missing"]
    assert read_last_good(home)[0] is not None, "이전 정상 상태는 남는다"
    r = ops_check(home / "elsewhere")
    assert r["db"]["problem"] == "db_missing" and "restore" in r["actions"][0]["text"] and not r["ok"]
    assert (home / "elsewhere" / OPS_LOG).is_file()


def test_new_pc_restore_then_resume_backups(db, home, tmp_path):
    """새 PC 이전: 백업 폴더만 가져와 빈 폴더에 복구 → 점검이 '백업 없음' 을 알린다(복구본에는 백업·파생본이 없다) → 백업·파생본 재개 → 통과."""
    b = create_backup(db, home)
    build_projection(db, home)
    ops_check(home, now="2026-01-01T00:00:00Z")
    new_home = tmp_path / "new_pc"
    assert restore_backup(home / b.backup_dir, new_home).outcome == "completed"
    r = ops_check(new_home)
    assert [a["code"] for a in r["actions"]] == ["backup_stale", "projection_stale"] and r["last_good"] is None
    assert r["db"]["dataset_version"] == 2 and r["photos"]["ok_all"] and r["overdue"]["text"].startswith("기준 없음")
    with Db.open(new_home / "db" / "j5.sqlite3") as nd:
        assert create_backup(nd, new_home).outcome == "completed" and build_projection(nd, new_home).outcome == "published"
    r2 = ops_check(new_home)
    assert r2["ok"] and read_last_good(new_home)[0]["dataset_version"] == 2
    assert read_last_good(home)[0]["checked_at"] == "2026-01-01T00:00:00Z", "옛 PC 의 파일은 그대로"


def test_empty_store_without_assets_can_pass(home):
    """물건이 없는 정본: `project` 는 빈 파생본을 거절하므로 파생본 없음은 조치 항목이 아니라 표시다 (첫 실사용 2026-09-26). 물건을 넣으면 다시 조치 항목이 된다."""
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    try:
        r = ops_check(home)
        assert [a["code"] for a in r["actions"]] == ["backup_stale"], "파생본 없음은 조치 항목이 아니다"
        assert r["projection"]["not_applicable"] is True and "대상 없음" in r["projection"]["state"]
        assert any("파생본 대상 없음" in w for w in r["warnings"])
        assert create_backup(d, home).outcome == "completed"
        pr = build_projection(d, home)
        assert pr.outcome == "failed" and any(f["code"] == "no_assets" for f in pr.findings), "빈 파생본은 만들지 않는다"
        r = ops_check(home)
        assert r["ok"] is True and r["last_good_updated"] is True and r["last_good"]["projection"] == {"dir": None, "published_at": None, "dataset_version": None}
        assert "대상 없음" in ops_text(r) and "점검 통과" in ops_text(r)
        # 물건이 없어도 포인터가 남아 있으면(다른 정본의 파생본·손상) 조치 항목이다 (리뷰 반영)
        pdir = home / PROJECTIONS_DIR
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / LATEST_POINTER).write_text(json.dumps({"dir": "exports/private/projections/ds9-deadbeef", "zip": "x.j5view.zip", "zip_sha256": "0" * 64, "source_dataset_version": 9}), encoding="utf-8")
        r = ops_check(home)
        assert [a["code"] for a in r["actions"]] == ["projection_stale"] and r["projection"].get("not_applicable") is None and "zip_missing" in r["projection"]["state"]
        (pdir / LATEST_POINTER).unlink()
        # 물건을 넣으면 파생본이 필요해진다
        d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
        r = ops_check(home)
        assert [a["code"] for a in r["actions"]] == ["backup_stale", "projection_stale"] and r["projection"].get("not_applicable") is None
    finally:
        d.close()


def test_overdue_by_last_good_or_backup(db, home):
    create_backup(db, home)
    build_projection(db, home)
    r = ops_check(home, now="2026-09-26T00:00:00Z")
    assert r["ok"] and r["overdue"]["basis"] == "마지막 백업" and r["overdue"]["overdue"] is False
    later = "2027-04-01T00:00:00Z"
    r2 = ops_check(home, now=later)
    assert r2["overdue"]["basis"] == "마지막 통과 점검" and r2["overdue"]["days"] > CHECK_INTERVAL_DAYS and r2["overdue"]["overdue"] is True
    assert "반기 점검 기한 초과" in ops_text(r2)
    assert r2["ok"] and read_last_good(home)[0]["checked_at"] == later == r2["last_good"]["checked_at"], "기한 초과는 조치가 아니라 표시이며 통과하면 갱신된다"
    # 손상된 마지막 정상 파일은 주의로 표시하고 통과 시 다시 쓴다
    (home / OPS_DIR / LAST_GOOD).write_text("{", encoding="utf-8")
    r3 = ops_check(home, now="2027-04-02T00:00:00Z")
    assert r3["last_good_problem"].startswith("unreadable") and any("읽을 수 없다" in w for w in r3["warnings"])
    assert r3["ok"] and read_last_good(home)[0]["checked_at"] == "2027-04-02T00:00:00Z"


def test_cli_ops_check(home, capsys, monkeypatch):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "init", "--study-id", STUDY, "--data-mode", "synthetic"]) == 0
    assert cli.main(["db", "load-seed", str(SEED_PATH)]) == 0
    assert cli.main(["db", "import", str(PACKAGES / "valid")]) == 0
    capsys.readouterr()
    assert cli.main(["db", "ops-check"]) == 1
    out = capsys.readouterr().out
    assert "조치 필요 2건" in out and "[backup_stale]" in out
    assert cli.main(["db", "backup"]) == 0 and cli.main(["db", "project"]) == 0
    capsys.readouterr()
    assert cli.main(["db", "ops-check"]) == 0
    assert "점검 통과" in capsys.readouterr().out
    assert cli.main(["db", "--db", str(home / "db" / "j5.sqlite3"), "ops-check", "--data-home", str(home), "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["ok"] is True and j["tool"]["db_schema_version"] == S.DB_SCHEMA_VERSION and j["last_good"]["checked_at"]
    monkeypatch.delenv("J5_DATA_HOME")
    assert cli.main(["db", "ops-check"]) == 3
