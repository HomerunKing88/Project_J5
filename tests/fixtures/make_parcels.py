"""가상 필지 fixture 생성기 (J5-013B-1).

가상 필지 6개를 EPSG:5186 Shapefile 로 쓰고(`parcels/synthetic_shp/`), `j5 parcels convert` 로 번들
`parcels/synthetic.j5parcels.json` 을 만든다. web/data/parcels.synthetic.j5parcels.json 은 이 번들과 같아야 한다.
표준 라이브러리와 j5.parcels 만 쓴다. 좌표·PNU 는 모두 가상값이다(시도 코드 99 는 존재하지 않는다).

    python tests/fixtures/make_parcels.py
"""

from __future__ import annotations

import shutil
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(REPO))

from j5.parcels.convert import Clip, ConvertOptions, convert, write_bundle  # noqa: E402
from j5.parcels.crs import KNOWN, forward_tm  # noqa: E402

OUT_SHP = ROOT / "parcels" / "synthetic_shp"
OUT_BUNDLE = ROOT / "parcels" / "synthetic.j5parcels.json"
WEB_BUNDLE = REPO / "web" / "data" / "parcels.synthetic.j5parcels.json"
GENERATED_AT = datetime(2026, 9, 23, 9, 0, 0, tzinfo=timezone.utc)
CRS = KNOWN[5186]
PRJ_5186 = ('PROJCS["KGD2002_Central_Belt_2010",GEOGCS["GCS_KGD2002",DATUM["D_Korea_Geodetic_Datum_2002",SPHEROID["GRS_1980",6378137.0,298.257222101]],'
            'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",200000.0],'
            'PARAMETER["False_Northing",600000.0],PARAMETER["Central_Meridian",127.0],PARAMETER["Scale_Factor",1.0],PARAMETER["Latitude_Of_Origin",38.0],UNIT["Meter",1.0]]')

# 가상 필지: (PNU, JIBUN 원문, [외곽 링(WGS84 lon,lat, 반시계)], [구멍 링(반시계)]). 가상 시드 물건 1·2·4·5 가 각각 1·2·3·4 번 필지 안에 있다.
# 시도 99·시군구 999·법정동 001 은 존재하지 않는 코드다.
EMD = "9999900100"


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


PARCELS = [
    (EMD + "1" + "0001" + "0000", "1대", [rect(126.99855, 37.57005, 126.99915, 37.57035)], []),                      # 1: 물건 1 (서쪽 끝)
    (EMD + "1" + "0001" + "0001", "1-1대", [rect(126.9989, 37.5704, 126.9993, 37.5707)], []),                        # 1-1: 물건 2
    (EMD + "1" + "0002" + "0000", "2대", [rect(126.9994, 37.5707, 126.9999, 37.5710)], []),                          # 2: 물건 4
    (EMD + "1" + "0003" + "0000", "3대", [rect(127.0001, 37.5711, 127.0005, 37.5714)], []),                          # 3: 물건 5
    (EMD + "1" + "0004" + "0002", "4-2도", [rect(126.9993, 37.5703, 126.9997, 37.5706)], [rect(126.99935, 37.57035, 126.99945, 37.57045)]),  # 4-2: 구멍, 물건 없음
    (EMD + "2" + "0001" + "0002", "산1-2임", [rect(127.0000, 37.5702, 127.00015, 37.5705), rect(127.00025, 37.5702, 127.0004, 37.5705)], []),  # 산1-2: 두 조각
]


def _ring_src(ring, clockwise: bool):
    pts = [forward_tm(CRS, lon, lat) for lon, lat in ring]
    # Shapefile: 외곽 시계방향, 구멍 반시계. 입력은 반시계이므로 외곽만 뒤집는다.
    return list(reversed(pts)) if clockwise else pts


