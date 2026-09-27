"""가상 필지 fixture 생성기 (J5-013B-1, J5-025).

가상 필지 6개를 EPSG:5186 Shapefile 로 쓰고(`parcels/synthetic_shp/`), `j5 parcels convert` 로 번들
`parcels/synthetic.j5parcels.json` 을 만든다. web/data/parcels.synthetic.j5parcels.json 은 이 번들과 같아야 한다.
J5-025: 같은 6개 필지를 VWorld 다운로드 형식(바깥 ZIP 안에 자료별 ZIP, 열 이름 A0…, 변경컬럼정보.csv, UTF-8, EPSG:4326)의 가상 묶음으로도 만들어
(`write_vworld_set`, 시험 때 임시 폴더에 생성) 토지특성·토지이용계획·토지소유공간정보 속성이 붙은 번들 `parcels/synthetic_vworld.j5parcels.json` 을 만든다.
표준 라이브러리와 j5.parcels 만 쓴다. 좌표·PNU·속성·소유구분은 모두 가상값이다(시도 코드 99 는 존재하지 않는다).

    python tests/fixtures/make_parcels.py
"""

from __future__ import annotations

import io
import shutil
import struct
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(REPO))

from j5.parcels.convert import Clip, ConvertOptions, convert, write_bundle  # noqa: E402
from j5.parcels.crs import KNOWN, forward_tm  # noqa: E402
from j5.parcels.vworld import detect_vworld, read_attrs  # noqa: E402

OUT_SHP = ROOT / "parcels" / "synthetic_shp"
OUT_BUNDLE = ROOT / "parcels" / "synthetic.j5parcels.json"
OUT_VW_BUNDLE = ROOT / "parcels" / "synthetic_vworld.j5parcels.json"
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


def write_shapefile(base: Path, records: list[tuple[list[list[tuple[float, float]]], dict]], fields: list[tuple[str, str, int]], prj: str, encoding="cp949", shape_type: int = 5, cpg: str | None = "EUC-KR") -> None:
    """records: [(링 목록(원본 좌표, 방향은 이미 Shapefile 규칙), 속성 dict)], fields: [(이름, 타입 C/N, 길이)]. shape_type 5 폴리곤, 3 폴리라인(레코드 배치 동일). cpg None 이면 .cpg 를 쓰지 않는다."""
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
        content = struct.pack("<i", shape_type) + struct.pack("<4d", min(xs), min(ys), max(xs), max(ys)) + struct.pack("<ii", len(rings), len(pts))
        content += struct.pack(f"<{len(parts)}i", *parts) + b"".join(struct.pack("<2d", x, y) for x, y in pts)
        words = len(content) // 2
        shp_records.append(struct.pack(">ii", i, words) + content)
        shx_records.append(struct.pack(">ii", offset, words))
        offset += 4 + words
    bbox = (min(all_x), min(all_y), max(all_x), max(all_y))

    def header(total_words: int) -> bytes:
        return struct.pack(">i", 9994) + b"\x00" * 20 + struct.pack(">i", total_words) + struct.pack("<ii", 1000, shape_type) + struct.pack("<4d", *bbox) + struct.pack("<4d", 0, 0, 0, 0)

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
                text = "" if v is None else str(v)
                while len(text.encode(encoding)) > length:   # 열 길이에 맞춰 문자 단위로 자른다 (다바이트 문자를 반으로 자르지 않게)
                    text = text[:-1]
                rec += text.encode(encoding).ljust(length, b" ")
            else:
                rec += ("" if v is None else str(v)).encode("ascii").rjust(length, b" ")[:length]
        dbf += rec
    dbf += b"\x1A"
    base.with_suffix(".dbf").write_bytes(bytes(dbf))
    base.with_suffix(".prj").write_text(prj, encoding="utf-8")
    if cpg:
        base.with_suffix(".cpg").write_text(cpg, encoding="ascii")


def synthetic_records():
    recs = []
    for pnu, jibun, outers, holes in PARCELS:
        rings = [_ring_src(r, True) for r in outers] + [_ring_src(r, False) for r in holes]
        recs.append((rings, {"PNU": pnu, "JIBUN": jibun, "BCHK": "1", "SGG_OID": len(recs) + 1, "COL_ADM_SE": "99999"}))
    return recs


FIELDS = [("PNU", "C", 19), ("JIBUN", "C", 30), ("BCHK", "C", 1), ("SGG_OID", "N", 10), ("COL_ADM_SE", "C", 5)]


# ---------------------------------------------------------------- VWorld 형식 가상 묶음 (J5-025)
PRJ_4326 = ('GEOGCS["WGS 84", DATUM["World Geodetic System 1984", SPHEROID["WGS 84", 6378137.0, 298.257223563, AUTHORITY["EPSG","7030"]], AUTHORITY["EPSG","6326"]], '
            'PRIMEM["Greenwich", 0.0, AUTHORITY["EPSG","8901"]], UNIT["degree", 0.017453292519943295], AXIS["Geodetic latitude", NORTH], AXIS["Geodetic longitude", EAST], AUTHORITY["EPSG","4326"]]')
