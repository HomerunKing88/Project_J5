"""J5-031 VWorld 묶음 한 번에 넣기 (`j5 db parcels-ingest`) 시험.

- 범위 자동(연속지적도 전체) 변환 → 반영 전 백업 → 반영, 번들은 실데이터 홈 parcels/bundles 에 입력 해시를 붙여 둔다.
- 다시 실행하면 있던 번들을 다시 쓰고 변화 없음. 같은 내용의 두 번째 구역(경계 겹침)은 변화 없음.
- 변환 실패(VWorld 묶음 아님·없는 파일)면 백업·반영 없음. 백업 실패면 반영 없음. 반영 중간 실패면 앞 번들만 남고 결과에 적는다.
- CLI 종료 코드·글·JSON, 실데이터 홈 없으면 사용 오류.
가상자료만 쓴다 (VWorld 묶음은 시험 때 임시 폴더에 만든다).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from j5 import cli
from j5.db import ingest as ING
from j5.db.backup import BackupResult
from j5.db.store import Db
from tests.fixtures import make_parcels as mk

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
PLAIN_SHP = Path(__file__).resolve().parent / "fixtures" / "parcels" / "synthetic_shp"
STUDY = "j5-synthetic-study"
GV = "2026-09-05"


@pytest.fixture(scope="module")
def vw_zip(tmp_path_factory) -> Path:
    return mk.write_vworld_set(tmp_path_factory.mktemp("vw"))


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    yield d
    d.close()


def counts(db):
    c = db.status()["counts"]
    return c["parcels"], c["parcel_attributes"], c["parcel_attribute_snapshots"]


def test_ingest_converts_backs_up_and_loads(db, home, vw_zip):
    v0 = db.status()["dataset_version"]
    r = ING.ingest_vworld(db, home, [vw_zip], geometry_version=GV)
    assert r.outcome == "applied" and r.stage == "done", r.to_text()
    it = r.items[0]
    assert it.error is None and not it.reused and it.count == 6 and it.attrs == 6 and it.load["inserted"] == 6
    assert it.bundle.startswith("parcels/bundles/") and it.bundle.endswith(".j5parcels.json") and (home / it.bundle).is_file()
    assert counts(db) == (6, 6, 18) and r.dataset_version == db.status()["dataset_version"] > v0
    assert r.backup_dir and (home / r.backup_dir).is_dir(), "반영 전 백업"
    doc = json.loads((home / it.bundle).read_text(encoding="utf-8"))
    assert doc["source"]["geometry_version"] == GV and doc["source"]["name"] == f"VWorld 토지 자료 {vw_zip.name}" and doc["data_mode"] == "synthetic"
    txt = r.to_text()
    assert txt.startswith("VWorld 묶음 넣기: 반영됨 · 입력 1개") and "필지 6개 · 속성 6개 → 반영됨 (신규 6" in txt and "다음: `j5 db project`" in txt
    # 다시 실행: 같은 입력·같은 기준일이면 있던 번들을 다시 쓰고 변화 없음
    r2 = ING.ingest_vworld(db, home, [vw_zip], geometry_version=GV, backup=False)
    assert r2.outcome == "unchanged" and r2.items[0].reused and r2.items[0].load["outcome"] == "unchanged" and r2.backup_dir is None
    assert counts(db) == (6, 6, 18) and r2.dataset_version == r.dataset_version


def test_two_zones_overlap_is_unchanged(db, home, vw_zip, tmp_path):
    zone2 = tmp_path / "zone2_vworld.zip"
    shutil.copyfile(vw_zip, zone2)   # 같은 내용·다른 이름: 경계가 겹친 두 구역처럼
    r = ING.ingest_vworld(db, home, [vw_zip, zone2], geometry_version=GV, source_name="VWorld 종로 (가상)", backup=False)
    assert r.outcome == "applied", r.to_text()
    assert [i.load["outcome"] for i in r.items] == ["applied", "unchanged"]
    assert [json.loads((home / i.bundle).read_text(encoding="utf-8"))["source"]["name"] for i in r.items] == [f"VWorld 종로 (가상) ({vw_zip.name})", "VWorld 종로 (가상) (zone2_vworld.zip)"]
    assert counts(db) == (6, 6, 18)


def test_convert_failure_loads_nothing(db, home, vw_zip, tmp_path):
    v0 = db.status()["dataset_version"]
    plain = tmp_path / "plain_shp"
    shutil.copytree(PLAIN_SHP, plain)
    r = ING.ingest_vworld(db, home, [vw_zip, plain, tmp_path / "missing.zip"], geometry_version=GV)
    assert r.outcome == "failed" and r.stage == "convert" and r.backup_dir is None
    assert r.items[0].error is None and "VWorld 토지 자료 묶음이 아니다" in r.items[1].error and r.items[2].error == "입력 파일·폴더가 없다"
    assert counts(db) == (0, 0, 0) and db.status()["dataset_version"] == v0 and not (home / "backups").exists()
    assert "아무것도 반영하지 않았다" in r.to_text()
    # 잘못된 기준일도 변환 단계에서 거절
    bad = ING.ingest_vworld(db, home, [vw_zip], geometry_version="2026-9-5")
    assert bad.outcome == "failed" and "bad_geometry_version" in bad.items[0].error


def test_backup_failure_loads_nothing(db, home, vw_zip, monkeypatch):
    import j5.db.backup as B
    monkeypatch.setattr(B, "create_backup", lambda db_, home_: BackupResult(outcome="failed", message="가상 실패"))
    r = ING.ingest_vworld(db, home, [vw_zip], geometry_version=GV)
    assert r.outcome == "failed" and r.stage == "backup" and "가상 실패" in r.message
    assert counts(db) == (0, 0, 0) and r.items[0].load is None


def test_load_failure_midway_keeps_earlier_bundles(db, home, vw_zip, tmp_path, monkeypatch):
    zone2 = tmp_path / "zone2_vworld.zip"
    shutil.copyfile(vw_zip, zone2)
    real = ING.load_bundle
    calls = []

    def flaky(db_, doc):
        calls.append(doc["source"]["name"])
        if len(calls) == 2:
            raise RuntimeError("가상 반영 실패")
        return real(db_, doc)
    monkeypatch.setattr(ING, "load_bundle", flaky)
    r = ING.ingest_vworld(db, home, [vw_zip, zone2], geometry_version=GV, backup=False)
    assert r.outcome == "failed" and r.stage == "load"
    assert r.items[0].load["outcome"] == "applied" and "가상 반영 실패" in r.items[1].error and r.items[1].load is None
    assert counts(db) == (6, 6, 18) and "1개 번들은 반영됐고" in r.message
    monkeypatch.setattr(ING, "load_bundle", real)
    again = ING.ingest_vworld(db, home, [vw_zip, zone2], geometry_version=GV, backup=False)
    assert again.outcome == "unchanged" and all(i.reused for i in again.items)


def test_cli_parcels_ingest(db, home, vw_zip, capsys, monkeypatch):
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "parcels-ingest", str(vw_zip), "--geometry-version", GV, "--license", "가상 이용조건"]) == 0
    out = capsys.readouterr().out
    assert "VWorld 묶음 넣기: 반영됨" in out and "반영 전 백업:" in out
    assert cli.main(["db", "parcels-ingest", str(vw_zip), "--geometry-version", GV, "--no-backup", "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["outcome"] == "unchanged" and j["items"][0]["reused"] and j["backup_dir"] is None
    assert cli.main(["db", "parcels-ingest", str(home / "nope.zip"), "--geometry-version", GV, "--no-backup"]) == 1
    assert "입력 파일·폴더가 없다" in capsys.readouterr().out
    monkeypatch.delenv("J5_DATA_HOME")
    assert cli.main(["db", "--db", str(home / "db" / "j5.sqlite3"), "parcels-ingest", str(vw_zip), "--geometry-version", GV]) == cli.USAGE_ERROR
    assert "실데이터 홈을 모른다" in capsys.readouterr().err
