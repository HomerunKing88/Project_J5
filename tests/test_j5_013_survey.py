"""J5-013A: 조사 경로·점포·표본틀·세션·점포 관측·공실 집계. 완료 조건 "자료 중복·표본 변경·누락 처리" 중 조사 표본 몫 (릴리스 계획 §10),
R2 인수(§6) "표본 추가·탈락·점포 통합·조사 누락이 공실로 잘못 집계되지 않는다. 같은 점포를 두 구간에서 세지 않는다". 데이터 사전 §5, ADR-04.

시험: 수동 파일 4종 반영(멱등·불변·버전 연결·참조 검사), N/K/V 집계(미확인 상태·미조사·표본틀 밖 관측), 같은 점포 두 번 세기 거절,
표본 추가·탈락·통합 후 공통 표본 비교(단절), records 의 경로 참조 소급 채움과 정본 없는 경로 표시, 백업·파생본과의 공존, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from j5 import cli
from j5.db.backup import create_backup, restore_backup
from j5.db.importer import import_package
from j5.db.projection import build_projection
from j5.db.store import Db, DbError
from j5.db.survey import apply_input, compare, load_input, overview, vacancy
from j5.db.validate import ValidationError
from tests.conftest import PACKAGES

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
SURVEY = Path(__file__).resolve().parent / "fixtures" / "survey"
STUDY = "j5-synthetic-study"
ROUTE_V1 = "9d2e6b1c-5f4a-4c3b-9e8d-7a6b5c4d3e2f"
U = [f"2e3f4a5b-0002-4000-8000-0000000000{i:02d}" for i in range(1, 8)]
FRAME_V1, FRAME_V2 = "3f4a5b6c-0003-4000-8000-000000000001", "3f4a5b6c-0003-4000-8000-000000000002"
S1, S2 = "4a5b6c7d-0004-4000-8000-000000000001", "4a5b6c7d-0004-4000-8000-000000000002"


def doc(name: str) -> dict:
    return load_input(SURVEY / f"{name}.json")


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    yield d
    d.close()


def apply_all(db, names):
    return [apply_input(db, doc(n)) for n in names]


def test_apply_files_idempotent_and_versioned(db):
    v0 = db.status()["dataset_version"]
    rs = apply_all(db, ["route_v1", "units_v1", "frame_v1", "session_1"])
    assert [r.outcome for r in rs] == ["applied"] * 4 and rs[0].inserted == 2 and rs[1].inserted == 6 and rs[2].inserted == 1 and rs[3].inserted == 7
    assert db.status()["dataset_version"] == v0 + 4, "정본이 바뀐 배치마다 버전이 오른다"
    rs2 = apply_all(db, ["route_v1", "units_v1", "frame_v1", "session_1"])
    assert all(r.outcome == "unchanged" and r.inserted == 0 and r.updated == 0 for r in rs2) and db.status()["dataset_version"] == v0 + 4, "같은 파일 재적용은 변화 없음·버전 유지"
    # 경로 버전·표본틀·세션은 불변: 같은 ID 다른 내용은 거절, 새 버전은 previous_version_id 로
    d = doc("route_v1"); d["segments"][0]["name"] = "바뀐 이름"
    with pytest.raises(DbError) as e:
        apply_input(db, d)
    assert e.value.code == "route_version_immutable"
    d = doc("frame_v1"); d["unit_ids"] = d["unit_ids"][:3]
    with pytest.raises(DbError) as e:
        apply_input(db, d)
    assert e.value.code == "frame_version_immutable"
    d = doc("session_1"); d["observations"][0]["status"] = "vacant"
    with pytest.raises(DbError) as e:
        apply_input(db, d)
    assert e.value.code == "observation_immutable"
    d = doc("route_v2"); d["previous_version_id"] = None
    with pytest.raises(ValidationError):
        apply_input(db, d)
    d = doc("route_v2"); d["change_reason"] = None
    with pytest.raises(ValidationError):
        apply_input(db, d)
    r = apply_input(db, doc("route_v2"))
    assert r.outcome == "applied" and "v2" in r.message
    assert db.conn.execute("SELECT version_no, previous_version_id FROM survey_route_versions WHERE route_version_id = ?", (doc("route_v2")["route_version_id"],)).fetchone()[0] == 2
    # 점포 서술 필드는 갱신, 링크는 불변, 참조 검사
    r = apply_input(db, doc("units_v2"))
    assert r.inserted == 3 and r.updated == 2  # 통합 점포 1 + 링크 2, 종료 2
    d = doc("units_v2"); d["links"][0]["effective_from"] = "2026-12-01"
    with pytest.raises(DbError) as e:
        apply_input(db, d)
    assert e.value.code == "link_immutable"
    d = doc("units_v1"); d["units"][0]["asset_id"] = "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"
    with pytest.raises(ValidationError):
        apply_input(db, d)
    d = doc("frame_v2"); d["unit_ids"].append("2e3f4a5b-0002-4000-8000-000000000099")
    with pytest.raises(ValidationError):
        apply_input(db, d)
    assert db.status()["ok"]
    assert db.conn.execute("SELECT subject_type FROM subjects WHERE subject_id = ?", (U[0],)).fetchone()[0] == "survey_unit"


def test_vacancy_counts_only_confirmed_units(db):
    apply_all(db, ["route_v1", "units_v1", "frame_v1", "session_1"])
    v = vacancy(db, S1)
    assert (v["N"], v["K"], v["V"]) == (6, 4, 2) and v["vacancy_rate"] == 0.5 and v["confirmation_rate"] == pytest.approx(4 / 6)
    assert v["unconfirmed"] == {"closed_today": 1, "lease_ad_only": 1, "not_visited": 0, "unclear": 0} and v["not_recorded"] == 0
    assert "지역 전체 공실률이 아님" in v["label"]
    # 미조사(기록 없음)·미방문·확인 불가·표본틀 밖 관측은 공실이 아니다
    d = doc("session_1"); d["session_id"] = "4a5b6c7d-0004-4000-8000-000000000009"
    d["observations"] = [o for o in d["observations"] if o["unit_id"] != U[5]]
    d["observations"][2]["status"] = "not_visited"; d["observations"][3]["status"] = "unclear"
    apply_input(db, doc("units_v2"))
    d["observations"].append({"unit_id": U[6], "observed_at": "2026-09-22T10:50:00+09:00", "status": "vacant"})  # 표본틀 v1 밖
    apply_input(db, d)
    v = vacancy(db, "4a5b6c7d-0004-4000-8000-000000000009")
    assert (v["N"], v["K"], v["V"]) == (6, 2, 1) and v["not_recorded"] == 1 and v["not_recorded_units"] == [U[5]]
    assert v["unconfirmed"] == {"closed_today": 0, "lease_ad_only": 1, "not_visited": 1, "unclear": 1}
    assert v["outside_frame"] == 1 and v["outside_frame_units"] == [U[6]]
    # K 가 0 이면 비율은 null
    d["session_id"] = "4a5b6c7d-0004-4000-8000-000000000010"
    d["observations"] = [{"unit_id": U[0], "observed_at": "2026-09-22T10:50:00+09:00", "status": "unclear"}]
    apply_input(db, d)
    v = vacancy(db, "4a5b6c7d-0004-4000-8000-000000000010")
    assert v["K"] == 0 and v["vacancy_rate"] is None and v["confirmation_rate"] == 0.0 and v["not_recorded"] == 5


def test_same_unit_is_not_counted_twice_in_a_session(db):
    """가상로 2 점포는 S1·S2 두 구간에 걸친다. 같은 세션에 두 관측을 넣으면 거절되고, 분모에는 한 번만 들어간다."""
    apply_all(db, ["route_v1", "units_v1", "frame_v1"])
    d = doc("session_1")
    d["observations"].append({"unit_id": U[2], "observed_at": "2026-09-22T10:30:00+09:00", "status": "vacant", "note": "S2 쪽에서 다시 봄"})
    with pytest.raises(ValidationError) as e:
        apply_input(db, d)
    assert "한 번만" in e.value.errors[0]
    apply_input(db, doc("session_1"))
    assert db.conn.execute("SELECT COUNT(*) FROM unit_observations WHERE session_id = ? AND unit_id = ?", (S1, U[2])).fetchone()[0] == 1
    assert vacancy(db, S1)["N"] == 6
    # 세션 머리 정보는 불변: 같은 ID 로 방문 구간을 바꾸면 거절
    d = doc("session_1"); d["visited_segments"] = ["S1"]
    with pytest.raises(DbError) as e:
        apply_input(db, d)
    assert e.value.code == "session_immutable"
    d = doc("session_1"); d["visited_segments"] = ["S9"]
    with pytest.raises(ValidationError):
        apply_input(db, d)


def test_frame_change_and_merge_do_not_distort_comparison(db):
    """R2 인수: 표본 추가·탈락·점포 통합·조사 누락이 공실로 잘못 집계되지 않고, 비교는 공통 확인 점포로만 한다."""
    apply_all(db, ["route_v1", "units_v1", "frame_v1", "session_1", "route_v2", "units_v2", "frame_v2", "session_2"])
    a, b = vacancy(db, S1), vacancy(db, S2)
    assert (a["N"], a["K"], a["V"]) == (6, 4, 2) and (b["N"], b["K"], b["V"]) == (5, 4, 1)
    assert b["not_recorded_units"] == [U[5]], "가상로 5 는 2차에서 미조사: 공실도 점유도 아니다"
    c = compare(db, S1, S2)
    assert c["common"]["units"] == 3 and c["common"]["vacant_a"] == 1 and c["common"]["vacant_b"] == 1 and c["common"]["delta"] == 0.0
    assert c["in_both_frames"] == 4 and c["only_in_a"] == 2 and c["only_in_b"] == 1 and c["unconfirmed_in_either"] == 1
    assert len(c["breaks"]) == 2 and sorted(c["broken_units"]) == sorted([U[3], U[4], U[6]]), "통합된 점포와 통합 결과 점포는 비교 단절"
    assert c["session_a"]["vacancy_rate"] == 0.5 and c["session_b"]["vacancy_rate"] == 0.25, "전체 값은 공통 표본 값과 섞지 않고 따로 준다"
    # 순서를 바꿔도 공통 표본은 같다
    c2 = compare(db, S2, S1)
    assert c2["common"]["units"] == 3 and c2["common"]["vacant_a"] == 1
    o = overview(db)
    assert len(o["routes"]) == 1 and o["routes"][0]["latest_version"] == 2 and len(o["frames"]) == 2 and len(o["sessions"]) == 2 and o["units"] == 7 and o["units_closed"] == 2
    with pytest.raises(DbError):
        vacancy(db, "4a5b6c7d-0004-4000-8000-000000000099")


def test_record_survey_refs_backfill_and_unknown_route_display(home, db):
    assert import_package(db, PACKAGES / "valid", home).outcome == "applied"
    refs = {r[0]: r[1] for r in db.conn.execute("SELECT record_id, route_version_id FROM record_survey_refs")}
    assert len(refs) == 3 and set(refs.values()) == {ROUTE_V1}, "반영기가 경로 참조를 records 밖에 남긴다"
    assert overview(db)["records_with_unknown_route_version"] == 3
    apply_all(db, ["route_v1"])
    assert overview(db)["records_with_unknown_route_version"] == 0
    # 소급 채움: 참조 행을 지우고 마이그레이션 4 의 INSERT 를 다시 실행해도 같은 결과 (records 는 손대지 않음)
    from j5.db.schema import MIGRATION_0004
    from j5.db.store import _split_statements
    with db.transaction():
        db.conn.execute("DELETE FROM record_survey_refs")
        stmt = next(s for s in _split_statements(MIGRATION_0004) if s.strip().startswith("INSERT"))
        db.conn.execute(stmt)
    assert {r[0]: r[1] for r in db.conn.execute("SELECT record_id, route_version_id FROM record_survey_refs")} == refs
    with pytest.raises(Exception):
        with db.transaction():
            db.conn.execute("UPDATE records SET observed_at = '2026-01-01' WHERE record_id = ?", (next(iter(refs)),))


def test_survey_data_survives_backup_restore_and_projection(db, home, tmp_path):
    assert import_package(db, PACKAGES / "valid", home).outcome == "applied"
    apply_all(db, ["route_v1", "units_v1", "frame_v1", "session_1"])
    assert build_projection(db, home).outcome == "published", "파생본은 물건·기록만 담지만 조사 자료가 있어도 생성된다"
    b = create_backup(db, home)
    assert b.outcome == "completed" and b.counts["unit_observations"] == 6 and b.counts["survey_units"] == 6
    rr = restore_backup(home / b.backup_dir, tmp_path / "restored")
    assert rr.outcome == "completed"
    with Db.open(tmp_path / "restored" / "db" / "j5.sqlite3") as rdb:
        assert vacancy(rdb, S1)["V"] == 2 and rdb.status()["counts"]["survey_sessions"] == 1


def test_cli_survey_commands(home, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "init", "--study-id", STUDY, "--data-mode", "synthetic"]) == 0
    assert cli.main(["db", "load-seed", str(SEED_PATH)]) == 0
    capsys.readouterr()
    for n in ("route_v1", "units_v1", "frame_v1", "session_1"):
        assert cli.main(["db", "survey-apply", str(SURVEY / f"{n}.json")]) == 0
        assert "반영됨" in capsys.readouterr().out
    assert cli.main(["db", "survey-apply", str(SURVEY / "session_1.json"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "unchanged"
    assert cli.main(["db", "survey-vacancy", S1]) == 0
    out = capsys.readouterr().out
    assert "N 6 · K 4 · V 2" in out and "0.500" in out and "지역 전체 공실률이 아님" in out
    for n in ("route_v2", "units_v2", "frame_v2", "session_2"):
        assert cli.main(["db", "survey-apply", str(SURVEY / f"{n}.json")]) == 0
    capsys.readouterr()
    assert cli.main(["db", "survey-compare", S1, S2, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["common"]["units"] == 3
    assert cli.main(["db", "survey-overview"]) == 0
    assert "경로 1개, 표본틀 버전 2개, 세션 2개, 점포 7개 (종료 2)" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text('{"kind": "session"}', encoding="utf-8")
    assert cli.main(["db", "survey-apply", str(bad)]) == 1
    assert "스키마" in capsys.readouterr().err
    d = doc("session_1"); d["observations"][0]["status"] = "vacant"
    (tmp_path / "conflict.json").write_text(json.dumps(d), encoding="utf-8")
    assert cli.main(["db", "survey-apply", str(tmp_path / "conflict.json")]) == 1
    assert "observation_immutable" in capsys.readouterr().err
    assert cli.main(["db", "survey-vacancy", "4a5b6c7d-0004-4000-8000-000000000099"]) == 1
    assert cli.main(["db", "survey-apply", str(tmp_path / "none.json")]) == cli.USAGE_ERROR
