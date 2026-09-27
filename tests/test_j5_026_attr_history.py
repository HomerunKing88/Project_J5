"""J5-026: 필지 속성 스냅샷과 이력 (ADR-19 확장, db_schema 16).

시험: 마이그레이션 16 의 기존 속성 백필(출처에 든 자료 종류만), 반영 시 스냅샷 신규(새 기준일은 값이 같아도 신규)·같은 기준일 교체·부분 자료 묶음, 변화 계산(처음 확인·값 변경·규제 목록),
파생본 parcels.geojson 의 attrs_history(스키마·상한), attribute_history·history_text, CLI parcels-history, 상태 행 수·백업. 가상자료만 쓴다.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.backup import create_backup, restore_backup
from j5.db.parcels import MAX_HISTORY_ENTRIES, _snapshot_changes, attribute_history, history_text, load_bundle, parcels_bundle_from_db
from j5.db.projection import build_projection
from j5.db.store import Db, DbError
from j5.schemas_loader import schema_errors

FIX = Path(__file__).resolve().parent / "fixtures" / "parcels"
VW_BUNDLE = FIX / "synthetic_vworld.j5parcels.json"
SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
STUDY = "j5-synthetic-study"
P1, P11, PM = "9999900100100010000", "9999900100100010001", "9999900100200010002"


def vw_bundle() -> dict:
    return json.loads(VW_BUNDLE.read_text(encoding="utf-8"))


def later_bundle(as_of: str, *, price=None, owner=None, zone=None, kinds=None) -> dict:
    """가상 번들의 변형: 기준일을 바꾸고 필지 1 의 공시지가·소유·용도지역을 바꾼다. kinds 를 주면 그 자료 종류만 든 번들."""
    b = vw_bundle()
    b["source"]["geometry_version"] = as_of
    f1 = next(f for f in b["features"] if f["id"] == P1)
    if price is not None:
        f1["properties"]["attrs"]["official_land_price_krw_m2"] = price
        f1["properties"]["attrs"]["price_base_year"] = int(as_of[:4])
    if owner is not None:
        f1["properties"]["attrs"].update({"ownership_kind_code": "06", "ownership_kind": owner, "ownership_changed_on": as_of, "ownership_change_cause_code": "04"})
    if zone is not None:
        f1["properties"]["attrs"]["use_zone_1"] = zone
    if kinds:
        from j5.db.parcels import ATTR_GROUPS
        keep = {k for kind in kinds for k in ATTR_GROUPS[kind]}
        b["attrs_sources"] = [a for a in b["attrs_sources"] if a["kind"] in kinds]
        for f in b["features"]:
            f["properties"]["attrs"] = {k: v for k, v in f["properties"]["attrs"].items() if k in keep}
    return b


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    yield d
    d.close()


def snaps(db, pnu):
    return [dict(r) for r in db.conn.execute("SELECT s.kind, s.as_of, s.values_json, s.source_json FROM parcel_attribute_snapshots s JOIN parcels p USING (parcel_id) WHERE p.pnu = ? ORDER BY s.as_of, s.kind", (pnu,))]


def test_schema_16_and_status_counts(db):
    st = db.status()
    assert st["db_schema_version"] == S.DB_SCHEMA_VERSION == 16 and st["counts"]["parcel_attribute_snapshots"] == 0 and db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_first_load_writes_one_snapshot_per_kind_and_migration_backfills_from_v15(db, home):
    r = load_bundle(db, vw_bundle())
    assert r.snapshots_inserted == 6 * 3 and r.snapshots_replaced == 0, r.to_text()   # 소유 자료에 행이 없는 산1-2 도 소유 스냅샷(모두 null)은 있다: 자료가 있고 행이 없음
    s1 = snaps(db, P1)
    assert [(s["kind"], s["as_of"]) for s in s1] == [("land_feature", "2026-09-05"), ("land_ownership", "2026-09-05"), ("land_plan", "2026-09-05")]
    assert json.loads(s1[0]["values_json"])["official_land_price_krw_m2"] == 12340000 and json.loads(s1[0]["source_json"])["kind"] == "land_feature"
    assert db.status()["counts"]["parcel_attribute_snapshots"] == 18
    # 다시 반영: 스냅샷도 변화 없음
    r2 = load_bundle(db, vw_bundle())
    assert r2.outcome == "unchanged" and r2.snapshots_inserted == 0 and db.status()["counts"]["parcel_attribute_snapshots"] == 18
    # 버전 15 정본처럼 만들고(스냅샷 표 삭제·마이그레이션 행 삭제) 다시 열면 기존 속성이 첫 스냅샷으로 백필된다
    path = db.path
    db.close()
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE parcel_attribute_snapshots")
    conn.execute("DELETE FROM schema_migrations WHERE version = 16")
    conn.commit()
    conn.close()
    with Db.open(path) as d2:
        assert d2.schema_version() == 16 and d2.status()["counts"]["parcel_attribute_snapshots"] == 18 and d2.status()["ok"]
        rows = snaps(d2, P1)
        assert json.loads(rows[0]["values_json"]) == {"jimok_name": "대", "registered_area_m2": 1770.5, "official_land_price_krw_m2": 12340000, "price_base_year": 2026, "price_base_month": 1,
                                                       "use_zone_1": "일반상업지역", "use_zone_2": None, "land_use_situation": "상업용", "road_side": "광대로한면", "terrain_height": "평지", "terrain_form": "세로장방"}
        plan = json.loads(next(s["values_json"] for s in rows if s["kind"] == "land_plan"))
        assert plan["plan_zones"][1]["name"] == "일반상업지역" and plan["plan_zones_truncated"] is False
        assert all(len(r_[0]) == 36 for r_ in d2.conn.execute("SELECT snapshot_id FROM parcel_attribute_snapshots"))
        # 백필된 스냅샷과 같은 번들을 다시 넣어도 변화 없음 (값 비교가 SQL json 과 Python 정규화 사이에서 일치)
        assert load_bundle(d2, vw_bundle()).outcome == "unchanged"


def test_new_as_of_adds_snapshots_even_when_unchanged_and_same_as_of_replaces(db):
    load_bundle(db, vw_bundle())
    r = load_bundle(db, later_bundle("2027-01-15"))
    assert r.outcome == "applied" and r.attrs_updated == 6 and r.snapshots_inserted == 18 and r.snapshots_replaced == 0, "값이 같아도 새 기준일이면 스냅샷이 는다 (그 날짜에도 같았다)"
    assert [s["as_of"] for s in snaps(db, P1)] == ["2026-09-05"] * 3 + ["2027-01-15"] * 3
    r = load_bundle(db, later_bundle("2027-01-15", price=13_000_000))
    assert r.snapshots_inserted == 0 and r.snapshots_replaced == 1 and r.attrs_updated == 1
    assert json.loads(next(s["values_json"] for s in snaps(db, P1) if s["as_of"] == "2027-01-15" and s["kind"] == "land_feature"))["official_land_price_krw_m2"] == 13_000_000
    # 소유만 든 부분 번들: 소유 스냅샷만 늘고 나머지는 그대로
    r = load_bundle(db, later_bundle("2027-06-01", owner="법인", kinds=["land_ownership"]))
    assert r.snapshots_inserted == 5 and r.attrs_updated == 5 and [s["kind"] for s in snaps(db, P1) if s["as_of"] == "2027-06-01"] == ["land_ownership"], "소유 자료에 행이 없는 산1-2 는 속성이 비어 반영 대상이 아니다"
    # 더 오래된 기준일은 거절 (정본 그대로)
    with pytest.raises(DbError) as e:
        load_bundle(db, later_bundle("2026-01-01", price=1))
    assert e.value.code == "parcel_older" and db.status()["counts"]["parcel_attribute_snapshots"] == 18 + 18 + 5   # 필지 규칙이 먼저 거절한다


def test_snapshot_changes_first_then_diffs():
    snaps_ = [{"kind": "land_feature", "as_of": "2026-09-05", "values_json": json.dumps({"jimok_name": "대", "official_land_price_krw_m2": 100, "use_zone_2": None})},
              {"kind": "land_feature", "as_of": "2027-01-15", "values_json": json.dumps({"jimok_name": "대", "official_land_price_krw_m2": 120, "use_zone_2": None})},
              {"kind": "land_feature", "as_of": "2028-01-15", "values_json": json.dumps({"jimok_name": "대", "official_land_price_krw_m2": 120, "use_zone_2": None})},
              {"kind": "land_plan", "as_of": "2026-09-05", "values_json": json.dumps({"plan_zones": [{"code": "A", "name": "a", "relation": "포함"}], "plan_zones_truncated": False})},
              {"kind": "land_plan", "as_of": "2027-01-15", "values_json": json.dumps({"plan_zones": [{"code": "A", "name": "a", "relation": "포함"}, {"code": "B", "name": "b", "relation": "저촉"}], "plan_zones_truncated": False})}]
    ch = _snapshot_changes(snaps_)
    assert [(c["as_of"], c["kind"], c["first"], sorted(c["changes"])) for c in ch] == [
        ("2026-09-05", "land_feature", True, ["jimok_name", "official_land_price_krw_m2"]), ("2026-09-05", "land_plan", True, ["plan_zones"]),
        ("2027-01-15", "land_feature", False, ["official_land_price_krw_m2"]), ("2027-01-15", "land_plan", False, ["plan_zones"])]
    assert ch[2]["changes"]["official_land_price_krw_m2"] == {"from": 100, "to": 120}
    assert ch[0]["changes"]["jimok_name"] == {"from": None, "to": "대"} and "use_zone_2" not in ch[0]["changes"], "처음 확인은 값 있는 필드만"
    assert _snapshot_changes([]) == []
    zero = _snapshot_changes([{"kind": "land_ownership", "as_of": "2026-09-05", "values_json": json.dumps({"co_owner_count": 0, "ownership_kind": None})},
                              {"kind": "land_feature", "as_of": "2026-09-05", "values_json": json.dumps({"official_land_price_krw_m2": 0, "registered_area_m2": 0.0})}])
    assert [c["changes"] for c in zero] == [{"official_land_price_krw_m2": {"from": None, "to": 0}, "registered_area_m2": {"from": None, "to": 0.0}}, {"co_owner_count": {"from": None, "to": 0}}], "0 은 값이다 (리뷰 반영)"
    # 스키마: 바뀐 필드는 그 자료 종류의 허용 필드만
    base = {"type": "FeatureCollection", "j5parcels": "1.0.0"}
    good = {"as_of": "2026-09-05", "kind": "land_ownership", "first": True, "changes": {"ownership_kind": {"from": None, "to": "개인"}}}
    bad_owner = {**good, "changes": {"owner_name": {"from": None, "to": "x"}}}
    wrong_kind = {**good, "changes": {"jimok_name": {"from": None, "to": "대"}}}
    for entry, ok in ((good, True), (bad_owner, False), (wrong_kind, False)):
        b = vw_bundle()
        b["features"][0]["properties"]["attrs_history"] = [entry]
        assert (schema_errors("parcels_bundle.schema.json", b) == []) is ok, entry


def test_projection_carries_history_and_cli_history(db, home, capsys, monkeypatch):
    load_bundle(db, vw_bundle())
    load_bundle(db, later_bundle("2027-01-15", price=13_000_000, owner="법인", zone="준주거지역"))
    b = parcels_bundle_from_db(db, generated_at="2027-02-01T00:00:00Z")
    assert schema_errors("parcels_bundle.schema.json", b) == []
    f1 = next(f for f in b["features"] if f["id"] == P1)
    h = f1["properties"]["attrs_history"]
    assert [(c["as_of"], c["kind"], c["first"]) for c in h] == [("2026-09-05", "land_feature", True), ("2026-09-05", "land_ownership", True), ("2026-09-05", "land_plan", True), ("2027-01-15", "land_feature", False), ("2027-01-15", "land_ownership", False)]
    assert h[3]["changes"] == {"official_land_price_krw_m2": {"from": 12340000, "to": 13000000}, "price_base_year": {"from": 2026, "to": 2027}, "use_zone_1": {"from": "일반상업지역", "to": "준주거지역"}}
    assert h[4]["changes"]["ownership_kind"] == {"from": "개인", "to": "법인"}
    f11 = next(f for f in b["features"] if f["id"] == P11)
    assert [c["first"] for c in f11["properties"]["attrs_history"]] == [True, True, True], "값이 안 바뀐 필지는 처음 확인 항목만"
    assert f1["properties"]["attrs"]["as_of"] == "2027-01-15"
    proj = build_projection(db, home, photos=False)
    assert proj.outcome == "published", proj.message
    with zipfile.ZipFile(home / proj.output_dir / proj.zip_name) as zf:
        pc = json.loads(zf.read("parcels.geojson"))
    # 처음 확인 항목은 값이 있는 필드가 있을 때만: 소유 값이 모두 null 인 필지 2개(3번·산1-2)는 소유 항목이 없다 → 3+3+3+2+3+2 = 16 + 필지 1 의 변경 2
    assert sum(len(f["properties"].get("attrs_history", [])) for f in pc["features"]) == 16 + 2
    # attribute_history · history_text
    ah = attribute_history(db, P1)
    assert ah["current"]["official_land_price_krw_m2"] == 13_000_000 and len(ah["snapshots"]) == 6 and ah["snapshots"][0]["source"]["kind"] == "land_feature" and len(ah["changes"]) == 5
    txt = history_text(ah)
    assert "공시지가(원/㎡): 12,340,000 → 13,000,000" in txt and "소유 구분: 개인 → 법인" in txt and "처음 확인" in txt and "확인·판단이 아니다" in txt
    with pytest.raises(DbError):
        attribute_history(db, "1" * 19)
    # CLI
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    db.close()
    assert cli.main(["db", "parcels-history", P1]) == 0
    assert "13,000,000" in capsys.readouterr().out
    assert cli.main(["db", "parcels-history", P1, "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["pnu"] == P1 and len(j["snapshots"]) == 6 and j["changes"][3]["changes"]["use_zone_1"]["to"] == "준주거지역"
    assert cli.main(["db", "parcels-history", "1" * 19]) == 1
    assert "parcel_missing" in capsys.readouterr().err

    # 출처는 모든 속성 행에서 모은다: 필지 1 만 든 다른 파일(해시 다름)을 넣으면 출처 4개 (리뷰 반영)
    one = later_bundle("2027-03-01", price=13_500_000, kinds=["land_feature"])
    one["features"] = [f for f in one["features"] if f["id"] == P1]
    one["count"] = 1
    one["attrs_sources"][0]["dbf_sha256"] = "4" * 64
    d3 = Db.open(home / "db" / "j5.sqlite3")
    load_bundle(d3, one)
    b2 = parcels_bundle_from_db(d3, generated_at="2027-03-02T00:00:00Z")
    assert sorted((s_["kind"], s_["dbf_sha256"][:1]) for s_ in b2["attrs_sources"]) == [("land_feature", "4"), ("land_feature", vw_bundle()["attrs_sources"][0]["dbf_sha256"][:1]), ("land_ownership", vw_bundle()["attrs_sources"][2]["dbf_sha256"][:1]), ("land_plan", vw_bundle()["attrs_sources"][1]["dbf_sha256"][:1])]
    d3.close()

def test_history_cap_keeps_latest_entries(db, monkeypatch):
    load_bundle(db, vw_bundle())
    monkeypatch.setattr("j5.db.parcels.MAX_HISTORY_ENTRIES", 2)
    load_bundle(db, later_bundle("2027-01-15", price=13_000_000))
    b = parcels_bundle_from_db(db, generated_at="2027-02-01T00:00:00Z")
    h = next(f for f in b["features"] if f["id"] == P1)["properties"]["attrs_history"]
    assert len(h) == 2 and h[-1]["as_of"] == "2027-01-15" and MAX_HISTORY_ENTRIES >= 2


def test_backup_restore_keeps_snapshots(db, home, tmp_path):
    load_bundle(db, vw_bundle())
    b = create_backup(db, home)
    assert b.outcome == "completed", b.to_text()
    rr = restore_backup(home / b.backup_dir, tmp_path / "restored")
    assert rr.outcome == "completed", rr.to_text()
    with Db.open(tmp_path / "restored" / "db" / "j5.sqlite3") as rdb:
        assert rdb.status()["counts"]["parcel_attribute_snapshots"] == 18 and rdb.status()["ok"]