def write_shapefile(base: Path, records: list[tuple[list[list[tuple[float, float]]], dict]], fields: list[tuple[str, str, int]], prj: str, encoding="cp949") -> None:
    """records: [(링 목록(원본 좌표, 방향은 이미 Shapefile 규칙), 속성 dict)], fields: [(이름, 타입 C/N, 길이)]."""
    base.parent.mkdir(parents=True, exist_ok=True)
    shp_records = []
    shx_records = []
    all_x, all_y = [], []
    offset = 50  # 16비트 워드 단위 (헤더 100 바이트)
    for i, (rings, _) in enumerate(records, start=1):
        pts = [p for r in rings for p in r]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        all_x += xs
        all_y += ys
        parts = []
        acc = 0
        for r in rings:
            parts.append(acc)
            acc += len(r)
        content = struct.pack("<i", 5) + struct.pack("<4d", min(xs), min(ys), max(xs), max(ys)) + struct.pack("<ii", len(rings), len(pts))
        content += struct.pack(f"<{len(parts)}i", *parts) + b"".join(struct.pack("<2d", x, y) for x, y in pts)
        words = len(content) // 2
        shp_records.append(struct.pack(">ii", i, words) + content)
        shx_records.append(struct.pack(">ii", offset, words))
        offset += 4 + words
    bbox = (min(all_x), min(all_y), max(all_x), max(all_y))

    def header(total_words: int) -> bytes:
        return struct.pack(">i", 9994) + b"\x00" * 20 + struct.pack(">i", total_words) + struct.pack("<ii", 1000, 5) + struct.pack("<4d", *bbox) + struct.pack("<4d", 0, 0, 0, 0)

    shp = b"".join(shp_records)
    base.with_suffix(".shp").write_bytes(header(50 + len(shp) // 2) + shp)
    shx = b"".join(shx_records)
    base.with_suffix(".shx").write_bytes(header(50 + len(shx) // 2) + shx)
    # DBF
    descs = b""
    for name, typ, length in fields:
        descs += name.encode("ascii").ljust(11, b"\x00") + typ.encode("ascii") + b"\x00" * 4 + bytes([length, 0]) + b"\x00" * 14
    header_len = 32 + len(descs) + 1
    record_len = 1 + sum(f[2] for f in fields)
    dbf = bytearray(struct.pack("<BBBBIHH", 0x03, 26, 9, 23, len(records), header_len, record_len) + b"\x00" * 20)
    dbf[29] = 0x4E  # 언어 구동기: 한국어
    dbf += descs + b"\x0D"
    for _, row in records:
        rec = b" "
        for name, typ, length in fields:
            v = row.get(name)
            if typ == "C":
                rec += ("" if v is None else str(v)).encode(encoding).ljust(length, b" ")[:length]
            else:
                rec += ("" if v is None else str(v)).encode("ascii").rjust(length, b" ")[:length]
        dbf += rec
    dbf += b"\x1A"
    base.with_suffix(".dbf").write_bytes(bytes(dbf))
    base.with_suffix(".prj").write_text(prj, encoding="utf-8")
    base.with_suffix(".cpg").write_text("EUC-KR", encoding="ascii")


def synthetic_records():
    recs = []
    for pnu, jibun, outers, holes in PARCELS:
        rings = [_ring_src(r, True) for r in outers] + [_ring_src(r, False) for r in holes]
        recs.append((rings, {"PNU": pnu, "JIBUN": jibun, "BCHK": "1", "SGG_OID": len(recs) + 1, "COL_ADM_SE": "99999"}))
    return recs


FIELDS = [("PNU", "C", 19), ("JIBUN", "C", 30), ("BCHK", "C", 1), ("SGG_OID", "N", 10), ("COL_ADM_SE", "C", 5)]


def main() -> None:
    if OUT_SHP.exists():
        shutil.rmtree(OUT_SHP)
    write_shapefile(OUT_SHP / "synthetic_parcels", synthetic_records(), FIELDS, PRJ_5186)
    opts = ConvertOptions(
        clip=Clip.from_bbox("126.998,37.5698,127.001,37.5716"), geometry_version="2026-09-01", source_name="가상 연속지적도 (synthetic)",
        license="가상자료 (이용허락 해당 없음)", emd_names={EMD: "가상동"}, data_mode="synthetic", now=GENERATED_AT,
    )
    bundle = convert(OUT_SHP / "synthetic_parcels.shp", opts)
    OUT_BUNDLE.unlink(missing_ok=True)
    write_bundle(bundle, OUT_BUNDLE)
    WEB_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(OUT_BUNDLE, WEB_BUNDLE)
    print(f"{OUT_SHP} ({len(PARCELS)} 필지), {OUT_BUNDLE}, {WEB_BUNDLE}")


if __name__ == "__main__":
    main()
