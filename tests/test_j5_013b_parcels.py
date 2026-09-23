"""J5-013B-1: 필지 경계·지번 번들 (ADR-13). 연속지적도 SHP → WGS84 GeoJSON 번들 변환 도구.

시험: 한국 TM 좌표계 역변환·데이텀 변환의 pyproj 기준값 대조(참조값은 pyproj 3.7.2 로 미리 계산해 상수로 둠, 의존성 아님),
.prj 판독과 --crs 지정, SHP/DBF 읽기(ZIP 포함, 안전하지 않은 경로 거절), 범위 자르기, PNU·지번 정규화, 구멍·다중 조각,
같은 PNU 합침, 인코딩, 스키마·덮어쓰기 금지, 가상 fixture 재현성과 web/data 동일성, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j5 import cli
from j5.parcels.convert import Clip, ConvertError, ConvertOptions, convert, inspect_source, parcel_props, parse_jibun, rings_to_polygons, write_bundle
from j5.parcels.crs import KNOWN, CrsError, Transformer, forward_tm, parse_crs_arg, parse_prj
from j5.parcels.shp import ShapeError, guess_encoding, iter_dbf_records, iter_shapes, open_source, read_dbf_fields
from j5.schemas_loader import schema_errors
from tests.conftest import zip_dir
from tests.fixtures import make_parcels as mk

FIX = Path(__file__).resolve().parent / "fixtures" / "parcels"
SHP = FIX / "synthetic_shp" / "synthetic_parcels.shp"
BUNDLE = FIX / "synthetic.j5parcels.json"
WEB_BUNDLE = Path(__file__).resolve().parents[1] / "web" / "data" / "parcels.synthetic.j5parcels.json"

# pyproj 3.7.2 (PROJ 9) 로 계산한 기준값: EPSG → [(x, y, lon, lat)] (x·y 는 m, 소수 4자리; lon·lat 는 도, 소수 9자리)
PYPROJ_REF = {
    5186: [(199876.3204, 552295.4608, 126.9986, 37.5702), (200000.0, 552273.2622, 127.0, 37.57), (195584.6305, 555604.0969, 126.95, 37.6), (204418.3226, 550054.6738, 127.05, 37.55), (199116.6307, 552828.2512, 126.99, 37.575)],
    5181: [(199876.3204, 452295.4608, 126.9986, 37.5702), (200000.0, 452273.2622, 127.0, 37.57), (195584.6305, 455604.0969, 126.95, 37.6), (204418.3226, 450054.6738, 127.05, 37.55), (199116.6307, 452828.2512, 126.99, 37.575)],
    5179: [(955722.5939, 1952432.6716, 126.9986, 37.5702), (955846.1073, 1952409.8238, 127.0, 37.57), (951450.1726, 1955762.8602, 126.95, 37.6), (960250.8977, 1950168.6108, 127.05, 37.55), (954966.0343, 1952969.2955, 126.99, 37.575)],
    5185: [(376570.7853, 554173.6174, 126.9986, 37.5702), (376694.9572, 554154.0491, 127.0, 37.57), (372207.9799, 557391.3177, 126.95, 37.6), (381161.2566, 552029.1607, 127.05, 37.55), (375799.6308, 554690.3117, 126.99, 37.575)],
    5187: [(23181.8164, 554178.8851, 126.9986, 37.5702), (23305.0428, 554154.0491, 127.0, 37.57), (18959.8938, 557579.5022, 126.95, 37.6), (27676.7861, 551841.0636, 127.05, 37.55), (22433.3522, 554727.9398, 126.99, 37.575)],
    5174: [(199806.7207, 451990.2552, 126.998600032, 37.570199958), (199930.4009, 451968.0544, 127.000000032, 37.569999958), (195515.0491, 455298.9879, 126.950000032, 37.599999958), (204348.7237, 449749.3763, 127.050000032, 37.549999958), (199047.0334, 452523.0623, 126.990000032, 37.574999958)],
    5175: [(199806.7207, 501990.2552, 126.998600032, 37.570199958), (199930.4009, 501968.0544, 127.000000032, 37.569999958), (195515.0491, 505298.9879, 126.950000032, 37.599999958), (204348.7237, 499749.3763, 127.050000032, 37.549999958), (199047.0334, 502523.0623, 126.990000032, 37.574999958)],
    5176: [(23126.7525, 453874.8872, 126.998600032, 37.570199958), (23249.9796, 453850.0492, 127.000000032, 37.569999958), (18904.8442, 457275.5961, 126.950000032, 37.599999958), (27621.7256, 451536.9792, 127.050000032, 37.549999958), (22378.29, 454423.9578, 126.990000032, 37.574999958)],
}
NOW = datetime(2026, 9, 23, 9, 0, 0, tzinfo=timezone.utc)


def _opts(**kw) -> ConvertOptions:
    base = dict(clip=Clip.from_bbox("126.998,37.5698,127.001,37.5716"), geometry_version="2026-09-01", source_name="가상 연속지적도 (synthetic)",
                license="가상자료 (이용허락 해당 없음)", emd_names={mk.EMD: "가상동"}, data_mode="synthetic", now=NOW)
    base.update(kw)
    return ConvertOptions(**base)


# ---------------------------------------------------------------- 좌표계

def test_tm_inverse_matches_pyproj_reference_within_1mm():
    for epsg, rows in PYPROJ_REF.items():
        t = Transformer(KNOWN[epsg])
        for x, y, lon, lat in rows:
            lo, la = t.to_wgs84(x, y)
            assert abs(lo - lon) * 111320 * 0.79 < 0.001 and abs(la - lat) * 111320 < 0.001, (epsg, x, y, lo, la)


def test_tm_forward_roundtrip_and_origin():
    for epsg in (5186, 5181, 5179, 5185, 5187):
        crs = KNOWN[epsg]
        t = Transformer(crs)
        # 원점: (FE, FN) → (lon0, lat0)
        lon, lat = t.to_wgs84(crs.fe, crs.fn)
        assert abs(lon - crs.lon0) < 1e-10 and abs(lat - crs.lat0) < 1e-10
        for lo0, la0 in ((126.9986, 37.5702), (127.3, 36.2), (126.5, 38.5)):
            x, y = forward_tm(crs, lo0, la0)
            lo, la = t.to_wgs84(x, y)
            assert abs(lo - lo0) < 1e-11 and abs(la - la0) < 1e-11


def test_korean1985_datum_shift_matches_pyproj():
    t = Transformer(KNOWN[5174])
    lo, la = t.to_wgs84(199806.7207, 451990.2552)
    assert abs(lo - 126.998600032) * 111320 * 0.79 < 0.001 and abs(la - 37.570199958) * 111320 < 0.001
    # 같은 평면좌표를 GRS80 원점대(5181)로 읽으면 수백 m 차이가 난다: 좌표계를 잘못 고르면 지도가 어긋난다는 뜻
    lo2, la2 = Transformer(KNOWN[5181]).to_wgs84(199806.7207, 451990.2552)
    assert abs(la2 - la) * 111320 > 100


def test_parse_prj_matches_known_epsg_and_rejects_unknown():
    assert parse_prj(mk.PRJ_5186).epsg == 5186
    bessel = mk.PRJ_5186.replace("GRS_1980\",6378137.0,298.257222101", "Bessel_1841\",6377397.155,299.1528128").replace("False_Northing\",600000.0", "False_Northing\",500000.0").replace("Central_Meridian\",127.0", "Central_Meridian\",127.002890277778")
    assert parse_prj(bessel).epsg == 5174
    geog = 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
    assert parse_prj(geog).geographic and parse_prj(geog).epsg == 4326
    with pytest.raises(CrsError) as e:
        parse_prj(mk.PRJ_5186.replace("Central_Meridian\",127.0", "Central_Meridian\",126.0"))
    assert e.value.code == "prj_unmatched"
    with pytest.raises(CrsError):
        parse_prj(mk.PRJ_5186.replace("Transverse_Mercator", "Lambert_Conformal_Conic"))
    with pytest.raises(CrsError):
        parse_prj("")
    assert parse_crs_arg("EPSG:5186").epsg == 5186 and parse_crs_arg("5174").epsg == 5174
    with pytest.raises(CrsError):
        parse_crs_arg("EPSG:3857")


# ---------------------------------------------------------------- SHP/DBF

def test_shp_and_dbf_reader_on_fixture():
    src = open_source(SHP)
    shapes = list(iter_shapes(src.shp))
    assert len(shapes) == 6 and shapes[0][0] == 1
    assert len(shapes[4][1]) == 2, "구멍 있는 필지는 링 2개"
    assert len(shapes[5][1]) == 2, "두 조각 필지는 링 2개"
    fields, n, _, _ = read_dbf_fields(src.dbf)
    assert [f.name for f in fields] == ["PNU", "JIBUN", "BCHK", "SGG_OID", "COL_ADM_SE"] and n == 6
    assert guess_encoding(src, None) == "cp949"
    rows = list(iter_dbf_records(src.dbf, "cp949"))
    assert rows[0]["PNU"] == mk.PARCELS[0][0] and rows[0]["JIBUN"] == "1대" and rows[0]["SGG_OID"] == 1
    assert rows[5]["JIBUN"] == "산1-2임"
    with pytest.raises(ShapeError) as e:
        list(iter_dbf_records(src.dbf, "ascii"))
    assert e.value.code == "dbf_decode"


def test_zip_input_and_unsafe_zip_rejected(tmp_path):
    z = zip_dir(SHP.parent, tmp_path / "parcels.zip")
    src = open_source(z)
    assert src.name == "synthetic_parcels" and src.members[".dbf"].endswith(".dbf")
    b = convert(z, _opts())
    assert b["count"] == 6 and b["source"]["file"] == "synthetic_parcels.shp"
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../escape.shp", b"x")
        zf.writestr("../escape.dbf", b"x")
    with pytest.raises(ConvertError) as e:
        convert(bad, _opts())
    assert e.value.code == "zip_unsafe_path"
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "no shp")
    with pytest.raises(ConvertError) as e:
        convert(empty, _opts())
    assert e.value.code == "shp_missing"


def test_multiple_layers_need_layer_option(tmp_path):
    d = tmp_path / "two"
    d.mkdir()
    for name in ("a", "b"):
        for ext in (".shp", ".dbf", ".prj", ".cpg"):
            shutil.copyfile(SHP.with_suffix(ext), d / f"{name}{ext}")
    z = zip_dir(d, tmp_path / "two.zip")
    with pytest.raises(ConvertError) as e:
        convert(z, _opts())
    assert e.value.code == "multiple_shp"
    assert convert(z, _opts(layer="b"))["count"] == 6


def test_missing_prj_requires_crs_argument(tmp_path):
    d = tmp_path / "noprj"
    d.mkdir()
    for ext in (".shp", ".dbf"):
        shutil.copyfile(SHP.with_suffix(ext), d / f"p{ext}")
    with pytest.raises(ConvertError) as e:
        convert(d / "p.shp", _opts())
    assert e.value.code == "crs_unknown"
    b = convert(d / "p.shp", _opts(crs_arg="EPSG:5186"))
    assert b["source"]["crs"]["detected_from"] == "argument" and b["count"] == 6
    assert b["source"]["encoding"] == "cp949", ".cpg 가 없으면 .dbf 언어 구동기 바이트(한국어)로"
    # 잘못된 좌표계를 지정하면 범위 밖으로 나가 0개가 된다 (조용히 틀린 위치를 만들지 않는다: 사용자가 inspect 로 bbox 를 대조한다)
    b2 = convert(d / "p.shp", _opts(crs_arg="EPSG:5181"))
    assert b2["count"] == 0 and b2["bbox"] is None


# ---------------------------------------------------------------- 정규화·도형

def test_pnu_jibun_normalization():
    assert parse_jibun("123-4대") == {"mountain": False, "bon": 123, "bu": 4, "jimok": "대"}
    assert parse_jibun("산12임") == {"mountain": True, "bon": 12, "bu": 0, "jimok": "임"}
    assert parse_jibun(" 7 ") == {"mountain": False, "bon": 7, "bu": 0, "jimok": None}
    assert parse_jibun("") == {"mountain": None, "bon": None, "bu": None, "jimok": None}
    p = parcel_props("1111017500101230004", "123-4대", {"1111017500": "종로5가"})
    assert p["label"] == "123-4" and p["emd_code"] == "1111017500" and p["emd_name"] == "종로5가" and p["jimok"] == "대" and not p["jibun_mismatch"]
    assert parcel_props("1111017500200120000", "산12임", {})["label"] == "산12"
    assert parcel_props("1111017500100050000", "5대", {})["label"] == "5"
    m = parcel_props("1111017500101230004", "123-5대", {})
    assert m["jibun_mismatch"] and m["label"] == "123-4", "PNU 우선"


def test_rings_orientation_holes_and_multipart():
    cw = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]           # 시계방향 = 외곽
    hole_ccw = [(2, 2), (8, 2), (8, 8), (2, 8), (2, 2)]        # 반시계 = 구멍
    cw2 = [(20, 0), (20, 5), (25, 5), (25, 0), (20, 0)]
    polys = rings_to_polygons([cw, hole_ccw, cw2])
    assert len(polys) == 2 and len(polys[0]) == 2 and len(polys[1]) == 1
    from j5.parcels.convert import polygons_area, ring_signed_area
    assert ring_signed_area(polys[0][0]) > 0 and ring_signed_area(polys[0][1]) < 0, "GeoJSON 방향으로 뒤집힘"
    assert polygons_area(polys) == 100 - 36 + 25
    assert rings_to_polygons([[(0, 0), (1, 1)]]) == [], "점 2개는 버림"


def test_convert_fixture_props_and_clip(tmp_path):
    b = convert(SHP, _opts())
    assert b["count"] == 6 and b["stats"]["outside"] == 0
    by = {f["id"]: f for f in b["features"]}
    hole = by[mk.PARCELS[4][0]]
    assert hole["geometry"]["type"] == "Polygon" and len(hole["geometry"]["coordinates"]) == 2
    same_rect = by[mk.PARCELS[3][0]]  # 3번 필지는 4-2 와 같은 크기의 사각형 (구멍 없음)
    assert 0 < hole["properties"]["area_m2_geom"] < same_rect["properties"]["area_m2_geom"], "구멍 면적을 뺀 도형면적"
    multi = by[mk.PARCELS[5][0]]
    assert multi["geometry"]["type"] == "MultiPolygon" and len(multi["geometry"]["coordinates"]) == 2
    assert multi["properties"]["label"] == "산1-2" and multi["properties"]["mountain"] and multi["properties"]["jimok"] == "임"
    assert by[mk.PARCELS[0][0]]["properties"]["emd_name"] == "가상동"
    # 좌표는 원래 값으로 돌아온다 (5186 왕복, 7자리 반올림)
    assert by[mk.PARCELS[0][0]]["geometry"]["coordinates"][0][0] == [126.99855, 37.57005]
    # 범위: 서쪽 두 필지만 걸치는 bbox
    narrow = convert(SHP, _opts(clip=Clip.from_bbox("126.998,37.5698,126.9990,37.5716")))
    assert narrow["count"] == 2 and narrow["stats"]["outside"] == 4
    # 중심·반경
    c = Clip.from_center("126.9995,37.5706", 30)
    assert c.center == (126.9995, 37.5706) and c.radius_m == 30
    around = convert(SHP, _opts(clip=c))
    assert 1 <= around["count"] < 6 and around["clip"]["radius_m"] == 30
    with pytest.raises(ConvertError):
        Clip.from_bbox("127,37.6,126.9,37.5")
    with pytest.raises(ConvertError):
        Clip.from_center("x,y", 10)


def test_duplicate_pnu_merged_and_bad_pnu_skipped(tmp_path):
    recs = mk.synthetic_records()
    recs.append((recs[0][0], {**recs[0][1], "SGG_OID": 99}))            # 같은 PNU 두 번 → 합침
    recs.append((recs[1][0], {**recs[1][1], "PNU": "12345"}))            # PNU 형식 오류 → 건너뜀
    base = tmp_path / "dup" / "dup"
    mk.write_shapefile(base, recs, mk.FIELDS, mk.PRJ_5186)
    b = convert(base.with_suffix(".shp"), _opts())
    assert b["count"] == 6 and b["stats"]["merged_duplicates"] == 1 and b["stats"]["bad_pnu"] == 1
    first = next(f for f in b["features"] if f["id"] == mk.PARCELS[0][0])
    assert first["geometry"]["type"] == "MultiPolygon" and len(first["geometry"]["coordinates"]) == 2
    assert any("PNU 가 19자리" in w for w in b["warnings"])
    with pytest.raises(ConvertError) as e:
        convert(base.with_suffix(".shp"), _opts(max_features=3))
    assert e.value.code == "too_many_features"


def test_encoding_override_and_utf8_cpg(tmp_path):
    base = tmp_path / "u8" / "u8"
    mk.write_shapefile(base, mk.synthetic_records(), mk.FIELDS, mk.PRJ_5186, encoding="utf-8")
    base.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    b = convert(base.with_suffix(".shp"), _opts())
    assert b["source"]["encoding"] == "utf-8" and b["features"][5]["properties"]["jimok"] == "임"
    base.with_suffix(".cpg").unlink()
    with pytest.raises(ConvertError) as e:
        convert(base.with_suffix(".shp"), _opts())
    assert e.value.code == "dbf_decode"
    assert convert(base.with_suffix(".shp"), _opts(encoding="utf-8"))["count"] == 6


def test_shp_dbf_record_count_mismatch_rejected(tmp_path):
    d = tmp_path / "mm"
    d.mkdir()
    for ext in (".shp", ".prj", ".cpg"):
        shutil.copyfile(SHP.with_suffix(ext), d / f"m{ext}")
    recs = mk.synthetic_records()[:5]
    mk.write_shapefile(tmp_path / "five" / "five", recs, mk.FIELDS, mk.PRJ_5186)
    shutil.copyfile(tmp_path / "five" / "five.dbf", d / "m.dbf")
    with pytest.raises(ConvertError) as e:
        convert(d / "m.shp", _opts())
    assert e.value.code == "count_mismatch"


def test_options_validation():
    for bad in ({"geometry_version": "2026-9-1"}, {"geometry_version": "2026-02-30"}, {"source_name": " "}, {"data_mode": "private_real"}):
        with pytest.raises(ConvertError):
            convert(SHP, _opts(**bad))
    with pytest.raises(ConvertError) as e:
        convert(SHP, _opts(pnu_field="NOPE"))
    assert e.value.code == "field_missing"


def test_write_bundle_schema_and_no_overwrite(tmp_path):
    b = convert(SHP, _opts())
    assert schema_errors("parcels_bundle.schema.json", {k: v for k, v in b.items() if k != "stats"}) == []
    out = tmp_path / "x.j5parcels.json"
    w = write_bundle(b, out)
    assert out.is_file() and w["bytes"] == out.stat().st_size and len(w["sha256"]) == 64
    back = json.loads(out.read_text(encoding="utf-8"))
    assert "stats" not in back and back["count"] == 6 and back["source"]["license"].startswith("가상")
    with pytest.raises(ConvertError) as e:
        write_bundle(b, out)
    assert e.value.code == "exists"
    with pytest.raises(ConvertError):
        write_bundle(b, tmp_path / "x.json")
    bad = dict(b)
    bad["features"] = [{"type": "Feature", "id": "1"}]
    with pytest.raises(ConvertError):
        write_bundle(bad, tmp_path / "y.j5parcels.json")


def test_fixture_is_reproducible_and_web_copy_identical(tmp_path):
    b = convert(SHP, _opts())
    out = tmp_path / "re.j5parcels.json"
    write_bundle(b, out)
    assert out.read_bytes() == BUNDLE.read_bytes(), "python tests/fixtures/make_parcels.py 로 다시 만든다"
    assert WEB_BUNDLE.read_bytes() == BUNDLE.read_bytes(), "web/data 의 가상 필지는 tests/fixtures 와 같아야 한다"
    doc = json.loads(BUNDLE.read_text(encoding="utf-8"))
    assert schema_errors("parcels_bundle.schema.json", doc) == [] and doc["data_mode"] == "synthetic"
    # 가상 시드 물건이 가상 필지 안에 있어야 폰 지도 시험이 성립한다 (물건 1·2·4·5 → 필지 1·1-1·2·3)
    seed = json.loads((Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json").read_text(encoding="utf-8"))
    by = {f["properties"]["label"]: f["properties"]["bbox"] for f in doc["features"]}
    for label, idx in (("1", 0), ("1-1", 1), ("2", 3), ("3", 4)):
        lon, lat = seed[idx]["location_point"]
        x0, y0, x1, y1 = by[label]
        assert x0 < lon < x1 and y0 < lat < y1, (label, seed[idx]["label"])


def test_inspect_reports_crs_fields_and_samples(tmp_path):
    r = inspect_source(SHP)
    assert r["record_count"] == 6 and r["crs"]["epsg"] == 5186 and r["pnu_field"] == "PNU" and r["jibun_field"] == "JIBUN"
    assert r["bbox_wgs84"][0] == pytest.approx(126.99855, abs=1e-6) and len(r["samples"]) == 3
    d = tmp_path / "noprj"
    d.mkdir()
    for ext in (".shp", ".dbf"):
        shutil.copyfile(SHP.with_suffix(ext), d / f"p{ext}")
    r2 = inspect_source(d / "p.shp")
    assert r2["crs"] is None and "crs_unknown" in r2["crs_error"] and r2["bbox_wgs84"] is None
    assert inspect_source(d / "p.shp", crs_arg="EPSG:5186")["crs"]["detected_from"] == "argument"


def test_cli_parcels(tmp_path, capsys):
    assert cli.main(["parcels", "inspect", str(SHP)]) == 0
    out = capsys.readouterr().out
    assert "EPSG:5186" in out and "PNU 필드: PNU" in out
    assert cli.main(["parcels", "inspect", str(SHP), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["record_count"] == 6
    dest = tmp_path / "out" / "jongno.j5parcels.json"
    rc = cli.main(["parcels", "convert", str(SHP), "--out", str(dest), "--geometry-version", "2026-09-01", "--source-name", "가상 연속지적도 (synthetic)",
                   "--bbox", "126.998,37.5698,127.001,37.5716", "--emd-name", f"{mk.EMD}=가상동", "--synthetic", "--license", "가상자료 (이용허락 해당 없음)"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "필지 번들: 6개" in out and dest.is_file()
    assert json.loads(dest.read_text(encoding="utf-8")) == json.loads(BUNDLE.read_text(encoding="utf-8")) | {"generated_at": json.loads(dest.read_text(encoding="utf-8"))["generated_at"]}
    # 덮어쓰기 금지, 범위 누락, bbox·center 동시 지정, 잘못된 emd-name
    assert cli.main(["parcels", "convert", str(SHP), "--out", str(dest), "--geometry-version", "2026-09-01", "--source-name", "x", "--bbox", "126.998,37.5698,127.001,37.5716"]) == 1
    assert "exists" in capsys.readouterr().err
    assert cli.main(["parcels", "convert", str(SHP), "--out", str(tmp_path / "b.j5parcels.json"), "--geometry-version", "2026-09-01", "--source-name", "x"]) == cli.USAGE_ERROR
    assert cli.main(["parcels", "convert", str(SHP), "--out", str(tmp_path / "b.j5parcels.json"), "--geometry-version", "2026-09-01", "--source-name", "x",
                     "--bbox", "126.998,37.5698,127.001,37.5716", "--center", "127,37.57", "--radius-m", "50"]) == cli.USAGE_ERROR
    assert cli.main(["parcels", "convert", str(SHP), "--out", str(tmp_path / "b.j5parcels.json"), "--geometry-version", "2026-09-01", "--source-name", "x",
                     "--bbox", "126.998,37.5698,127.001,37.5716", "--emd-name", "bad"]) == cli.USAGE_ERROR
    rc = cli.main(["parcels", "convert", str(SHP), "--out", str(tmp_path / "c.j5parcels.json"), "--geometry-version", "2026-09-01", "--source-name", "x",
                   "--center", "126.9995,37.5706", "--radius-m", "60", "--json"])
    assert rc == 0
    j = json.loads(capsys.readouterr().out)
    assert j["clip"]["radius_m"] == 60 and j["written"]["path"].endswith("c.j5parcels.json") and j["source"]["license"] is None
    assert cli.main(["parcels", "inspect", str(tmp_path / "nope.shp")]) == 1
