"""J5-013B-2: 정본 필지·물건 구성 (데이터 사전 §2 parcels / asset_components, §6, ADR-13).

시험: db_schema 5 마이그레이션·상태 행 수, 필지 번들 반영(신규·변화 없음·새 도형 기준일 갱신·같은 기준일 충돌·오래된 기준일·data_mode 불일치·출처 문서),
위치점 포함 연결 제안(정본에 쓰지 않음)과 검토 후 반영(멱등·갱신·물건/필지 없음·날짜), 파생본의 parcels.geojson(번들 스키마·연결 물건·검증기),
백업·복구에 새 테이블 포함, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.backup import create_backup, restore_backup
from j5.db.parcels import active_links, apply_links, geometry_contains, load_bundle, load_json, parcels_bundle_from_db, suggest_links
from j5.db.projection import ProjectionError, build_projection, verify_projection_dir
from j5.db.store import Db, DbError
from j5.db.validate import ValidationError
from j5.schemas_loader import schema_errors

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
BUNDLE = Path(__file__).resolve().parent / "fixtures" / "parcels" / "synthetic.j5parcels.json"
STUDY = "j5-synthetic-study"
A = [f"7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a5{i}" for i in range(5)]
P1, P11, P2, P3, P42, PM = ("9999900100100010000", "9999900100100010001", "9999900100100020000", "9999900100100030000", "9999900100100040002", "9999900100200010002")


def bundle() -> dict:
    return json.loads(BUNDLE.read_text(encoding="utf-8"))


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))  # dataset_version 1
    yield d
    d.close()


def test_schema_5_tables_and_status_counts(db):
    st = db.status()
    assert st["db_schema_version"] == S.DB_SCHEMA_VERSION == 5 and st["counts"]["parcels"] == 0 and st["counts"]["asset_components"] == 0
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(parcels)")}
    for c in ("pnu", "parcel_id", "registered_area_m2", "registered_area_missing_reason", "geom_area_m2", "geometry_crs", "geometry_version", "address_status", "source_document_id"):
        assert c in cols, c


def test_load_bundle_insert_unchanged_update_conflict(db):
    r = load_bundle(db, bundle())
    assert r.outcome == "applied" and r.inserted == 6 and r.dataset_version == 2 and r.source_document_id
    st = db.status()
    assert st["ok"] and st["counts"]["parcels"] == 6 and st["counts"]["subjects"] == 11 and st["counts"]["source_documents"] == 1
    row = dict(db.conn.execute("SELECT * FROM parcels WHERE pnu = ?", (P42,)).fetchone())
    assert row["label"] == "4-2" and row["jimok"] == "도" and row["geom_area_m2"] > 0 and row["geom_area_missing_reason"] is None
    assert row["registered_area_m2"] is None and row["registered_area_missing_reason"] == "not_collected", "공부면적은 아직 수집하지 않음: null + 사유"
    assert row["geometry_crs"] == "EPSG:4326" and row["source_crs"] == "EPSG:5186" and row["geometry_version"] == "2026-09-01" and row["address_status"] == "unverified"
    assert db.conn.execute("SELECT subject_type FROM subjects WHERE subject_id = ?", (row["parcel_id"],)).fetchone()[0] == "parcel"
    doc = dict(db.conn.execute("SELECT * FROM source_documents").fetchone())
    assert doc["document_kind"] == "official_file" and doc["sha256"] == bundle()["source"]["shp_sha256"] and doc["source_published_at"] == "2026-09-01"
    # 같은 번들 다시 → 변화 없음, 버전 유지, 출처 문서 추가 없음
    r2 = load_bundle(db, bundle())
    assert r2.outcome == "unchanged" and r2.unchanged == 6 and r2.dataset_version == 2 and db.status()["counts"]["source_documents"] == 1
    # 새 도형 기준일 + 바뀐 도형 → 갱신 (parcel_id 유지)
    b3 = bundle()
    b3["source"]["geometry_version"] = "2026-10-01"
    b3["features"][0]["properties"]["label"] = "1"
    b3["features"][0]["geometry"]["coordinates"][0][1][0] += 0.00001
    r3 = load_bundle(db, b3)
    assert r3.outcome == "applied" and r3.updated == 6 and r3.unchanged == 0 and r3.dataset_version == 3, "기준일이 바뀌면 (내용이 같아도 출처가 바뀌므로) 모든 필지가 갱신된다"
    row3 = dict(db.conn.execute("SELECT * FROM parcels WHERE pnu = ?", (P1,)).fetchone())
    assert row3["parcel_id"] == db.conn.execute("SELECT parcel_id FROM parcels WHERE pnu = ?", (P1,)).fetchone()[0] and row3["geometry_version"] == "2026-10-01"
    assert db.status()["counts"]["parcels"] == 6 and db.status()["counts"]["subjects"] == 11
    # 같은 기준일·다른 내용 → 거절(전체 롤백), 오래된 기준일 → 거절
    b4 = bundle()
    b4["source"]["geometry_version"] = "2026-10-01"
    b4["features"][1]["properties"]["jimok"] = "잡"
    with pytest.raises(DbError) as e:
        load_bundle(db, b4)
    assert e.value.code == "parcel_conflict" and db.status()["dataset_version"] == 3
    with pytest.raises(DbError) as e:
        load_bundle(db, bundle())  # 2026-09-01 < 2026-10-01 인데 내용이 다름
    assert e.value.code == "parcel_older"
    # 잘못된 번들·data_mode 불일치
    with pytest.raises(ValidationError):
        load_bundle(db, {**bundle(), "count": 1})
    real = bundle()
    real["data_mode"] = "real"
    with pytest.raises(DbError) as e:
        load_bundle(db, real)
    assert e.value.code == "data_mode_mismatch"


def test_suggest_and_apply_links(db, tmp_path):
    load_bundle(db, bundle())
    sug = suggest_links(db, effective_from="2026-09-23")
    pairs = {(l["asset_id"], l["pnu"]) for l in sug["links"]}
    assert pairs == {(A[0], P1), (A[1], P11), (A[3], P2), (A[4], P3)}, "가상 시드 1·2·4·5 → 필지 1·1-1·2·3"
    assert sug["_unlocated_asset_ids"] == [A[2]] and sug["_unmatched_asset_ids"] == []
    assert all(l["basis"] == "location_point" and l["effective_to"] is None for l in sug["links"])
    assert db.status()["counts"]["asset_components"] == 0 and db.status()["dataset_version"] == 2, "제안은 정본에 쓰지 않는다"
    assert schema_errors("asset_components_input.schema.json", {k: v for k, v in sug.items() if not k.startswith("_")}) == []
    # 검토 후 반영 (제안 그대로)
    doc = {k: v for k, v in sug.items() if not k.startswith("_")}
    r = apply_links(db, doc)
    assert r.outcome == "applied" and r.inserted == 4 and r.dataset_version == 3 and db.status()["ok"]
    assert len(active_links(db, "2026-09-23")) == 4 and active_links(db, "2026-09-01") == [], "시작일 전에는 유효하지 않다"
    assert apply_links(db, doc).outcome == "unchanged"
    assert suggest_links(db, effective_from="2026-09-23")["links"] == [], "이미 연결된 쌍은 제안하지 않는다"
    # 종료일 갱신, 수동 연결 추가(주소만 있는 물건 3 → 필지 4-2)
    doc2 = {"kind": "asset_components", "links": [{"asset_id": A[0], "pnu": P1, "effective_from": "2026-09-23", "effective_to": "2026-12-31", "basis": "location_point", "note": "연말 종료"},
                                                  {"asset_id": A[2], "pnu": P42, "effective_from": "2026-09-23", "effective_to": None, "basis": "manual", "note": "현장 확인"}]}
    r2 = apply_links(db, doc2)
    assert r2.outcome == "applied" and r2.updated == 1 and r2.inserted == 1 and r2.dataset_version == 4
    assert [l["pnu"] for l in active_links(db, "2027-01-01") if l["asset_id"] == A[0]] == [], "종료일 뒤에는 유효하지 않다"
    assert [l["pnu"] for l in active_links(db, "2026-12-31") if l["asset_id"] == A[0]] == [], "종료일 당일도 유효하지 않다 (반개구간)"
    assert [l["pnu"] for l in active_links(db, "2026-12-30") if l["asset_id"] == A[0]] == [P1]
    assert any(l["pnu"] == P1 for l in suggest_links(db, effective_from="2026-12-31")["links"]), "종료일부터는 다시 제안한다"
    # 오류: 정본에 없는 물건·필지, 날짜 순서, 달력에 없는 날짜, 스키마
    for links, code in (([{"asset_id": "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99", "pnu": P1, "effective_from": "2026-09-23", "effective_to": None, "basis": "manual"}], "물건"),
                        ([{"asset_id": A[0], "pnu": "1111017500100010000", "effective_from": "2026-09-23", "effective_to": None, "basis": "manual"}], "PNU"),
                        ([{"asset_id": A[0], "pnu": P1, "effective_from": "2026-09-23", "effective_to": "2026-09-01", "basis": "manual"}], "뒤여야"),
                        ([{"asset_id": A[0], "pnu": P1, "effective_from": "2026-02-30", "effective_to": None, "basis": "manual"}], "date")):
        with pytest.raises(ValidationError) as e:
            apply_links(db, {"kind": "asset_components", "links": links})
        assert code in str(e.value)
    with pytest.raises(ValidationError):
        apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[0], "pnu": P1}]})
    # 같은 물건·필지의 겹치는 기간은 거절, 반개구간이라 끝날 = 다음 시작일은 허용 (물건 1·필지 1: [2026-09-23, 2026-12-31) 뒤 [2026-12-31, ∞))
    with pytest.raises(ValidationError) as e:
        apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[0], "pnu": P1, "effective_from": "2026-10-01", "effective_to": None, "basis": "manual"}]})
    assert "겹친다" in str(e.value)
    with pytest.raises(ValidationError):
        apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[2], "pnu": P42, "effective_from": "2026-01-01", "effective_to": "2026-09-30", "basis": "manual"}]})
    assert apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[2], "pnu": P42, "effective_from": "2026-01-01", "effective_to": "2026-09-23", "basis": "manual"}]}).inserted == 1, "끝날 = 기존 시작일은 겹침이 아니다"
    with pytest.raises(ValidationError):
        apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[2], "pnu": P42, "effective_from": "2026-03-01", "effective_to": "2026-03-01", "basis": "manual"}]})
    r3 = apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[0], "pnu": P1, "effective_from": "2026-12-31", "effective_to": None, "basis": "manual"}]})
    assert r3.inserted == 1 and [l["effective_from"] for l in active_links(db, "2027-03-01") if l["asset_id"] == A[0]] == ["2026-12-31"]
    assert db.status()["dataset_version"] == 6, "실패는 정본을 바꾸지 않는다"
    with pytest.raises(ValidationError):
        suggest_links(db, effective_from="2026-13-01")


def test_geometry_contains_hole_and_multipart():
    by = {f["properties"]["label"]: f["geometry"] for f in bundle()["features"]}
    assert geometry_contains(by["4-2"], 126.9995, 37.57055) and not geometry_contains(by["4-2"], 126.9994, 37.5704), "구멍 안은 밖"
    assert geometry_contains(by["산1-2"], 127.0001, 37.5703) and not geometry_contains(by["산1-2"], 127.0002, 37.5703)


def test_projection_includes_parcels_geojson_with_links(db, home):
    r0 = build_projection(db, home)
    assert r0.outcome == "published" and r0.counts["parcels"] == 0 and not (home / r0.output_dir / "parcels.geojson").exists()
    load_bundle(db, bundle())
    sug = suggest_links(db, effective_from="2026-09-23")
    apply_links(db, {k: v for k, v in sug.items() if not k.startswith("_")})
    r = build_projection(db, home)
    assert r.outcome == "published", r.to_text()
    assert r.counts["parcels"] == 6 and r.source_dataset_version == 3
    out = home / r.output_dir
    pb = json.loads((out / "parcels.geojson").read_text(encoding="utf-8"))
    assert schema_errors("parcels_bundle.schema.json", pb) == [] and pb["count"] == 6 and pb["data_mode"] == "synthetic" and pb["generated_at"] == r.generated_at
    by = {f["id"]: f["properties"] for f in pb["features"]}
    assert by[P1]["asset_ids"] == [A[0]] and by[P11]["asset_ids"] == [A[1]] and by[P42]["asset_ids"] == [] and by[P1]["geometry_version"] == "2026-09-01"
    assert pb["source"]["name"] == "가상 연속지적도 (synthetic)" and pb["source"]["crs"]["epsg"] == 5186 and pb["warnings"] == []
    assert pb["source"]["crs"]["ellipsoid"] == "GRS80" and pb["source"]["crs"]["datum_shift"] is None, "원본 좌표계 정보를 그대로 전한다"
    assert pb["study_id"] == STUDY and pb["source_dataset_version"] == 3
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert any(f["path"] == "parcels.geojson" for f in manifest["files"]) and manifest["counts"]["parcels"] == 6
    # 검증기: 연결 물건이 목록 밖이면 거절, 필지 수가 다르면 거절
    pb_bad = dict(pb)
    pb_bad["features"] = [{**pb["features"][0], "properties": {**pb["features"][0]["properties"], "asset_ids": ["7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"]}}] + pb["features"][1:]
    tampered = home / "tampered"
    import shutil
    shutil.copytree(out, tampered)
    (tampered / "parcels.geojson").write_text(json.dumps(pb_bad, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    m = json.loads((tampered / "manifest.json").read_text(encoding="utf-8"))
    for f in m["files"]:
        if f["path"] == "parcels.geojson":
            data = (tampered / "parcels.geojson").read_bytes()
            import hashlib
            f["bytes"], f["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    (tampered / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ProjectionError) as e:
        verify_projection_dir(tampered, expected_version=3)
    assert e.value.code == "verify_parcel_link"
    # 출처가 섞이면 warnings 에 적힌다 (정본 조회 함수)
    b2 = bundle()
    b2["source"]["geometry_version"] = "2026-10-01"
    b2["source"]["name"] = "가상 연속지적도 2차 (synthetic)"
    b2["features"] = b2["features"][:1]
    b2["count"] = 1
    b2["features"][0]["geometry"]["coordinates"][0][1][0] += 0.00001
    load_bundle(db, b2)
    mixed = parcels_bundle_from_db(db, generated_at="2026-10-02T00:00:00Z")
    assert mixed["source"]["geometry_version"] == "2026-10-01" and any("섞여" in w for w in mixed["warnings"])
    # Bessel(Korean 1985) 출처는 좌표계·데이텀 변환이 보존되고 정확도 경고가 다시 붙는다
    b5 = bundle()
    b5["source"]["geometry_version"] = "2026-11-01"
    b5["source"]["crs"] = {"epsg": 5174, "name": "Korean 1985 / Modified Central Belt", "ellipsoid": "Bessel", "datum_shift": "korean1985", "detected_from": "prj"}
    load_bundle(db, b5)
    bes = parcels_bundle_from_db(db, generated_at="2026-11-02T00:00:00Z")
    assert bes["source"]["crs"] == {"epsg": 5174, "name": "EPSG:5174", "ellipsoid": "Bessel", "datum_shift": "korean1985", "detected_from": "argument"}
    assert any("Bessel" in w for w in bes["warnings"])
    row = dict(db.conn.execute("SELECT source_ellipsoid, source_datum_shift FROM parcels WHERE pnu = ?", (P1,)).fetchone())
    assert row == {"source_ellipsoid": "Bessel", "source_datum_shift": "korean1985"}


def test_aggregate_parcel_limit_enforced_on_load(db, monkeypatch):
    """리뷰 반영: 번들마다 8,000 이하라도 여러 번 넣으면 정본 합계가 상한을 넘어 파생본이 영영 실패한다. 반영 전에 합계를 검사한다."""
    from j5.db import parcels as PM
    load_bundle(db, bundle())
    monkeypatch.setattr(PM, "MAX_FEATURES", 8)
    b2 = bundle()
    for i, f in enumerate(b2["features"][:3]):
        f["id"] = f["properties"]["pnu"] = f"9999900200100{i:02d}0000"
    b2["features"] = b2["features"][:3]
    b2["count"] = 3
    with pytest.raises(DbError) as e:
        load_bundle(db, b2)
    assert e.value.code == "parcels_limit" and db.status()["counts"]["parcels"] == 6 and db.status()["dataset_version"] == 2
    b2["features"] = b2["features"][:2]
    b2["count"] = 2
    assert load_bundle(db, b2).inserted == 2 and db.status()["counts"]["parcels"] == 8


def test_parcels_survive_backup_and_restore(db, home, tmp_path):
    load_bundle(db, bundle())
    apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[0], "pnu": P1, "effective_from": "2026-09-23", "effective_to": None, "basis": "manual", "note": None}]})
    b = create_backup(db, home)
    assert b.outcome == "completed" and b.counts["parcels"] == 6 and b.counts["asset_components"] == 1
    rr = restore_backup(home / b.backup_dir, tmp_path / "restored")
    assert rr.outcome == "completed", rr.to_text()
    with Db.open(tmp_path / "restored" / "db" / "j5.sqlite3") as rdb:
        assert rdb.status()["counts"]["parcels"] == 6 and len(active_links(rdb, "2026-09-23")) == 1 and rdb.status()["ok"]


def test_cli_parcels_db_commands(home, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "init", "--study-id", STUDY, "--data-mode", "synthetic"]) == 0
    assert cli.main(["db", "load-seed", str(SEED_PATH)]) == 0
    capsys.readouterr()
    assert cli.main(["db", "parcels-load", str(BUNDLE)]) == 0
    out = capsys.readouterr().out
    assert "필지 반영: 반영됨 · 신규 6" in out and "dataset_version 2" in out
    assert cli.main(["db", "parcels-load", str(BUNDLE), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "unchanged"
    assert cli.main(["db", "parcels-load", str(tmp_path / "nope.json")]) == cli.USAGE_ERROR
    bad = tmp_path / "bad.j5parcels.json"
    bad.write_text(json.dumps({**bundle(), "count": 1}), encoding="utf-8")
    assert cli.main(["db", "parcels-load", str(bad)]) == 1
    assert "입력 거절" in capsys.readouterr().err
    # 제안 → 파일 → 반영
    sug = tmp_path / "links.json"
    assert cli.main(["db", "parcels-suggest", "--out", str(sug), "--effective-from", "2026-09-23"]) == 0
    o = capsys.readouterr()
    assert "연결 제안 4건" in o.out and "위치점 없는 물건 1" in o.err
    assert cli.main(["db", "parcels-suggest", "--out", str(sug)]) == 1, "덮어쓰지 않는다"
    capsys.readouterr()
    assert cli.main(["db", "parcels-suggest"]) == 0
    assert len(json.loads(capsys.readouterr().out)["links"]) == 4
    assert cli.main(["db", "parcels-link", str(sug)]) == 0
    assert "신규 4" in capsys.readouterr().out
    assert cli.main(["db", "parcels-link", str(sug), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "unchanged"
    assert cli.main(["db", "status"]) == 0
    st_out = capsys.readouterr().out
    assert "parcels 6" in st_out and "asset_components 4" in st_out
    assert cli.main(["db", "project"]) == 0
    assert "parcels=6" in capsys.readouterr().out
    assert cli.main(["db", "parcels-link", str(tmp_path / "none.json")]) == cli.USAGE_ERROR
    assert load_json(sug, "asset_components_input.schema.json")["kind"] == "asset_components"
