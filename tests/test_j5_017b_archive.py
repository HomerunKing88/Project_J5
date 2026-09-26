"""J5-017B: 연말 개방형 포맷 보존본. 릴리스 계획 §8 R6 ("연말 개방형 포맷 보존본"), 데이터 사전 §12 ("연말 CSV/JSONL/GeoJSON 보존본을 유지한다").

시험: 모든 표의 jsonl(값 보존: NULL·BLOB)·csv(편의 사본)·GeoJSON·schema.sql·스키마 사본·README·manifest 와 재검증, 사진 포함/미포함, 필지 GeoJSON,
변조·누락 검출, 참조 사진 누락 시 실패(부분 산출물 격리, 정본·백업 그대로), 외장 위치, 정본 버전 변화에 따른 새 보존본과 옛 보존본 불변, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.archive import ARCHIVE_LOG, ArchiveError, create_archive, verify_archive_dir
from j5.db.backup import create_backup, read_latest
from j5.db.importer import import_package
from j5.db.parcels import load_bundle
from j5.db.store import Db
from tests.conftest import PACKAGES

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
BUNDLE = Path(__file__).resolve().parent / "fixtures" / "parcels" / "synthetic.j5parcels.json"
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


def _rows(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_bytes().split(b"\n") if l]


def _csv(p: Path) -> list[list[str]]:
    with open(p, encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def _log(home: Path) -> list[dict]:
    return [json.loads(l) for l in (home / ARCHIVE_LOG).read_text(encoding="utf-8").splitlines()]


def test_archive_writes_open_formats_and_verifies(db, home):
    r = create_archive(db, home)
    assert r.outcome == "completed", r.to_text()
    adir = home / r.archive_dir
    assert adir.parent == home / "exports" / "private" / "archives" and adir.name.endswith(f"-ds2-{r.run_id[:8]}")
    m = json.loads((adir / "archive_manifest.json").read_text(encoding="utf-8"))
    assert m["format"] == "j5archive" and m["dataset_version"] == 2 and m["db_schema_version"] == S.DB_SCHEMA_VERSION and m["study_id"] == STUDY and m["photos_included"] is False
    assert m["tool"]["app_version"] and m["tool"]["sqlite_version"] and m["tool"]["python_version"]
    # 모든 사용자 표가 jsonl·csv 로 있고 행 수가 정본과 같다
    names = [row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    assert [t["name"] for t in m["tables"]] == names and m["counts"]["tables"] == len(names) == r.tables
    by = {t["name"]: t for t in m["tables"]}
    assert by["records"]["rows"] == 3 and by["attachments"]["rows"] == 3 and by["assets"]["rows"] == 5 and by["schema_migrations"]["rows"] == S.DB_SCHEMA_VERSION
    for t in m["tables"]:
        n = db.conn.execute(f'SELECT COUNT(*) FROM "{t["name"]}"').fetchone()[0]
        rows = _rows(adir / t["jsonl"])
        assert len(rows) == n == t["rows"] and (adir / t["csv"]).is_file()
        cols = [c["name"] for c in t["columns"]]
        assert all(list(x.keys()) == cols for x in rows)
        c = _csv(adir / t["csv"])
        assert c[0] == cols and len(c) - 1 == n
    # 값 보존: NULL 은 null(jsonl)·빈 칸(csv), BLOB 은 hex, JSON 열은 문자열 그대로
    recs = _rows(adir / "tables/records.jsonl")
    assert recs[0]["source_published_at"] is None and recs[0]["supersedes_id"] is None and isinstance(recs[0]["payload_json"], str)
    assert json.loads(recs[0]["payload_json"])["change_status"] == "change_observed"
    csv_recs = _csv(adir / "tables/records.csv")
    i = csv_recs[0].index("source_published_at")
    assert csv_recs[1][i] == ""
    ev = _rows(adir / "tables/import_events.jsonl")
    line = bytes.fromhex(ev[0]["line"]["$hex"])
    assert json.loads(line)["event_id"] == ev[0]["event_id"] and hashlib.sha256(line).hexdigest() == ev[0]["event_hash"]
    # GeoJSON·schema.sql·스키마 사본·README
    geo = json.loads((adir / "assets.geojson").read_text(encoding="utf-8"))
    assert len(geo["features"]) == 4 == m["counts"]["assets_located"] and len(geo["unlocated_asset_ids"]) == 1 and geo["source_dataset_version"] == 2
    sql = (adir / "schema.sql").read_text(encoding="utf-8")
    assert "-- table records\n" in sql and "CREATE TRIGGER" in sql and "CREATE INDEX" in sql  # 재작성된 표는 CREATE TABLE "records" 로 남는다
    schemas = sorted(p.name for p in (adir / "schemas").glob("*.schema.json"))
    assert "archive_manifest.schema.json" in schemas and "observation_event.schema.json" in schemas and "plan_records.schema.json" in schemas
    readme = (adir / "README.txt").read_text(encoding="utf-8")
    assert "j5archive" in readme and "NULL 은 빈 칸" in readme and "백업이 아니" in readme and "records(3)" in readme
    assert not (adir / "photos").exists() and m["counts"]["photos"] == 0 and "parcels.geojson" not in {f["path"] for f in m["files"]}
    # manifest 의 파일 목록이 디스크와 같고 재검증이 통과한다
    on_disk = {p.relative_to(adir).as_posix() for p in adir.rglob("*") if p.is_file()} - {"archive_manifest.json"}
    assert on_disk == {f["path"] for f in m["files"]} and r.files == len(m["files"])
    v = verify_archive_dir(adir)
    assert v["rows"] == m["counts"]["rows"] == r.rows and v["manifest_sha256"] == r.manifest_sha256
    assert _log(home)[-1]["outcome"] == "completed" and _log(home)[-1]["archive_dir"] == r.archive_dir
    assert "보존본은 백업이 아니다" in r.to_text()
    # 정본은 그대로
    assert db.status()["dataset_version"] == 2 and db.status()["counts"]["records"] == 3


def test_archive_with_photos_and_parcels(db, home):
    load_bundle(db, json.loads(BUNDLE.read_text(encoding="utf-8")))
    r = create_archive(db, home, photos=True)
    assert r.outcome == "completed" and r.photos == 2, r.to_text()
    adir = home / r.archive_dir
    m = json.loads((adir / "archive_manifest.json").read_text(encoding="utf-8"))
    assert m["photos_included"] is True and m["counts"]["photos"] == 2 and m["counts"]["parcels"] == 6
    atts = _rows(adir / "tables/attachments.jsonl")
    for a in atts:
        p = adir / a["rel_path"]
        assert p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == a["sha256"] and p.stat().st_size == a["bytes"]
    pb = json.loads((adir / "parcels.geojson").read_text(encoding="utf-8"))
    assert pb["count"] == 6 and pb["source_dataset_version"] == r.dataset_version and pb["generated_at"] == r.generated_at
    assert verify_archive_dir(adir)["photos"] == 2


def test_verify_detects_tampering(db, home):
    r = create_archive(db, home, photos=True)
    adir = home / r.archive_dir
    p = adir / "tables/records.jsonl"
    data = p.read_bytes()
    p.write_bytes(data + b"\n")
    with pytest.raises(ArchiveError) as e:
        verify_archive_dir(adir)
    assert e.value.code == "file_hash"
    p.write_bytes(data)
    verify_archive_dir(adir)
    extra = adir / "tables" / "note.txt"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(ArchiveError) as e:
        verify_archive_dir(adir)
    assert e.value.code == "extra_or_missing"
    extra.unlink()
    photo = next(q for q in (adir / "photos").rglob("*") if q.is_file())
    photo_data = photo.read_bytes()
    photo.unlink()
    with pytest.raises(ArchiveError) as e:
        verify_archive_dir(adir)
    assert e.value.code == "file_missing"
    photo.write_bytes(photo_data)
    # manifest 의 행 수를 고치면 jsonl 과 어긋난다
    mp = adir / "archive_manifest.json"
    m = json.loads(mp.read_text(encoding="utf-8"))
    next(t for t in m["tables"] if t["name"] == "records")["rows"] = 2
    mp.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(ArchiveError) as e:
        verify_archive_dir(adir)
    assert e.value.code == "rows_jsonl"


def test_missing_photo_fails_and_keeps_everything(db, home):
    b = create_backup(db, home)
    assert b.outcome == "completed"
    photo = next(p for p in (home / "photos").rglob("*") if p.is_file())
    photo.unlink()
    r = create_archive(db, home, photos=True)
    assert r.outcome == "failed" and r.findings[0]["code"] == "photos_incomplete" and r.archive_dir is None
    root = home / "exports" / "private" / "archives"
    assert [p.name for p in root.iterdir()] == [f"failed-{r.run_id[:8]}"] and not list(root.glob(".tmp-*"))
    assert _log(home)[-1]["outcome"] == "failed"
    assert read_latest(home)["dataset_version"] == 2 and db.status()["counts"]["records"] == 3, "백업 포인터·정본은 그대로"
    # 사진 없이 만들면 된다 (사진은 백업에 있다)
    assert create_archive(db, home).outcome == "completed"


def test_new_version_new_archive_old_untouched_and_external_dest(db, home, tmp_path):
    r1 = create_archive(db, home)
    tree1 = {p.relative_to(home / r1.archive_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in (home / r1.archive_dir).rglob("*") if p.is_file()}
    with db.transaction():
        db.bump_dataset_version()
    ext = tmp_path / "external"
    r2 = create_archive(db, home, dest_root=ext)
    assert r2.outcome == "completed" and r2.dataset_version == 3 and Path(r2.archive_dir).is_absolute() and Path(r2.archive_dir).parent == ext.resolve()
    assert json.loads((Path(r2.archive_dir) / "archive_manifest.json").read_text(encoding="utf-8"))["dataset_version"] == 3
    tree1b = {p.relative_to(home / r1.archive_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in (home / r1.archive_dir).rglob("*") if p.is_file()}
    assert tree1 == tree1b
    assert verify_archive_dir(Path(r2.archive_dir))["manifest"]["dataset_version"] == 3


def test_cli_archive_and_verify(home, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "init", "--study-id", STUDY, "--data-mode", "synthetic"]) == 0
    assert cli.main(["db", "load-seed", str(SEED_PATH)]) == 0
    assert cli.main(["db", "import", str(PACKAGES / "valid")]) == 0
    capsys.readouterr()
    assert cli.main(["db", "archive", "--photos", "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["outcome"] == "completed" and j["photos"] == 2 and j["tables"] >= 8
    adir = home / j["archive_dir"]
    assert cli.main(["db", "archive-verify", str(adir)]) == 0
    assert "보존본 검증 통과" in capsys.readouterr().out
    assert cli.main(["db", "archive-verify", str(adir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    (adir / "README.txt").write_text("changed", encoding="utf-8")
    assert cli.main(["db", "archive-verify", str(adir)]) == 1
    assert "file_hash" in capsys.readouterr().err
    assert cli.main(["db", "archive-verify", str(tmp_path / "nope")]) == 3
    monkeypatch.delenv("J5_DATA_HOME")
    assert cli.main(["db", "--db", str(home / "db" / "j5.sqlite3"), "archive"]) == 3
