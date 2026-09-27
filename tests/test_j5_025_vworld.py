"""J5-025: VWorld 토지 자료 묶음 자동 인식과 필지 속성 (ADR-19).

시험: 묶음 인식 세 형태(바깥 ZIP / 자료별 ZIP·SHP 하나와 형제 / 폴더)와 비-VWorld 입력, 안쪽 ZIP 안전 추출(경로 탈출·중복 자료·d002 없음),
변경컬럼정보.csv 매핑, 토지특성·이용계획·소유 속성 읽기('지정되지않음'→null, 빈 값, 이름 목록 잘림, 소유 자료 없는 필지, 연령대·거주 구분 미수록),
CLI(inspect·convert·--no-land-attrs), 정본 반영(parcel_attributes 신규·변화 없음·갱신·공부면적 채움·구역 겹침), 파생본 parcels.geojson 의 속성, 상태 행 수, 백업.
가상자료만 쓴다 (fixture 의 VWorld 묶음은 시험 때 임시 폴더에 만든다).
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.backup import create_backup, restore_backup
from j5.db.parcels import load_bundle, parcels_bundle_from_db
from j5.db.projection import build_projection
from j5.db.store import Db, DbError
from j5.parcels.convert import ConvertOptions, convert, inspect_source
from j5.parcels.vworld import PRIVATE_FIELDS, VWorldError, detect_vworld, read_attrs, read_mapping
from j5.schemas_loader import schema_errors
from tests.fixtures import make_parcels as mk

FIX = Path(__file__).resolve().parent / "fixtures" / "parcels"
VW_BUNDLE = FIX / "synthetic_vworld.j5parcels.json"
SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
STUDY = "j5-synthetic-study"
P1, P11, P2, P3, P42, PM = ("9999900100100010000", "9999900100100010001", "9999900100100020000", "9999900100100030000", "9999900100100040002", "9999900100200010002")


@pytest.fixture(scope="module")
def vw_zip(tmp_path_factory) -> Path:
    return mk.write_vworld_set(tmp_path_factory.mktemp("vw"))


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    yield d
    d.close()


def vw_bundle() -> dict:
    return json.loads(VW_BUNDLE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 묶음 인식

def test_detect_outer_zip_extracts_four_datasets_with_mappings(vw_zip):
    vw = detect_vworld(vw_zip)
    try:
        assert vw is not None and sorted(vw.datasets) == ["cadastral", "land_feature", "land_ownership", "land_plan"]
        assert vw.cadastral.name.endswith("dt_d002_연속지적도형정보.zip") and vw.tmpdir is not None
        assert vw.mappings["cadastral"]["A4"] == "pnu" and vw.mappings["land_plan"]["A2"] == "pnu"   # 열 순서는 자료마다 다르다
        assert vw.mappings["land_ownership"]["A1"] == "agrde_se_code"
        tmp = Path(vw.tmpdir.name)
        assert tmp.exists()
    finally:
        vw.cleanup()
    assert not tmp.exists(), "cleanup 이 임시 폴더를 지운다"


def test_detect_single_dataset_zip_finds_siblings_and_folder_and_shp(vw_zip, tmp_path):
    with zipfile.ZipFile(vw_zip) as zf:
        zf.extractall(tmp_path / "flat")
    inner = sorted((tmp_path / "flat").glob("*dt_d002_*.zip"))[0]
    vw = detect_vworld(inner)
    assert vw is not None and vw.tmpdir is None and sorted(vw.datasets) == ["cadastral", "land_feature", "land_ownership", "land_plan"]
    vw = detect_vworld(tmp_path / "flat")
    assert vw is not None and len(vw.datasets) == 4 and vw.cadastral == inner
    # 자료별 ZIP 을 각자 폴더에 푼 형태 (VWorld 다운로드를 두 번 풀었을 때). .shp 하나를 주면 형제 폴더의 다른 자료도 찾는다
    for z in (tmp_path / "flat").glob("*.zip"):
        with zipfile.ZipFile(z) as zf:
            zf.extractall(tmp_path / "unpacked" / z.stem)
    shp = next((tmp_path / "unpacked").glob("*dt_d002_*/*.shp"))
    vw = detect_vworld(shp)
    assert vw is not None and vw.cadastral == shp and sorted(vw.datasets) == ["cadastral", "land_feature", "land_ownership", "land_plan"]
    assert read_mapping(shp)["A4"] == "pnu"
    # 한 폴더에 여러 자료의 CSV 가 있으면 같은 자료 코드의 CSV 를 고른다
    flat2 = tmp_path / "flat2"
    for sub in (tmp_path / "unpacked").iterdir():
        for f in sub.iterdir():
            shutil.copyfile(f, flat2.joinpath(f.name) if flat2.mkdir(exist_ok=True) is None else f)
    d154 = next(flat2.glob("*dt_d154_*.shp"))
    assert read_mapping(d154)["A2"] == "pnu" and read_mapping(next(flat2.glob("*dt_d002_*.shp")))["A4"] == "pnu"


def test_detect_returns_none_for_plain_shapefile_and_rejects_missing_cadastral(tmp_path, vw_zip):
    assert detect_vworld(FIX / "synthetic_shp" / "synthetic_parcels.shp") is None
    assert detect_vworld(tmp_path / "nope.zip") is None
    with zipfile.ZipFile(vw_zip) as zf, zipfile.ZipFile(tmp_path / "no_d002.zip", "w") as out:
        for i in zf.infolist():
            if "d002" not in i.filename:
                out.writestr(i, zf.read(i))
    with pytest.raises(VWorldError) as e:
        detect_vworld(tmp_path / "no_d002.zip")
    assert e.value.code == "cadastral_missing"
    # 자료별 ZIP 하나만 있고 d002 가 없어도 같은 오류
    with zipfile.ZipFile(tmp_path / "no_d002.zip") as zf:
        zf.extractall(tmp_path / "only")
    with pytest.raises(VWorldError) as e:
        detect_vworld(next((tmp_path / "only").glob("*dt_d194_*.zip")))
    assert e.value.code == "cadastral_missing"


def test_nested_zip_rejects_unsafe_paths_and_duplicate_datasets(tmp_path, vw_zip):
    with zipfile.ZipFile(vw_zip) as zf:
        members = {i.filename: zf.read(i) for i in zf.infolist()}
    d002 = next(k for k in members if "d002" in k)
    with zipfile.ZipFile(tmp_path / "unsafe.zip", "w") as out:
        for k, v in members.items():
            out.writestr("../" + k if k == d002 else k, v)
    with pytest.raises(VWorldError) as e:
        detect_vworld(tmp_path / "unsafe.zip")
    assert e.value.code == "zip_unsafe_path"
    with zipfile.ZipFile(tmp_path / "dup.zip", "w") as out:
        for k, v in members.items():
            out.writestr(k, v)
        out.writestr("again_" + d002, members[d002])
    with pytest.raises(VWorldError) as e:
        detect_vworld(tmp_path / "dup.zip")
    assert e.value.code == "duplicate_dataset"
    # 바깥 ZIP 에 자료별 ZIP 이 없으면 (일반 SHP ZIP) VWorld 묶음이 아니다 → None (기존 변환 경로)
    with zipfile.ZipFile(tmp_path / "plain.zip", "w") as out:
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            out.write(FIX / "synthetic_shp" / f"synthetic_parcels{ext}", f"synthetic_parcels{ext}")
    assert detect_vworld(tmp_path / "plain.zip") is None


# ---------------------------------------------------------------- 속성 읽기

def test_read_attrs_values_nulls_truncation_and_privacy(vw_zip):
    vw = detect_vworld(vw_zip)
    try:
        attrs, sources = read_attrs(vw)
    finally:
        vw.cleanup()
    assert sorted(attrs) == sorted([P1, P11, P2, P3, P42, PM])
    a1 = attrs[P1]
    assert a1["jimok_name"] == "대" and a1["registered_area_m2"] == 1770.5 and a1["official_land_price_krw_m2"] == 12340000 and a1["price_base_year"] == 2026 and a1["price_base_month"] == 1
    assert a1["use_zone_1"] == "일반상업지역" and a1["use_zone_2"] is None, "'지정되지않음' 은 null"
    assert a1["plan_zones"] == [{"code": "UQA01X", "name": "도시지역", "relation": "포함"}, {"code": "UQA220", "name": "일반상업지역", "relation": "포함"}, {"code": "UQQ300", "name": "지구단위계획구역(가상)", "relation": "포함"}]
    assert a1["plan_zones_truncated"] is False and a1["ownership_kind"] == "개인" and a1["ownership_kind_code"] == "01" and a1["ownership_changed_on"] == "2017-01-01" and a1["co_owner_count"] == 1
    a3 = attrs[P3]
    assert a3["official_land_price_krw_m2"] is None and a3["price_base_year"] is None, "빈 값은 null (0 이 아니다)"
    assert a3["ownership_kind"] is None and a3["ownership_kind_code"] is None and a3["ownership_changed_on"] is None, "소유 행이 비어 있으면 null"
    a42 = attrs[P42]
    assert a42["land_use_situation"] is None and a42["road_side"] is None and a42["terrain_height"] is None and a42["terrain_form"] is None
    assert a42["ownership_kind"] == "국유지" and a42["national_institution_code"] == "01"
    a2 = attrs[P2]
    assert a2["plan_zones_truncated"] is True and len(a2["plan_zones"]) == 5 and a2["plan_zones"][4] == {"code": "ZA0014", "name": None, "relation": "저촉"}, "이름 목록이 열 길이에 잘리면 코드만 남고 표시한다"
    assert "ownership_kind" not in attrs[PM] or attrs[PM].get("ownership_kind") is None
    assert all(k not in attrs[PM] for k in ("ownership_kind_code", "co_owner_count")), "소유 자료가 없는 필지에는 소유 필드 자체가 없다"
    assert [s["kind"] for s in sources] == ["land_feature", "land_plan", "land_ownership"] and sources[2]["rows_used"] == 5 and sources[2]["record_count"] == 5
    for s in sources:
        assert not (set(s["fields"]) & PRIVATE_FIELDS)
    assert not any(k in json.dumps(attrs) for k in PRIVATE_FIELDS)


def test_fixture_bundle_matches_generator_and_schema(vw_zip, tmp_path):
    out = tmp_path / "vw.j5parcels.json"
    mk.build_vworld_bundle(vw_zip, out)
    assert json.loads(out.read_text(encoding="utf-8")) == vw_bundle(), "fixture 는 생성기로 재현된다 (python tests/fixtures/make_parcels.py)"
    b = vw_bundle()
    assert schema_errors("parcels_bundle.schema.json", b) == []
    assert len(b["attrs_sources"]) == 3 and b["source"]["encoding"] == "utf-8" and "stats" not in b   # stats 는 파일에 쓰지 않는다
    assert all("attrs" in f["properties"] for f in b["features"])
    text = VW_BUNDLE.read_text(encoding="utf-8")
    assert "agrde" not in text and "resdnc" not in text
    # 기존 가상 번들(EPSG:5186 SHP)은 그대로다
    old = json.loads((FIX / "synthetic.j5parcels.json").read_text(encoding="utf-8"))
    assert "attrs_sources" not in old and not any("attrs" in f["properties"] for f in old["features"])


def test_convert_with_field_map_but_without_attrs(vw_zip):
    vw = detect_vworld(vw_zip)
    try:
        r = inspect_source(vw.cadastral, encoding="utf-8", field_map=vw.mappings["cadastral"])
        assert r["field_map_applied"] and r["pnu_field"] == "pnu" and r["jibun_field"] == "lnm_lndcgr_smbol" and r["crs"]["epsg"] == 4326
        b = convert(vw.cadastral, ConvertOptions(clip=mk.Clip.from_bbox("126.998,37.5698,127.001,37.5716"), geometry_version="2026-09-05", source_name="x", data_mode="synthetic",
                                                 encoding="utf-8", field_map=vw.mappings["cadastral"], emd_names={mk.EMD: "가상동"}))
    finally:
        vw.cleanup()
    assert b["count"] == 6 and "attrs_sources" not in b and b["stats"].get("attrs_attached", 0) == 0
    assert [f["properties"]["label"] for f in b["features"]] == [f["properties"]["label"] for f in vw_bundle()["features"]]


# ---------------------------------------------------------------- CLI

def test_cli_inspect_and_convert_outer_zip(vw_zip, tmp_path, capsys):
    assert cli.main(["parcels", "inspect", str(vw_zip), "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["vworld"]["datasets"] == ["cadastral", "land_feature", "land_ownership", "land_plan"] and r["field_map_applied"] and r["pnu_field"] == "pnu"
    assert cli.main(["parcels", "inspect", str(vw_zip)]) == 0
    assert "pnu" in capsys.readouterr().out
    out = tmp_path / "out.j5parcels.json"
    assert cli.main(["parcels", "convert", str(vw_zip), "--out", str(out), "--geometry-version", "2026-09-05", "--source-name", "가상 VWorld", "--bbox", "126.998,37.5698,127.001,37.5716",
                     "--emd-name", f"{mk.EMD}=가상동", "--synthetic", "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["count"] == 6 and j["stats"]["attrs_attached"] == 6 and [a["kind"] for a in j["attrs_sources"]] == ["land_feature", "land_plan", "land_ownership"]
    b = json.loads(out.read_text(encoding="utf-8"))
    assert schema_errors("parcels_bundle.schema.json", b) == [] and b["features"][0]["properties"]["attrs"] == vw_bundle()["features"][0]["properties"]["attrs"]
    out2 = tmp_path / "noattrs.j5parcels.json"
    assert cli.main(["parcels", "convert", str(vw_zip), "--out", str(out2), "--geometry-version", "2026-09-05", "--source-name", "가상 VWorld", "--bbox", "126.998,37.5698,127.001,37.5716",
                     "--synthetic", "--no-land-attrs"]) == 0
    text = capsys.readouterr().out
    assert "필지 속성" not in text and "attrs_sources" not in json.loads(out2.read_text(encoding="utf-8"))
    # 안쪽 ZIP 을 푼 임시 폴더는 남지 않는다
    assert not list(Path(tmp_path).glob("j5vworld-*")) and not list(Path("/tmp").glob("j5vworld-*"))


def test_cli_reports_vworld_errors(tmp_path, vw_zip, capsys):
    with zipfile.ZipFile(vw_zip) as zf, zipfile.ZipFile(tmp_path / "no_d002.zip", "w") as out:
        for i in zf.infolist():
            if "d002" not in i.filename:
                out.writestr(i, zf.read(i))
    assert cli.main(["parcels", "inspect", str(tmp_path / "no_d002.zip")]) == 1
    assert "cadastral_missing" in capsys.readouterr().err


# ---------------------------------------------------------------- 정본 반영

def test_schema_15_and_status_counts(db):
    st = db.status()
    assert st["db_schema_version"] == S.DB_SCHEMA_VERSION == 15 and st["counts"]["parcel_attributes"] == 0
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(parcel_attributes)")]
    assert "use_zone_1" in cols and "official_land_price_krw_m2" in cols and not any(c.startswith(("agrde", "resdnc")) for c in cols)
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_load_bundle_inserts_attributes_and_fills_registered_area(db):
    r = load_bundle(db, vw_bundle())
    assert r.outcome == "applied" and r.inserted == 6 and r.attrs_inserted == 6 and r.attrs_updated == 0 and r.dataset_version == 2
    assert "필지 속성 신규 6" in r.to_text() and r.to_dict()["attrs_inserted"] == 6
    row = db.conn.execute("SELECT a.*, p.registered_area_m2 AS p_area, p.registered_area_missing_reason AS p_reason FROM parcel_attributes a JOIN parcels p USING (parcel_id) WHERE p.pnu = ?", (P1,)).fetchone()
    assert row["use_zone_1"] == "일반상업지역" and row["official_land_price_krw_m2"] == 12340000 and row["as_of"] == "2026-09-05" and row["ownership_kind"] == "개인"
    assert row["p_area"] == 1770.5 and row["p_reason"] is None, "공부면적은 parcels 에도 채우고 결측 사유를 지운다"
    assert json.loads(row["plan_zones_json"])[1]["name"] == "일반상업지역" and len(json.loads(row["sources_json"])) == 3 and row["source_document_id"] == r.source_document_id
    doc = db.conn.execute("SELECT notes FROM source_documents WHERE document_id = ?", (r.source_document_id,)).fetchone()
    assert "토지특성공간정보" in doc["notes"]
    assert db.status()["counts"]["parcel_attributes"] == 6 and db.status()["ok"]
    # 다시 반영: 변화 없음, 버전 그대로
    r2 = load_bundle(db, vw_bundle())
    assert r2.outcome == "unchanged" and r2.unchanged == 6 and r2.attrs_unchanged == 6 and r2.dataset_version == 2


def test_load_bundle_updates_changed_attributes_same_as_of(db):
    load_bundle(db, vw_bundle())
    b = vw_bundle()
    f1 = next(f for f in b["features"] if f["id"] == P1)
    f1["properties"]["attrs"]["ownership_kind"] = "법인"
    f1["properties"]["attrs"]["ownership_kind_code"] = "06"
    f1["properties"]["attrs"]["registered_area_m2"] = 1771.0
    r = load_bundle(db, b)
    assert r.outcome == "applied" and r.inserted == 0 and r.unchanged == 6 and r.attrs_updated == 1 and r.attrs_unchanged == 5 and r.dataset_version == 3
    row = db.conn.execute("SELECT a.ownership_kind, p.registered_area_m2 FROM parcel_attributes a JOIN parcels p USING (parcel_id) WHERE p.pnu = ?", (P1,)).fetchone()
    assert row["ownership_kind"] == "법인" and row["registered_area_m2"] == 1771.0
    # 속성 없는 번들(기존 형식)을 같은 기준일에 다시 넣어도 속성은 지워지지 않는다
    plain = vw_bundle()
    for f in plain["features"]:
        f["properties"].pop("attrs", None)
    plain.pop("attrs_sources", None)
    r3 = load_bundle(db, plain)
    assert r3.outcome == "unchanged" and db.status()["counts"]["parcel_attributes"] == 6


def test_partial_bundle_keeps_fields_of_absent_datasets_and_null_area_clears_mirror(db):
    """리뷰 반영(PR #71): 토지특성만 든 번들(소유·이용계획 없음)은 그 묶음만 갱신하고 나머지는 기존 값·출처를 유지한다. 면적 null 은 parcels 미러도 null 로."""
    load_bundle(db, vw_bundle())
    part = vw_bundle()
    part["attrs_sources"] = [a for a in part["attrs_sources"] if a["kind"] == "land_feature"]
    part["attrs_sources"][0]["dbf_sha256"] = "3" * 64
    part["source"]["geometry_version"] = "2026-10-01"
    keep = ("jimok_name", "registered_area_m2", "official_land_price_krw_m2", "price_base_year", "price_base_month", "use_zone_1", "use_zone_2", "land_use_situation", "road_side", "terrain_height", "terrain_form")
    for f in part["features"]:
        f["properties"]["attrs"] = {k: f["properties"]["attrs"].get(k) for k in keep}
        f["properties"]["attrs"]["official_land_price_krw_m2"] = 55_000_000
    p1 = next(f for f in part["features"] if f["id"] == P1)
    p1["properties"]["attrs"]["registered_area_m2"] = None
    r = load_bundle(db, part)
    assert r.outcome == "applied" and r.updated == 6 and r.attrs_updated == 6
    row = db.conn.execute("SELECT a.*, p.registered_area_m2 AS p_area, p.registered_area_missing_reason AS p_reason FROM parcel_attributes a JOIN parcels p USING (parcel_id) WHERE p.pnu = ?", (P1,)).fetchone()
    assert row["official_land_price_krw_m2"] == 55_000_000 and row["as_of"] == "2026-10-01"
    assert row["ownership_kind"] == "개인" and row["ownership_changed_on"] == "2017-01-01" and json.loads(row["plan_zones_json"])[1]["name"] == "일반상업지역", "번들에 없는 자료의 값은 지우지 않는다"
    assert row["registered_area_m2"] is None and row["p_area"] is None and row["p_reason"] == "not_collected", "면적이 null 이면 parcels 미러도 null"
    srcs = json.loads(row["sources_json"])
    assert sorted(s_["kind"] for s_ in srcs) == ["land_feature", "land_ownership", "land_plan"] and next(s_ for s_ in srcs if s_["kind"] == "land_feature")["dbf_sha256"] == "3" * 64
    row2 = db.conn.execute("SELECT a.registered_area_m2, p.registered_area_m2 AS p_area FROM parcel_attributes a JOIN parcels p USING (parcel_id) WHERE p.pnu = ?", (P11,)).fetchone()
    assert row2["registered_area_m2"] == 980.0 and row2["p_area"] == 980.0
    # 같은 부분 번들을 다시 넣으면 변화 없음. 소유 자료만 든 번들(as_of 같음)로 소유가 바뀌면 그 묶음만 갱신
    assert load_bundle(db, part).outcome == "unchanged"
    own = vw_bundle()
    own["attrs_sources"] = [a for a in own["attrs_sources"] if a["kind"] == "land_ownership"]
    own["source"]["geometry_version"] = "2026-10-01"
    for f in own["features"]:
        f["properties"]["attrs"] = {"ownership_kind_code": "06", "ownership_kind": "법인", "co_owner_count": 1, "ownership_changed_on": "2026-09-30", "ownership_change_cause_code": "04", "national_institution_code": "ZZ"}
    r = load_bundle(db, own)
    assert r.attrs_updated == 6
    row = db.conn.execute("SELECT a.* FROM parcel_attributes a JOIN parcels p USING (parcel_id) WHERE p.pnu = ?", (P1,)).fetchone()
    assert row["ownership_kind"] == "법인" and row["official_land_price_krw_m2"] == 55_000_000 and json.loads(row["plan_zones_json"])[1]["name"] == "일반상업지역"
    # 파생본에는 필지마다 속성 기준일이 따로 실린다 (도형 기준일 2026-10-01 과 같지만 별도 필드)
    b = parcels_bundle_from_db(db, generated_at="2026-10-02T00:00:00Z")
    assert next(f for f in b["features"] if f["id"] == P1)["properties"]["attrs"]["as_of"] == "2026-10-01" and schema_errors("parcels_bundle.schema.json", b) == []
    # 파생본을 다시 반영하면(다른 정본으로 옮길 때) attrs.as_of 를 기준일로 쓴다
    b["source"]["geometry_version"] = "2026-10-01"
    d2 = Db.create(db.path.parent / "other.sqlite3", study_id=STUDY, data_mode="synthetic")
    try:
        d2.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
        r2 = load_bundle(d2, b)
        assert r2.attrs_inserted == 6 and d2.conn.execute("SELECT as_of FROM parcel_attributes").fetchone()[0] == "2026-10-01"
    finally:
        d2.close()


def test_load_bundle_overlapping_zone_download_is_unchanged_but_geometry_change_conflicts(db):
    load_bundle(db, vw_bundle())
    other = vw_bundle()
    other["source"]["name"] = "다른 구역 다운로드"
    other["source"]["shp_sha256"] = "1" * 64
    other["source"]["dbf_sha256"] = "2" * 64
    other["features"] = [f for f in other["features"] if f["id"] in (P1, P11)]
    other["count"] = 2
    r = load_bundle(db, other)
    assert r.outcome == "unchanged" and r.unchanged == 2 and r.attrs_unchanged == 2, "구역을 나눠 받은 경계 필지: 출처 표기만 다르면 변화 없음"
    other["features"][0]["properties"]["label"] = "1-9"
    with pytest.raises(DbError) as e:
        load_bundle(db, other)
    assert e.value.code == "parcel_conflict"


def test_projection_carries_attrs_and_sources(db, home):
    load_bundle(db, vw_bundle())
    b = parcels_bundle_from_db(db, generated_at="2026-09-27T00:00:00Z")
    assert schema_errors("parcels_bundle.schema.json", b) == []
    f1 = next(f for f in b["features"] if f["id"] == P1)
    assert f1["properties"]["attrs"] == vw_bundle()["features"][0]["properties"]["attrs"] | {"plan_zones_truncated": False, "as_of": "2026-09-05"}, "파생본 속성에는 기준일이 따로 실린다"
    assert [s["kind"] for s in b["attrs_sources"]] == ["land_feature", "land_ownership", "land_plan"]
    proj = build_projection(db, home, photos=False)
    assert proj.outcome == "published", proj.message
    with zipfile.ZipFile(home / proj.output_dir / proj.zip_name) as zf:
        pc = json.loads(zf.read("parcels.geojson"))
    assert sum(1 for f in pc["features"] if "attrs" in f["properties"]) == 6 and len(pc["attrs_sources"]) == 3
    assert "agrde" not in json.dumps(pc) and "resdnc" not in json.dumps(pc)


def test_backup_restore_keeps_attributes(db, home, tmp_path):
    load_bundle(db, vw_bundle())
    b = create_backup(db, home)
    assert b.outcome == "completed" and b.counts.get("parcel_attributes", 6) == 6, b.to_text()
    rr = restore_backup(home / b.backup_dir, tmp_path / "restored")
    assert rr.outcome == "completed", rr.to_text()
    with Db.open(tmp_path / "restored" / "db" / "j5.sqlite3") as rdb:
        assert rdb.status()["counts"]["parcel_attributes"] == 6 and rdb.status()["ok"]
