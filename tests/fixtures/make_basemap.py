"""가상 배경 도형 fixture 생성기 (J5-022, ADR-16).

가상 필지(make_parcels.py) 주변에 건물 윤곽 7개(폴리곤)·실폭도로 2개(폴리곤)·도로 중심선 3개(폴리라인, 도로명)를 EPSG:5186 Shapefile 로 쓰고
(`basemap/synthetic_shp/`), `j5 basemap convert` 로 번들 `basemap/synthetic.j5basemap.json` 을 만든다.
web/data/basemap.synthetic.j5basemap.json 은 이 번들과 같아야 한다. 좌표·이름은 모두 가상값이다.

    python tests/fixtures/make_basemap.py
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(REPO))

from j5.parcels.basemap import BasemapOptions, LayerInput, convert_basemap, write_basemap  # noqa: E402
from j5.parcels.crs import KNOWN, forward_tm  # noqa: E402
from tests.fixtures.make_parcels import PRJ_5186, rect, write_shapefile  # noqa: E402

OUT_SHP = ROOT / "basemap" / "synthetic_shp"
OUT_BUNDLE = ROOT / "basemap" / "synthetic.j5basemap.json"
WEB_BUNDLE = REPO / "web" / "data" / "basemap.synthetic.j5basemap.json"
GENERATED_AT = datetime(2026, 9, 27, 9, 0, 0, tzinfo=timezone.utc)
CRS = KNOWN[5186]

# 건물 윤곽 (WGS84, 반시계). 필지 1·1-1·2·3 안에 하나씩, 필지 밖 2개, 범위 밖 1개(잘려 나감)
BUILDINGS = [
    [rect(126.99862, 37.57010, 126.99885, 37.57028)],
    [rect(126.99893, 37.57045, 126.99915, 37.57065)],
    [rect(126.99945, 37.57075, 126.99975, 37.57095)],
    [rect(127.00015, 37.57115, 127.00045, 37.57135)],
    [rect(126.99950, 37.57000, 126.99975, 37.57015)],
    [rect(127.00010, 37.57060, 127.00040, 37.57080)],
    [rect(127.00300, 37.57500, 127.00330, 37.57520)],   # 범위 밖
]
# 실폭도로 (폴리곤): 남북 도로 하나, 동서 도로 하나
ROAD_AREAS = [
    [rect(126.99920, 37.56990, 126.99935, 37.57160)],
    [rect(126.99840, 37.57036, 127.00060, 37.57046)],
]
# 도로 중심선 (폴리라인, 도로명). 세 번째는 두 파트
ROADS = [
    ([[(126.999275, 37.56990), (126.999275, 37.57160)]], "가상로"),
    ([[(126.99840, 37.57041), (127.00060, 37.57041)]], "가상1길"),
    ([[(126.99960, 37.57100), (126.99990, 37.57120)], [(127.00000, 37.57125), (127.00030, 37.57140)]], "가상2길"),
]


def _poly_src(rings):
    # Shapefile 외곽은 시계방향. 입력은 반시계이므로 뒤집는다.
    return [list(reversed([forward_tm(CRS, lon, lat) for lon, lat in r])) for r in rings]


def _line_src(parts):
    return [[forward_tm(CRS, lon, lat) for lon, lat in p] for p in parts]


BUILDING_FIELDS = [("BD_MGT_SN", "C", 25), ("GRO_FLO_CO", "N", 5), ("BULD_NM", "C", 40)]
ROAD_AREA_FIELDS = [("RW_SN", "C", 12), ("WDR_RD_CD", "C", 2)]
ROAD_FIELDS = [("RDS_MAN_NO", "C", 10), ("RN", "C", 80), ("ROAD_BT", "N", 5)]


def main() -> None:
    if OUT_SHP.exists():
        shutil.rmtree(OUT_SHP)
    write_shapefile(OUT_SHP / "buildings", [(_poly_src(b), {"BD_MGT_SN": f"99999{i:020d}", "GRO_FLO_CO": 3 + i, "BULD_NM": f"가상건물{i}"}) for i, b in enumerate(BUILDINGS, 1)], BUILDING_FIELDS, PRJ_5186)
    write_shapefile(OUT_SHP / "road_areas", [(_poly_src(r), {"RW_SN": f"RW{i:06d}", "WDR_RD_CD": "03"}) for i, r in enumerate(ROAD_AREAS, 1)], ROAD_AREA_FIELDS, PRJ_5186)
    write_shapefile(OUT_SHP / "roads", [(_line_src(p), {"RDS_MAN_NO": f"RD{i:06d}", "RN": name, "ROAD_BT": 8}) for i, (p, name) in enumerate(ROADS, 1)], ROAD_FIELDS, PRJ_5186, shape_type=3)
    from j5.parcels.convert import Clip
    opts = BasemapOptions(
        clip=Clip.from_bbox("126.998,37.5698,127.001,37.5716"), geometry_version="2026-09-01", source_name="가상 배경 도형 (synthetic)",
        inputs=[LayerInput("building", OUT_SHP / "buildings.shp"), LayerInput("road_area", OUT_SHP / "road_areas.shp"), LayerInput("road", OUT_SHP / "roads.shp")],
        license="가상자료 (이용허락 해당 없음)", data_mode="synthetic", now=GENERATED_AT,
    )
    bundle = convert_basemap(opts)
    OUT_BUNDLE.unlink(missing_ok=True)
    write_basemap(bundle, OUT_BUNDLE)
    WEB_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(OUT_BUNDLE, WEB_BUNDLE)
    print(f"{OUT_SHP}, {OUT_BUNDLE} ({bundle['counts']}), {WEB_BUNDLE}")


if __name__ == "__main__":
    main()