VW_STAMP = "0001_202609270900"
VW_NAMES = {"d002": "연속지적도형정보", "d194": "토지특성공간정보", "d154": "토지이용계획공간정보", "d160": "토지소유공간정보"}
# 자료별 원본 열 이름 (실측한 VWorld 열 순서와 다르게 섞어 두어 A 번호가 아니라 변경컬럼정보.csv 로 찾는지 확인한다)
VW_COLUMNS = {
    "d002": ["src_objectid", "last_updt_dt", "lnm_lndcgr_smbol", "ld_emd_li_code", "pnu", "regstr_se_code"],
    "d194": ["pnu", "lndcgr_code_nm", "lndpcl_ar", "pblntf_pclnd", "stdr_year", "stdr_mt", "prpos_area_1_nm", "prpos_area_2_nm", "lad_use_sittn_nm", "road_side_code_nm", "tpgrph_hg_code_nm", "tpgrph_frm_code_nm", "src_signgu_code"],
    "d154": ["src_signgu_code", "prpos_area_dstrc_code_list", "pnu", "prpos_area_dstrc_nm_list", "cnflc_at_nm_list"],
    "d160": ["ownship_chg_cause_code", "agrde_se_code", "posesn_se_code", "cnrs_psn_co", "resdnc_se_code", "pnu", "lbl", "ownship_chg_de", "nation_instt_se_code"],
}
# PNU 별 가상 속성 (값은 모두 가상). 4-2 는 도로라 이용상황·도로접면이 '지정되지않음', 산1-2 는 소유 자료가 없고, 2 번은 규제 이름 목록이 열 길이에 잘린다.
VW_FEATURE = {
    PARCELS[0][0]: {"lndcgr_code_nm": "대", "lndpcl_ar": "1770.5", "pblntf_pclnd": "12340000", "stdr_year": "2026", "stdr_mt": "1", "prpos_area_1_nm": "일반상업지역", "prpos_area_2_nm": "지정되지않음", "lad_use_sittn_nm": "상업용", "road_side_code_nm": "광대로한면", "tpgrph_hg_code_nm": "평지", "tpgrph_frm_code_nm": "세로장방"},
    PARCELS[1][0]: {"lndcgr_code_nm": "대", "lndpcl_ar": "980.0", "pblntf_pclnd": "9870000", "stdr_year": "2026", "stdr_mt": "1", "prpos_area_1_nm": "제2종일반주거지역", "prpos_area_2_nm": "지정되지않음", "lad_use_sittn_nm": "주상용", "road_side_code_nm": "소로한면", "tpgrph_hg_code_nm": "평지", "tpgrph_frm_code_nm": "가로장방"},
    PARCELS[2][0]: {"lndcgr_code_nm": "대", "lndpcl_ar": "1230.4", "pblntf_pclnd": "8880000", "stdr_year": "2026", "stdr_mt": "1", "prpos_area_1_nm": "제3종일반주거지역", "prpos_area_2_nm": "준주거지역", "lad_use_sittn_nm": "주거용", "road_side_code_nm": "세로한면(가)", "tpgrph_hg_code_nm": "완경사", "tpgrph_frm_code_nm": "부정형"},
    PARCELS[3][0]: {"lndcgr_code_nm": "대", "lndpcl_ar": "1100.0", "pblntf_pclnd": "", "stdr_year": "", "stdr_mt": "", "prpos_area_1_nm": "준공업지역", "prpos_area_2_nm": "지정되지않음", "lad_use_sittn_nm": "공업용", "road_side_code_nm": "소로각지", "tpgrph_hg_code_nm": "평지", "tpgrph_frm_code_nm": "정방형"},
    PARCELS[4][0]: {"lndcgr_code_nm": "도로", "lndpcl_ar": "1080.2", "pblntf_pclnd": "1200000", "stdr_year": "2026", "stdr_mt": "1", "prpos_area_1_nm": "자연녹지지역", "prpos_area_2_nm": "지정되지않음", "lad_use_sittn_nm": "지정되지않음", "road_side_code_nm": "지정되지않음", "tpgrph_hg_code_nm": "지정되지않음", "tpgrph_frm_code_nm": "지정되지않음"},
    PARCELS[5][0]: {"lndcgr_code_nm": "임야", "lndpcl_ar": "2500.0", "pblntf_pclnd": "300000", "stdr_year": "2026", "stdr_mt": "1", "prpos_area_1_nm": "보전관리지역", "prpos_area_2_nm": "지정되지않음", "lad_use_sittn_nm": "임야", "road_side_code_nm": "맹지", "tpgrph_hg_code_nm": "급경사", "tpgrph_frm_code_nm": "부정형"},
}
VW_PLAN = {
    PARCELS[0][0]: {"prpos_area_dstrc_code_list": "UQA01X,UQA220,UQQ300", "prpos_area_dstrc_nm_list": "도시지역,일반상업지역,지구단위계획구역(가상)", "cnflc_at_nm_list": "포함,포함,포함"},
    PARCELS[1][0]: {"prpos_area_dstrc_code_list": "UQA01X,UQA123,UQS100", "prpos_area_dstrc_nm_list": "도시지역,제2종일반주거지역,소로2류(폭 8m~10m)", "cnflc_at_nm_list": "포함,포함,접함"},
    PARCELS[2][0]: {"prpos_area_dstrc_code_list": "UQA01X,UQA124,UQA130,UQQ600,ZA0014", "prpos_area_dstrc_nm_list": "도시지역,제3종일반주거지역,준주거지역,토지거래계약에관한허가구역(가상 아주 긴 이름)", "cnflc_at_nm_list": "포함,포함,포함,포함,저촉"},
    # 실측 모양(J5-032): 한 코드(UOA120)의 이름이 두 번 나와 이름 목록이 코드보다 길고 순서가 어긋난다. 위치로 짝지으면 준공업지역 코드에 다른 이름이 붙는다
    PARCELS[3][0]: {"prpos_area_dstrc_code_list": "UQA01X,UOA120,UQA320", "prpos_area_dstrc_nm_list": "도시지역,상대보호구역,상대보호구역(가상),준공업지역", "cnflc_at_nm_list": "포함,저촉,포함"},
    PARCELS[4][0]: {"prpos_area_dstrc_code_list": "UQA01X,UQA410,UQS100", "prpos_area_dstrc_nm_list": "도시지역,자연녹지지역,중로1류(폭 20m~25m)", "cnflc_at_nm_list": "포함,포함,저촉"},
    PARCELS[5][0]: {"prpos_area_dstrc_code_list": "UQA02X,UQA520", "prpos_area_dstrc_nm_list": "관리지역,보전관리지역", "cnflc_at_nm_list": "포함,포함"},
}
# 소유: 구분 코드·구분명·공유인수·변동일·원인 코드·국가기관 구분만. 연령대(agrde_se_code)·거주 구분(resdnc_se_code)은 앱이 읽지 않아야 한다 (가상값을 넣어 두고 번들에 없음을 확인).
VW_OWN = {
    PARCELS[0][0]: {"posesn_se_code": "01", "lbl": "개인", "cnrs_psn_co": "1", "ownship_chg_de": "20170101", "ownship_chg_cause_code": "03", "nation_instt_se_code": "ZZ", "agrde_se_code": "5", "resdnc_se_code": "1"},
    PARCELS[1][0]: {"posesn_se_code": "06", "lbl": "법인", "cnrs_psn_co": "1", "ownship_chg_de": "20200315", "ownship_chg_cause_code": "04", "nation_instt_se_code": "ZZ", "agrde_se_code": "", "resdnc_se_code": ""},
    PARCELS[2][0]: {"posesn_se_code": "01", "lbl": "개인", "cnrs_psn_co": "3", "ownship_chg_de": "20110707", "ownship_chg_cause_code": "04", "nation_instt_se_code": "ZZ", "agrde_se_code": "6", "resdnc_se_code": "2"},
    PARCELS[3][0]: {"posesn_se_code": "", "lbl": "", "cnrs_psn_co": "", "ownship_chg_de": "", "ownship_chg_cause_code": "", "nation_instt_se_code": "", "agrde_se_code": "", "resdnc_se_code": ""},
    PARCELS[4][0]: {"posesn_se_code": "02", "lbl": "국유지", "cnrs_psn_co": "1", "ownship_chg_de": "19950101", "ownship_chg_cause_code": "01", "nation_instt_se_code": "01", "agrde_se_code": "", "resdnc_se_code": ""},
}
VW_LENGTHS = {"prpos_area_dstrc_nm_list": 80, "lnm_lndcgr_smbol": 30, "pnu": 19, "last_updt_dt": 10, "ld_emd_li_code": 10, "src_objectid": 10, "regstr_se_code": 2, "lndcgr_code_nm": 20, "lndpcl_ar": 16,
              "pblntf_pclnd": 12, "stdr_year": 4, "stdr_mt": 2, "src_signgu_code": 5, "prpos_area_dstrc_code_list": 120, "cnflc_at_nm_list": 60, "ownship_chg_cause_code": 2, "agrde_se_code": 2,
              "posesn_se_code": 2, "cnrs_psn_co": 5, "resdnc_se_code": 2, "lbl": 30, "ownship_chg_de": 8, "nation_instt_se_code": 2}


