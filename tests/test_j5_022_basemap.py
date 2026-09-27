"""J5-022 배경 도형 번들 (ADR-16): 폴리라인 읽기, 층별 변환, 범위 자르기, 도로명 필드, 이름·주소 미포함, 스키마·덮어쓰기 금지,
가상 fixture 재현성과 web/data 동일성, CLI. 가상자료만 쓴다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from j5 import cli
from j5.parcels.basemap import BasemapOptions, LayerInput, MAX_FEATURES, convert_basemap, write_basemap
from j5.parcels.convert import Clip, ConvertError
from j5.parcels.shp import POLYGON_TYPES, POLYLINE_TYPES, ShapeError, iter_shapes
from j5.schemas_loader import schema_errors
from tests.fixtures import make_basemap as mb
from tests.fixtures import make_parcels as mk

FIX = Path(__file__).resolve().parent / "fixtures" / "basemap"
SHP = FIX / "synthetic_shp"
BUNDLE = FIX / "synthetic.j5basemap.json"
WEB_BUNDLE = Path(__file__).resolve().parents[1] / "web" / "data" / "basemap.synthetic.j5basemap.json"
CLIP = Clip.from_bbox("126.998,37.5698,127.001,37.5716")


def _opts(inputs, **kw):
    base = dict(clip=CLIP, geometry_version="2026-09-01", source_name="가상 배경 도형 (synthetic)", inputs=inputs, license="가상자료 (이용허락 해당 없음)", data_mode="synthetic", now=mb.GENERATED_AT)
    base.update(kw)
    return BasemapOptions(**base)


def test_iter_shapes_reads_polylines_only_when_asked():
    data = (SHP / "roads.shp").read_bytes()
    recs = list(iter_shapes(data, POLYLINE_TYPES))
    assert [r[0] for r in recs] == [1, 2, 3]
    assert len(recs[2][1]) == 2, "세 번째 도로는 두 파트"
    with pytest.raises(ShapeError) as e:
        list(iter_shapes(data))
    assert e.value.code == "shp_not_polygon"
    with pytest.raises(ShapeError) as e:
        list(iter_shapes((SHP / "buildings.shp").read_bytes(), POLYLINE_TYPES))
    assert e.value.code == "shp_not_polyline"
    assert POLYGON_TYPES == {5, 15, 25}


def test_convert_three_layers_clips_and_keeps_only_geometry_and_road_name():
    b = convert_basemap(_opts([LayerInput("building", SHP / "buildings.shp"), LayerInput("road_area", SHP / "road_areas.shp"), LayerInput("road", SHP / "roads.shp")]))
    assert b["counts"] == {"building": 6, "road_area": 2, "road": 3} and b["count"] == 11
    assert b["stats"]["building"]["outside"] == 1, "범위 밖 건물 하나는 잘려 나간다"
    assert [s["layer"] for s in b["sources"]] == ["building", "road_area", "road"]
    assert b["sources"][2]["name_field"] == "RN" and b["sources"][0]["name_field"] is None
    kinds = {f["properties"]["layer"]: set() for f in b["features"]}
    for f in b["features"]:
        kinds[f["properties"]["layer"]].add(f["geometry"]["type"])
        assert set(f["properties"]) == {"layer", "name", "bbox"}
        assert f["id"].startswith(f["properties"]["layer"] + ":")
    assert kinds["building"] == {"Polygon"} and kinds["road_area"] == {"Polygon"} and kinds["road"] == {"LineString", "MultiLineString"}
    names = sorted(f["properties"]["name"] for f in b["features"] if f["properties"]["layer"] == "road")
    assert names == ["가상1길", "가상2길", "가상로"]
    assert all(f["properties"]["name"] is None for f in b["features"] if f["properties"]["layer"] != "road")
    text = json.dumps({k: v for k, v in b.items() if k != "stats"}, ensure_ascii=False)
    assert "가상건물" not in text, "건물명은 번들에 넣지 않는다"
    assert "99999000" not in text, "건물 관리번호는 번들에 넣지 않는다"
    assert not schema_errors("basemap_bundle.schema.json", {k: v for k, v in b.items() if k != "stats"})
    assert b["bbox"][0] >= 126.998 - 1e-6 and b["bbox"][2] <= 127.001 + 1e-3


def test_road_name_field_override_and_missing():
    b = convert_basemap(_opts([LayerInput("road", SHP / "roads.shp", name_field="RDS_MAN_NO")]))
    assert b["sources"][0]["name_field"] == "RDS_MAN_NO" and b["features"][0]["properties"]["name"] == "RD000001"
    with pytest.raises(ConvertError) as e:
        convert_basemap(_opts([LayerInput("road", SHP / "roads.shp", name_field="NOPE")]))
    assert e.value.code == "field_missing"


def test_polyline_without_name_candidates_warns(tmp_path):
    base = tmp_path / "r" / "r"
    mk.write_shapefile(base, [([[(200000.0, 550000.0), (200050.0, 550050.0)]], {"ID": "1"})], [("ID", "C", 4)], mk.PRJ_5186, shape_type=3)
    b = convert_basemap(_opts([LayerInput("road", base.with_suffix(".shp"))], clip=Clip.from_bbox("126,37,128,38")))
    assert b["counts"]["road"] == 1 and b["features"][0]["properties"]["name"] is None
    assert any("도로명 필드를 찾지 못해" in w for w in b["warnings"])


def test_wrong_geometry_kind_per_layer_is_rejected():
    with pytest.raises(ConvertError) as e:
        convert_basemap(_opts([LayerInput("building", SHP / "roads.shp")]))
    assert e.value.code == "shp_not_polygon"
    with pytest.raises(ConvertError) as e:
        convert_basemap(_opts([LayerInput("road", SHP / "buildings.shp")]))
    assert e.value.code == "shp_not_polyline"


@pytest.mark.parametrize("inputs,code", [
    ([], "no_inputs"),
    ([LayerInput("building", SHP / "buildings.shp"), LayerInput("building", SHP / "buildings.shp")], "duplicate_layer"),
    ([LayerInput("water", SHP / "buildings.shp")], "bad_layer"),
])
def test_input_validation(inputs, code):
    with pytest.raises(ConvertError) as e:
        convert_basemap(_opts(inputs))
    assert e.value.code == code


def test_max_features_and_bad_version():
    with pytest.raises(ConvertError) as e:
        convert_basemap(_opts([LayerInput("building", SHP / "buildings.shp")], max_features=3))
    assert e.value.code == "too_many_features"
    with pytest.raises(ConvertError) as e:
        convert_basemap(_opts([LayerInput("building", SHP / "buildings.shp")], max_features=MAX_FEATURES + 1))
    assert e.value.code == "bad_max_features"
    with pytest.raises(ConvertError) as e:
        convert_basemap(_opts([LayerInput("building", SHP / "buildings.shp")], geometry_version="2026-13-01"))
    assert e.value.code == "bad_geometry_version"


def test_write_verifies_and_never_overwrites(tmp_path):
    b = convert_basemap(_opts([LayerInput("building", SHP / "buildings.shp")]))
    out = tmp_path / "a.j5basemap.json"
    w = write_basemap(b, out)
    back = json.loads(out.read_text(encoding="utf-8"))
    assert back["count"] == 6 and "stats" not in back and w["bytes"] == out.stat().st_size
    with pytest.raises(ConvertError) as e:
        write_basemap(b, out)
    assert e.value.code == "exists"
    with pytest.raises(ConvertError) as e:
        write_basemap(b, tmp_path / "a.json")
    assert e.value.code == "bad_out_name"
    assert not list(tmp_path.glob(".*tmp*")), "임시 파일이 남지 않는다"


def test_fixture_is_reproducible_and_web_copy_matches(tmp_path):
    b = convert_basemap(_opts([LayerInput("building", SHP / "buildings.shp"), LayerInput("road_area", SHP / "road_areas.shp"), LayerInput("road", SHP / "roads.shp")]))
    out = tmp_path / "synthetic.j5basemap.json"
    write_basemap(b, out)
    assert out.read_bytes() == BUNDLE.read_bytes(), "python tests/fixtures/make_basemap.py 로 다시 만든다"
    assert WEB_BUNDLE.read_bytes() == BUNDLE.read_bytes(), "web/data 의 가상 배경은 tests/fixtures 와 같아야 한다"
    doc = json.loads(BUNDLE.read_text(encoding="utf-8"))
    assert doc["data_mode"] == "synthetic" and doc["counts"] == {"building": 6, "road_area": 2, "road": 3}


def test_cli_convert(tmp_path, capsys):
    out = tmp_path / "bg.j5basemap.json"
    rc = cli.main(["basemap", "convert", "--out", str(out), "--geometry-version", "2026-09-01", "--source-name", "가상", "--synthetic",
                   "--buildings", str(SHP / "buildings.shp"), "--roads", str(SHP / "roads.shp"), "--bbox", "126.998,37.5698,127.001,37.5716"])
    assert rc == 0, capsys.readouterr()
    text = capsys.readouterr().out
    assert "배경 번들: 도형 9개 (건물 6 · 실폭도로 0 · 도로 중심선 3)" in text and "도로명 필드 RN" in text
    assert out.is_file()
    rc = cli.main(["basemap", "convert", "--out", str(tmp_path / "x.j5basemap.json"), "--geometry-version", "2026-09-01", "--source-name", "가상", "--bbox", "126.998,37.5698,127.001,37.5716"])
    assert rc == cli.USAGE_ERROR, "층 입력 없음"
    rc = cli.main(["basemap", "convert", "--out", str(tmp_path / "y.j5basemap.json"), "--geometry-version", "2026-09-01", "--source-name", "가상", "--buildings", str(SHP / "buildings.shp")])
    assert rc == cli.USAGE_ERROR, "범위 없음"
    rc = cli.main(["basemap", "convert", "--out", str(tmp_path / "z.j5basemap.json"), "--geometry-version", "2026-09-01", "--source-name", "가상", "--buildings", str(SHP / "roads.shp"), "--bbox", "126.998,37.5698,127.001,37.5716", "--json"])
    assert rc == 1 and "shp_not_polygon" in capsys.readouterr().err
