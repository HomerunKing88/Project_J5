"""J5-012: 백업·복구. 완료 조건 "빈 폴더 원 복원·사진 대사" (릴리스 계획 §10), R1b 인수(§5) "백업 실패를 시험한다. 빈 폴더 복구 후 기록 수·해시·사진 연결을 확인한다".
데이터 사전 §12 (SQLite 백업 API 의 일관된 사본, 무결성·외래키 검사, 참조 파일·해시 목록, 다른 폴더에서 복구해 기록 수와 사진 해시 확인), ADR E07.

시험: 일관된 사본·manifest·포인터·로그·검증, 상태 표시(없음/최신/백업 필요/손상/접근 불가/미확인), 참조 사진 누락·손상 시 백업 실패(이전 백업 유지),
DB 사본 실패, 쓰기 트랜잭션 안 백업 거절·스냅샷 중 다른 쓰기 대기, 외부 위치 백업, 빈 폴더 복구와 대조, 비어 있지 않은 폴더·변조된 백업 거절,
사진 대사, CLI. 가상자료만 쓴다. "실제 두 차례 실사용" 은 실기기 시험이라 여기 없다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from j5 import cli
from j5.db import backup as B
from j5.db.backup import BackupError, backup_status, check_photos, create_backup, read_latest, restore_backup, verify_backup_dir
from j5.db.importer import import_package
from j5.db.projection import build_projection
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
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))  # dataset_version 1
    assert import_package(d, PACKAGES / "valid", home).outcome == "applied"  # dataset_version 2, 기록 3, 사진 2
    yield d
    d.close()


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def test_backup_makes_consistent_copy_manifest_pointer_and_log(db, home):
    before = tree(home / "db") | tree(home / "photos")
    r = create_backup(db, home)
    assert r.outcome == "completed", r.to_text()
    assert r.dataset_version == 2 and r.db_schema_version == 5 and r.photos == 2 and r.files == 3
    bdir = home / r.backup_dir
    assert bdir.parent == home / "backups" and bdir.name.endswith(f"-ds2-{r.run_id[:8]}")
    manifest = json.loads((bdir / "backup_manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "j5backup" and manifest["dataset_version"] == 2 and manifest["study_id"] == STUDY and manifest["data_mode"] == "synthetic"
    assert manifest["counts"]["records"] == 3 and manifest["counts"]["attachments"] == 3 and manifest["counts"]["assets"] == 5
    paths = [f["path"] for f in manifest["files"]]
    assert paths[0] == "db/j5.sqlite3" and all(p.startswith("photos/") for p in paths[1:]) and len(paths) == 3
    for f in manifest["files"]:
        assert sha(bdir / f["path"]) == f["sha256"] and (bdir / f["path"]).stat().st_size == f["bytes"]
    # 사본은 정본과 같은 내용을 담고, 원본 정본·사진은 바뀌지 않았다
    with Db.open(bdir / "db" / "j5.sqlite3") as copy:
        st = copy.status()
        assert st["ok"] and st["dataset_version"] == 2 and st["counts"]["records"] == 3 and st["study_id"] == STUDY
    assert tree(home / "db") | tree(home / "photos") == before
    assert db.status()["dataset_version"] == 2 and db.status()["counts"]["records"] == 3
    ptr = read_latest(home)
    assert ptr["dataset_version"] == 2 and ptr["dir"] == r.backup_dir and ptr["manifest_sha256"] == r.manifest_sha256 == sha(bdir / "backup_manifest.json")
    log = [json.loads(l) for l in (home / "logs" / "backup.log").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["outcome"] == "completed" and log[-1]["backup_dir"] == r.backup_dir
    v = verify_backup_dir(bdir)
    assert v["photos"] == 2 and v["referenced"] == 2 and v["counts"]["records"] == 3
    assert backup_status(db, home)["state"] == "최신 (정본 v2)" and db.status(home)["backup"]["stale"] is False
    assert not list((home / "backups").glob(".tmp-*")) and not list((home / "backups").glob("failed-*"))


def test_status_states_none_latest_needed_damaged_unreachable_unchecked(db, home):
    st = backup_status(db, home)
    assert st["state"] == "없음 (백업 필요)" and st["stale"] and st["checked"]
    assert backup_status(db, None)["state"].startswith("파일 미확인") and backup_status(db, None)["checked"] is False, "백업 기록은 정본에 없으므로 data_home 없이는 미확인"
    r = create_backup(db, home)
    assert backup_status(db, home)["state"] == "최신 (정본 v2)"
    # 정본이 바뀌면 백업 필요
    with db.transaction():
        db.bump_dataset_version()
    st = backup_status(db, home)
    assert st["state"] == "백업 필요 (정본 v3, 마지막 백업 v2)" and st["stale"] and st["backup_version"] == 2
    # 파생본 생성은 백업 상태를 바꾸지 않는다 (정본 데이터가 아니다)
    assert build_projection(db, home).outcome == "published" and backup_status(db, home)["backup_version"] == 2
    # manifest 변조 → 손상, 폴더 이동(외장 드라이브 분리) → 접근 불가
    mpath = home / r.backup_dir / "backup_manifest.json"
    original = mpath.read_bytes()
    mpath.write_bytes(original + b"\n")
    assert backup_status(db, home)["problem"] == "manifest_mismatch" and backup_status(db, home)["state"].startswith("손상")
    mpath.write_bytes(original)
    (home / r.backup_dir).rename(home / "backups" / "moved-away")
    st = backup_status(db, home)
    assert st["problem"] == "backup_dir_unreachable" and st["state"].startswith("접근 불가")
    (home / "backups" / "moved-away").rename(home / r.backup_dir)
    assert backup_status(db, home)["problem"] is None


def test_missing_or_corrupt_photo_fails_backup_and_keeps_previous(db, home):
    """R1b 인수 '백업 실패': 참조 사진이 없거나 다르면 완료로 표시하지 않고, 이전 백업·포인터·정본은 그대로다."""
    r1 = create_backup(db, home)
    ptr1, tree1 = read_latest(home), tree(home / r1.backup_dir)
    victim = next((home / "photos").rglob("*.png"))
    original = victim.read_bytes()
    victim.write_bytes(b"corrupted")
    r = create_backup(db, home)
    assert r.outcome == "failed" and any(f["code"] == "photos_incomplete" and "hash_mismatch" in f["message"] for f in r.findings)
    assert "이전 백업 v2" in r.message and "정본은 그대로" in r.message
    victim.unlink()
    r = create_backup(db, home)
    assert r.outcome == "failed" and any(f["code"] == "photos_incomplete" and "file_missing" in f["message"] for f in r.findings)
    assert read_latest(home) == ptr1 and tree(home / r1.backup_dir) == tree1, "이전 백업·포인터 유지"
    failed = list((home / "backups").glob("failed-*"))
    assert len(failed) == 2 and all((d / "db" / "j5.sqlite3").is_file() for d in failed), "부분 산출물은 failed- 폴더에 격리 (백업 아님, 자동 삭제 없음)"
    assert not list((home / "backups").glob(".tmp-*"))
    c = check_photos(db, home)
    assert c["missing"] == [victim.relative_to(home).as_posix()] and not c["ok_all"]
    log = [json.loads(l) for l in (home / "logs" / "backup.log").read_text(encoding="utf-8").splitlines()]
    assert [e["outcome"] for e in log] == ["completed", "failed", "failed"]
    # 이전 백업에서 사진을 되찾으면 다시 완료된다
    victim.write_bytes(original)
    assert create_backup(db, home).outcome == "completed" and db.status()["dataset_version"] == 2


class _Conn:
    """sqlite3.Connection 대리자. backup() 을 실패시켜 사본 작성 실패를 흉내 낸다."""

    def __init__(self, real):
        self._real = real

    def backup(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error (시험)")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_db_copy_failure_and_write_transaction_are_rejected(db, home):
    r1 = create_backup(db, home)
    real = db.conn
    db.conn = _Conn(real)
    r = create_backup(db, home)
    db.conn = real
    assert r.outcome == "failed" and any(f["code"] == "OperationalError" for f in r.findings) and read_latest(home)["run_id"] == r1.run_id
    assert db._snapshot is False and db.status()["ok"], "스냅샷은 닫혔고 정본은 정상"
    # 쓰기 트랜잭션 안에서는 백업하지 않는다 (같은 연결의 쓰기 잠금 안에서 백업 API 가 진행되지 않으므로 거절이 맞다)
    with db.transaction():
        r = create_backup(db, home)
        assert r.outcome == "failed" and any(f["code"] == "in_transaction" for f in r.findings)
    assert read_latest(home)["run_id"] == r1.run_id


def test_snapshot_is_read_only_and_makes_other_writers_wait(db, home):
    other = sqlite3.connect(str(db.path), isolation_level=None, timeout=0.2)
    with db.snapshot():
        with pytest.raises(DbError) as e:
            with db.transaction():
                pass
        assert e.value.code == "snapshot_read_only"
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO meta (key, value) VALUES ('x', '1')")
        with pytest.raises(sqlite3.OperationalError):
            other.execute("COMMIT")  # 스냅샷이 열려 있는 동안 다른 연결의 쓰기는 커밋되지 않는다
        other.execute("ROLLBACK")
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO meta (key, value) VALUES ('x', '1')")
    other.execute("COMMIT")
    other.execute("BEGIN IMMEDIATE")
    other.execute("DELETE FROM meta WHERE key = 'x'")
    other.execute("COMMIT")
    other.close()
    assert db.meta("x") is None


def test_backup_to_external_dest(db, home, tmp_path):
    ext = tmp_path / "usb"
    r = create_backup(db, home, dest_root=ext)
    assert r.outcome == "completed" and Path(r.backup_dir).is_absolute() and Path(r.backup_dir).parent == ext.resolve()
    assert read_latest(home)["dir"] == r.backup_dir and backup_status(db, home)["state"] == "최신 (정본 v2)"
    assert not (home / "backups" / Path(r.backup_dir).name).exists()
    ext.rename(tmp_path / "unplugged")
    assert backup_status(db, home)["problem"] == "backup_dir_unreachable"
    (tmp_path / "unplugged").rename(ext)
    assert verify_backup_dir(Path(r.backup_dir))["counts"]["records"] == 3


def test_restore_into_empty_folder_matches_counts_hashes_and_photo_links(db, home, tmp_path):
    """R1b 인수: 빈 폴더 복구 후 기록 수·해시·사진 연결 확인. 복구본은 반영·파생본 생성에 그대로 쓸 수 있다."""
    r = create_backup(db, home)
    bdir = home / r.backup_dir
    dest = tmp_path / "new-home"
    rr = restore_backup(bdir, dest)
    assert rr.outcome == "completed", rr.to_text()
    assert rr.dataset_version == 2 and rr.counts["records"] == 3 and rr.counts["attachments"] == 3 and rr.photos == 2
    assert tree(dest / "db") == tree(bdir / "db") and tree(dest / "photos") == tree(home / "photos"), "복구한 정본 파일은 백업 사본과, 사진은 원본과 바이트가 같다"
    assert not list(dest.glob(".restoring-*")) and sorted(p.name for p in dest.iterdir()) == ["db", "logs", "photos"]
    log = [json.loads(l) for l in (dest / "logs" / "restore.log").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["outcome"] == "completed" and log[-1]["dataset_version"] == 2
    with Db.open(dest / "db" / "j5.sqlite3") as rdb:
        st = rdb.status(dest)
        assert st["ok"] and st["dataset_version"] == 2 and st["counts"] == db.status()["counts"]
        assert st["backup"]["state"] == "없음 (백업 필요)" and st["projection"]["state"].startswith("없음"), "복구본에는 아직 백업·파생본이 없다"
        assert check_photos(rdb, dest)["ok_all"]
        assert [x["record_id"] for x in rdb.list_records("7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50")] == [x["record_id"] for x in db.list_records("7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50")]
        assert import_package(rdb, PACKAGES / "valid", dest).outcome == "duplicate", "이벤트 대장도 복구되어 같은 파일은 중복으로 판정"
        assert build_projection(rdb, dest, photos=True).outcome == "published"
        assert create_backup(rdb, dest).outcome == "completed"
    # 빈 폴더가 아니면 거절, 없는 폴더는 만든다
    current = tree(dest / "db") | tree(dest / "photos")
    rr2 = restore_backup(bdir, dest)
    assert rr2.outcome == "failed" and any(f["code"] == "dest_not_empty" for f in rr2.findings)
    assert tree(dest / "db") | tree(dest / "photos") == current, "기존 정본·사진은 손대지 않는다"
    assert restore_backup(bdir, tmp_path / "deeper" / "home2").outcome == "completed"


def test_restore_refuses_tampered_or_incomplete_backup_and_leaves_dest_empty(db, home, tmp_path):
    r = create_backup(db, home)
    bdir = home / r.backup_dir
    dest = tmp_path / "dest"
    dest.mkdir()
    photo = next((bdir / "photos").rglob("*.png"))
    original = photo.read_bytes()
    photo.write_bytes(b"tampered")
    rr = restore_backup(bdir, dest)
    assert rr.outcome == "failed" and any(f["code"] == "file_hash" for f in rr.findings)
    photo.write_bytes(original)
    photo.rename(photo.with_name("gone.bin"))
    rr = restore_backup(bdir, dest)
    assert rr.outcome == "failed" and any(f["code"] in ("file_missing", "extra_or_missing") for f in rr.findings)
    photo.with_name("gone.bin").rename(photo)
    dbfile = bdir / "db" / "j5.sqlite3"
    data = bytearray(dbfile.read_bytes())
    data[-1] ^= 0xFF
    dbfile.write_bytes(bytes(data))
    rr = restore_backup(bdir, dest)
    assert rr.outcome == "failed" and any(f["code"] == "file_hash" for f in rr.findings)
    assert sorted(p.name for p in dest.iterdir()) == ["logs"], "실패한 복구는 대상에 정본·사진을 남기지 않는다 (로그만, 재시도 가능)"
    with pytest.raises(BackupError):
        verify_backup_dir(bdir)
    with pytest.raises(BackupError) as e:
        verify_backup_dir(tmp_path / "nowhere")
    assert e.value.code == "manifest_missing"
    dbfile.write_bytes(bytes(data[:-1]) + bytes([data[-1] ^ 0xFF]))
    assert verify_backup_dir(bdir)["counts"]["records"] == 3
    assert restore_backup(bdir, dest).outcome == "completed", "백업을 고치면 같은 폴더(로그만 있음)에 재시도할 수 있다"


def test_verify_detects_broken_photo_link_even_when_hashes_match(db, home):
    """manifest 의 파일 해시가 맞아도 정본 사본이 참조한 사진이 목록에 없으면 실패한다 (사진 연결)."""
    r = create_backup(db, home)
    bdir = home / r.backup_dir
    m = json.loads((bdir / "backup_manifest.json").read_text(encoding="utf-8"))
    dropped = next(f for f in m["files"] if f["path"].startswith("photos/"))
    (bdir / dropped["path"]).unlink()
    m["files"] = [f for f in m["files"] if f is not dropped]
    (bdir / "backup_manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(BackupError) as e:
        verify_backup_dir(bdir)
    assert e.value.code == "photo_link"


def test_check_photos_reports_missing_mismatch_and_unreferenced(db, home):
    c = check_photos(db, home)
    assert c == {"referenced": 2, "ok": 2, "missing": [], "mismatched": [], "unreferenced": [], "ok_all": True}
    photos = sorted((home / "photos").rglob("*.png"))
    photos[0].write_bytes(b"x")
    photos[1].unlink()
    stray = home / "photos" / "zz" / "stray.png"
    stray.parent.mkdir()
    stray.write_bytes(b"y")
    c = check_photos(db, home)
    assert c["ok"] == 0 and c["mismatched"] == [photos[0].relative_to(home).as_posix()] and c["missing"] == [photos[1].relative_to(home).as_posix()]
    assert c["unreferenced"] == ["photos/zz/stray.png"] and not c["ok_all"]
    assert stray.is_file() and photos[0].is_file(), "대사는 아무것도 지우지 않는다"
    text = B.check_photos_text(c)
    assert "누락 1장" in text and "해시 불일치 1장" in text and "미참조 파일 1개" in text and "자동 삭제 없음" in text


def test_cli_backup_verify_restore_status_and_check(home, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "init", "--study-id", STUDY, "--data-mode", "synthetic"]) == 0
    assert cli.main(["db", "load-seed", str(SEED_PATH)]) == 0
    assert cli.main(["db", "import", str(PACKAGES / "valid")]) == 0
    capsys.readouterr()
    assert cli.main(["db", "status"]) == 0
    assert "백업: 없음 (백업 필요)" in capsys.readouterr().out
    assert cli.main(["db", "backup"]) == 0
    out = capsys.readouterr().out
    assert "백업: 완료" in out and "정본 v2" in out and "외부 사본 보관을 뜻하지 않는다" in out
    assert cli.main(["db", "status"]) == 0
    assert "백업: 최신 (정본 v2)" in capsys.readouterr().out
    bdir = home / read_latest(home)["dir"]
    assert cli.main(["db", "backup-verify", str(bdir)]) == 0
    assert "백업 검증 통과" in capsys.readouterr().out
    assert cli.main(["db", "check-photos", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok_all"] is True
    dest = tmp_path / "restored"
    assert cli.main(["db", "restore", str(bdir), str(dest)]) == 0
    out = capsys.readouterr().out
    assert "복구: 완료" in out and "사진 2장 연결 확인" in out
    assert cli.main(["db", "restore", str(bdir), str(dest)]) == 1
    assert "dest_not_empty" in capsys.readouterr().out
    assert cli.main(["db", "--db", str(dest / "db" / "j5.sqlite3"), "status", "--data-home", str(dest)]) == 0
    assert "백업: 없음" in capsys.readouterr().out
    # 사진 손상 → check-photos 1, backup 1, 정본 그대로
    victim = next((home / "photos").rglob("*.png"))
    victim.write_bytes(b"bad")
    assert cli.main(["db", "check-photos"]) == 1
    assert "해시 불일치 1장" in capsys.readouterr().out
    assert cli.main(["db", "backup", "--json"]) == 1
    j = json.loads(capsys.readouterr().out)
    assert j["outcome"] == "failed" and j["findings"][0]["code"] == "photos_incomplete"
    (bdir / "backup_manifest.json").write_bytes(b"{")
    assert cli.main(["db", "backup-verify", str(bdir)]) == 1
    assert "manifest_unreadable" in capsys.readouterr().err
    assert cli.main(["db", "backup-verify", str(tmp_path / "none")]) == cli.USAGE_ERROR
    monkeypatch.delenv("J5_DATA_HOME")
    assert cli.main(["db", "--db", str(home / "db" / "j5.sqlite3"), "backup"]) == cli.USAGE_ERROR


def test_restore_of_older_backup_survives_newer_tool_migrations(db, home, tmp_path, monkeypatch):
    """도구가 백업보다 새로우면(마이그레이션 추가) 복구 시 마이그레이션이 적용되고, 대조는 백업에 있던 데이터 테이블로 한다 (Codex P1)."""
    from j5.db import schema as S
    r = create_backup(db, home)
    bdir = home / r.backup_dir
    monkeypatch.setattr(S, "MIGRATIONS", S.MIGRATIONS + ((6, "test_future", "CREATE TABLE t_future (x INTEGER) STRICT;"),))
    monkeypatch.setattr(S, "DB_SCHEMA_VERSION", 6)
    assert verify_backup_dir(bdir)["manifest"]["db_schema_version"] == 5, "백업 파일 자체는 그대로 검증된다"
    dest = tmp_path / "newer-tool"
    rr = restore_backup(bdir, dest)
    assert rr.outcome == "completed", rr.to_text()
    assert rr.db_schema_version == 6 and rr.dataset_version == 2 and rr.counts["records"] == 3 and rr.counts["schema_migrations"] == 6 and rr.photos == 2
    with Db.open(dest / "db" / "j5.sqlite3") as rdb:
        assert rdb.schema_version() == 6 and rdb.status()["counts"]["records"] == 3 and check_photos(rdb, dest)["ok_all"]
    # 백업 원본은 손대지 않았고(마이그레이션은 복구본에만), 도구가 백업보다 오래되면 복구를 거절한다
    assert verify_backup_dir(bdir)["manifest"]["db_schema_version"] == 5
    monkeypatch.setattr(S, "MIGRATIONS", S.MIGRATIONS[:2])
    monkeypatch.setattr(S, "DB_SCHEMA_VERSION", 2)
    rr = restore_backup(bdir, tmp_path / "older-tool")
    assert rr.outcome == "failed" and any(f["code"] == "tool_too_old" for f in rr.findings)


def test_unreadable_pointer_does_not_mask_backup_failure(db, home, capsys, monkeypatch):
    """포인터가 손상된 상태에서 백업이 실패해도 결과 객체·종료 코드로 끝난다 (Codex P2)."""
    create_backup(db, home)
    (home / "backups" / "latest.json").write_bytes(b"{not json")
    assert backup_status(db, home)["problem"].startswith("pointer_unreadable")
    next((home / "photos").rglob("*.png")).unlink()
    r = create_backup(db, home)
    assert r.outcome == "failed" and r.findings[0]["code"] == "photos_incomplete" and "pointer_unreadable" in r.message
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "backup"]) == 1
    out = capsys.readouterr()
    assert "백업: 실패" in out.out and "Traceback" not in out.err


def test_status_rejects_backup_of_another_canonical_db(db, home, tmp_path):
    """같은 data_home 의 포인터가 다른 정본(study_id·data_mode)의 백업이면 버전이 같아도 '최신' 이 아니다 (Codex P2)."""
    create_backup(db, home)
    with Db.create(tmp_path / "other" / "j5.sqlite3", study_id="other-study", data_mode="synthetic") as other:
        with other.transaction():
            other.set_meta("dataset_version", "2")
        st = backup_status(other, home)
        assert st["problem"] == "identity_mismatch" and st["stale"] and st["state"].startswith("다른 정본의 백업")
        assert other.status(home)["backup"]["stale"]
    assert backup_status(db, home)["state"] == "최신 (정본 v2)"