def _vw_rows(kind: str) -> list[tuple[list, dict]]:
    """자료별 레코드: (WGS84 링 목록(Shapefile 방향), 원본 열 이름 → 값)."""
    rows = []
    for i, (pnu, jibun, outers, holes) in enumerate(PARCELS):
        rings = [list(reversed(r)) for r in outers] + [list(r) for r in holes]
        if kind == "d002":
            attrs = {"src_objectid": str(i + 1), "last_updt_dt": "2026-09-05", "lnm_lndcgr_smbol": jibun, "ld_emd_li_code": EMD[5:], "pnu": pnu, "regstr_se_code": "1"}
        elif kind == "d194":
            attrs = {"pnu": pnu, "src_signgu_code": EMD[:5], **VW_FEATURE[pnu]}
        elif kind == "d154":
            attrs = {"src_signgu_code": EMD[:5], "pnu": pnu, **VW_PLAN[pnu]}
        else:
            if pnu not in VW_OWN:
                continue
            attrs = {"pnu": pnu, **VW_OWN[pnu]}
        rows.append((rings, attrs))
    return rows


def write_vworld_set(out_dir: Path, *, stamp: str = VW_STAMP) -> Path:
    """VWorld 다운로드 형식의 가상 묶음을 out_dir 에 만든다: <stamp>_synthetic_vworld.zip 안에 자료별 ZIP 4개 (열 이름 A0…, 변경컬럼정보.csv, UTF-8 .dbf, .cpg 없음, EPSG:4326).
    시각을 고정해 바이트가 재현된다. 반환: 바깥 ZIP 경로."""
    out_dir.mkdir(parents=True, exist_ok=True)
    outer = out_dir / f"{stamp}_synthetic_vworld.zip"
    fixed = (2026, 9, 27, 9, 0, 0)
    with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_DEFLATED) as zo:
        for code, label in VW_NAMES.items():
            cols = VW_COLUMNS[code]
            mapping = {name: f"A{i}" for i, name in enumerate(cols)}
            work = out_dir / f"_work_{code}"
            if work.exists():
                shutil.rmtree(work)
            base = work / f"{stamp}_dt_{code}_{label}"
            fields = [(mapping[c], "C", VW_LENGTHS.get(c, 60)) for c in cols]
            records = [(rings, {mapping[k]: v for k, v in attrs.items()}) for rings, attrs in _vw_rows(code)]
            write_shapefile(base, records, fields, PRJ_4326, encoding="utf-8", cpg=None)
            csv_text = "\ufeff원본컬럼명,변경컬럼명\n" + "".join(f"{c},{mapping[c]}\n" for c in cols)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zi:
                zi.writestr(zipfile.ZipInfo(f"dt_{code}_변경컬럼정보.csv", fixed), csv_text.encode("utf-8"))
                for ext in (".shp", ".shx", ".dbf", ".prj"):
                    zi.writestr(zipfile.ZipInfo(base.name + ext, fixed), base.with_suffix(ext).read_bytes())
            shutil.rmtree(work)
            zo.writestr(zipfile.ZipInfo(f"{stamp}_dt_{code}_{label}.zip", fixed), buf.getvalue())
    return outer


def build_vworld_bundle(vw_zip: Path, out: Path) -> dict:
    """가상 VWorld 묶음 → 속성이 붙은 번들 (CLI 의 `parcels convert` 와 같은 경로). 생성 시각 고정."""
    vw = detect_vworld(vw_zip)
    assert vw is not None
    try:
        attrs_by_pnu, attrs_sources = read_attrs(vw, encoding="utf-8")
        opts = ConvertOptions(
            clip=Clip.from_bbox("126.998,37.5698,127.001,37.5716"), geometry_version="2026-09-05", source_name="가상 VWorld 토지 자료 (synthetic)",
            license="가상자료 (이용허락 해당 없음)", emd_names={EMD: "가상동"}, data_mode="synthetic", now=GENERATED_AT, encoding="utf-8",
            field_map=vw.mappings.get("cadastral"), attrs_by_pnu=attrs_by_pnu, attrs_sources=attrs_sources,
        )
        bundle = convert(vw.cadastral, opts)
    finally:
        vw.cleanup()
    out.unlink(missing_ok=True)
    write_bundle(bundle, out)
    return bundle


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
    import tempfile
    with tempfile.TemporaryDirectory(prefix="j5vw-fixture-") as td:
        vw_bundle = build_vworld_bundle(write_vworld_set(Path(td)), OUT_VW_BUNDLE)
    print(f"{OUT_SHP} ({len(PARCELS)} 필지), {OUT_BUNDLE}, {WEB_BUNDLE}, {OUT_VW_BUNDLE} (속성 {vw_bundle['stats'].get('attrs_attached')})")


if __name__ == "__main__":
    main()
