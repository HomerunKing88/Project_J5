"""VWorld(국토교통부 공간정보 오픈플랫폼) 토지 자료 묶음 인식 (J5-025, ADR-19).

VWorld 에서 구역을 지정해 내려받은 ZIP 은 안에 자료별 ZIP 이 들어 있다:
  <번호>_<시각>_dt_d002_연속지적도형정보.zip   필지 도형 + PNU (필수)
  <번호>_<시각>_dt_d194_토지특성공간정보.zip   지목·면적·공시지가·용도지역·이용상황·도로접면·지형 (선택)
  <번호>_<시각>_dt_d154_토지이용계획공간정보.zip 용도지역·지구 목록과 저촉 여부 (선택)
  <번호>_<시각>_dt_d160_토지소유공간정보.zip   소유구분·공유인수·변동일·변동원인 코드 (선택; 이름·주소 없음)
각 ZIP 의 .dbf 는 열 이름이 A0, A1, … 로 익명화돼 있고 원래 이름은 함께 든 `dt_dNNN_변경컬럼정보.csv` 에 있다. 문자는 UTF-8, 좌표계는 WGS84 다.
이 모듈은 묶음을 찾아 열 이름 표를 읽고, 선택 자료를 PNU 별 속성으로 정리한다. 통신은 없다. 소유자 이름·주소·연령대·거주 구분처럼 개인을 가리키는 값은 읽지 않는다.
실제 자료는 저장소에 넣지 않는다.
"""

from __future__ import annotations

import csv
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from j5.parcels.shp import MAX_MEMBER_BYTES, MAX_RATIO, MAX_TOTAL_BYTES, ShapeError, ShapeSource, _safe_member, iter_dbf_records, open_source, read_dbf_fields

KINDS = {"d002": "cadastral", "d194": "land_feature", "d154": "land_plan", "d160": "land_ownership"}
KIND_LABEL = {"cadastral": "연속지적도형정보", "land_feature": "토지특성공간정보", "land_plan": "토지이용계획공간정보", "land_ownership": "토지소유공간정보"}
MAPPING_CSV = re.compile(r"변경컬럼정보\.csv$")
DATASET_RE = re.compile(r"dt_(d\d{3})_")
ATTRS_VERSION = "1.0.0"
# 읽지 않는 열 (개인을 가리키거나 필요 없는 값). 소유 자료의 연령대·거주 구분은 소유자 개인 정보라 버린다.
PRIVATE_FIELDS = {"agrde_se_code", "resdnc_se_code"}


class VWorldError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class VWorldSet:
    cadastral: Path                       # d002 의 .shp 또는 ZIP (open_source 가 읽는 형태)
    datasets: dict[str, Path] = field(default_factory=dict)   # kind → .shp 또는 ZIP (d002 포함)
    mappings: dict[str, dict[str, str]] = field(default_factory=dict)  # kind → {A0: 원래 이름}
    origin: str = ""
    tmpdir: tempfile.TemporaryDirectory | None = None

    def cleanup(self) -> None:
        if self.tmpdir is not None:
            self.tmpdir.cleanup()
            self.tmpdir = None


def _kind_of(name: str) -> str | None:
    m = DATASET_RE.search(Path(name).name)
    return KINDS.get(m.group(1)) if m else None


def _decode_csv(raw: bytes) -> list[list[str]]:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return list(csv.reader(raw.decode(enc).splitlines()))
        except UnicodeDecodeError:
            continue
    raise VWorldError("mapping_decode", "변경컬럼정보.csv 를 UTF-8 이나 CP949 로 읽지 못했다")


def _mapping_from_rows(rows: list[list[str]]) -> dict[str, str]:
    out = {}
    for r in rows[1:] if rows and rows[0][:1] == ["원본컬럼명"] else rows:
        if len(r) >= 2 and r[0] and r[1]:
            out[r[1].strip()] = r[0].strip()
    return out


def read_mapping(source: Path) -> dict[str, str]:
    """자료(.shp 또는 ZIP) 옆·안의 변경컬럼정보.csv → {A열 이름: 원래 이름}. 없으면 {}."""
    source = Path(source)
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as zf:
            for info in zf.infolist():
                if MAPPING_CSV.search(info.filename) and _safe_member(info.filename) and info.file_size <= 1_000_000:
                    return _mapping_from_rows(_decode_csv(zf.read(info)))
        return {}
    cands = sorted(source.parent.glob("*변경컬럼정보.csv"))
    m = DATASET_RE.search(source.name)
    if m:   # 한 폴더에 여러 자료의 CSV 가 있으면 같은 자료 코드(dt_dNNN)의 것을 고른다
        same = [c for c in cands if f"dt_{m.group(1)}_" in c.name]
        cands = same or ([] if len(cands) > 1 else cands)
    for cand in cands:
        return _mapping_from_rows(_decode_csv(cand.read_bytes()))
    return {}


def _extract_nested(zpath: Path) -> VWorldSet | None:
    """바깥 ZIP 에 자료별 ZIP 이 들어 있으면 임시 폴더에 안전하게 풀어 묶음으로 만든다. 아니면 None."""
    with zipfile.ZipFile(zpath) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        inner = [i for i in infos if i.filename.lower().endswith(".zip") and _kind_of(i.filename)]
        if not inner:
            return None
        for i in inner:
            if not _safe_member(i.filename):
                raise VWorldError("zip_unsafe_path", f"ZIP 항목 경로가 안전하지 않다: {i.filename}")
        total = sum(i.file_size for i in inner)
        compressed = sum(i.compress_size for i in inner)
        if total > MAX_TOTAL_BYTES or any(i.file_size > MAX_MEMBER_BYTES for i in inner):
            raise VWorldError("zip_too_big", f"안쪽 ZIP 합계 {total} 바이트가 상한을 넘는다")
        if total > 1024 * 1024 and total > MAX_RATIO * max(compressed, 1):
            raise VWorldError("zip_ratio", "ZIP 압축비가 비정상이다. 과도한 압축 해제를 차단한다")
        tmp = tempfile.TemporaryDirectory(prefix="j5vworld-")
        root = Path(tmp.name)
        vw = VWorldSet(cadastral=Path(), origin=str(zpath), tmpdir=tmp)
        for i in inner:
            kind = _kind_of(i.filename)
            if kind in vw.datasets:
                raise VWorldError("duplicate_dataset", f"같은 종류의 자료가 두 번 있다: {KIND_LABEL[kind]}")
            out = root / Path(i.filename).name
            data = zf.read(i)
            if len(data) != i.file_size:
                raise VWorldError("zip_size_mismatch", f"ZIP 항목 크기가 헤더와 다르다: {i.filename}")
            out.write_bytes(data)
            vw.datasets[kind] = out
        if "cadastral" not in vw.datasets:
            vw.cleanup()
            raise VWorldError("cadastral_missing", "묶음에 연속지적도형정보(dt_d002)가 없다")
        vw.cadastral = vw.datasets["cadastral"]
        return vw


def detect_vworld(path: Path) -> VWorldSet | None:
    """입력이 VWorld 묶음이면 VWorldSet, 아니면 None. 입력: 바깥 ZIP(안에 자료별 ZIP) / 자료별 ZIP·SHP 하나(같은 폴더의 다른 자료를 찾음) / 자료 폴더."""
    path = Path(path)
    if not path.exists():
        return None
    vw: VWorldSet | None = None
    if path.is_file() and zipfile.is_zipfile(path):
        vw = _extract_nested(path)
        if vw is None and _kind_of(path.name):
            vw = VWorldSet(cadastral=Path(), origin=str(path))
            vw.datasets[_kind_of(path.name)] = path
            _find_siblings(vw, path.parent)
    elif path.is_file() and path.suffix.lower() == ".shp" and _kind_of(path.name):
        vw = VWorldSet(cadastral=Path(), origin=str(path))
        vw.datasets[_kind_of(path.name)] = path
        _find_siblings(vw, path.parent)
        _find_siblings(vw, path.parent.parent)
    elif path.is_dir():
        vw = VWorldSet(cadastral=Path(), origin=str(path))
        _find_siblings(vw, path)
        for sub in sorted(p for p in path.iterdir() if p.is_dir()):
            _find_siblings(vw, sub)
        if not vw.datasets:
            return None
    if vw is None:
        return None
    if "cadastral" not in vw.datasets:
        vw.cleanup()
        raise VWorldError("cadastral_missing", "연속지적도형정보(dt_d002)를 찾지 못했다. 필지 도형 자료가 있어야 한다")
    vw.cadastral = vw.datasets["cadastral"]
    for kind, src in vw.datasets.items():
        vw.mappings[kind] = read_mapping(src)
    return vw


def _find_siblings(vw: VWorldSet, folder: Path) -> None:
    for p in sorted(folder.iterdir()):
        kind = _kind_of(p.name)
        if not kind or kind in vw.datasets:
            continue
        if p.is_file() and (p.suffix.lower() == ".shp" or zipfile.is_zipfile(p)):
            vw.datasets[kind] = p
        elif p.is_dir():
            shp = next(iter(sorted(p.glob("*.shp"))), None)
            if shp:
                vw.datasets[kind] = shp


# ---------------------------------------------------------------- 속성 정리

def _num(v, kind=float):
    if v is None or v == "":
        return None
    try:
        return kind(str(v).strip())
    except ValueError:
        return None


def _str(v) -> str | None:
    if v is None:
        return None
    s = " ".join(str(v).split())
    return s or None


def _date8(v) -> str | None:
    s = _str(v)
    if not s or not re.fullmatch(r"\d{8}", s):
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _rows(src: ShapeSource, mapping: dict[str, str], encoding: str):
    for row in iter_dbf_records(src.dbf, encoding):
        if row is None:
            continue
        yield {mapping.get(k, k): v for k, v in row.items() if mapping.get(k, k) not in PRIVATE_FIELDS}


UNSPECIFIED = ("지정되지않음", "지정되지 않음", "해당없음", "해당 없음")


def _named(v) -> str | None:
    """이름 값. 자료의 '지정되지않음' 은 값이 없는 것이므로 null 로 둔다 (실측: 도로접면·지형 등에도 나타난다)."""
    s = _str(v)
    return None if s in UNSPECIFIED else s


def _feature_attrs(row: dict) -> dict:
    return {
        "jimok_name": _named(row.get("lndcgr_code_nm")), "registered_area_m2": _num(row.get("lndpcl_ar")),
        "official_land_price_krw_m2": _num(row.get("pblntf_pclnd"), int), "price_base_year": _num(row.get("stdr_year"), int), "price_base_month": _num(row.get("stdr_mt"), int),
        "use_zone_1": _named(row.get("prpos_area_1_nm")), "use_zone_2": _named(row.get("prpos_area_2_nm")), "land_use_situation": _named(row.get("lad_use_sittn_nm")),
        "road_side": _named(row.get("road_side_code_nm")), "terrain_height": _named(row.get("tpgrph_hg_code_nm")), "terrain_form": _named(row.get("tpgrph_frm_code_nm")),
    }


def _plan_attrs(row: dict) -> dict:
    codes = [c.strip() for c in str(row.get("prpos_area_dstrc_code_list") or "").split(",") if c.strip()]
    names = [n.strip() for n in str(row.get("prpos_area_dstrc_nm_list") or "").split(",")] if row.get("prpos_area_dstrc_nm_list") else []
    rels = [r.strip() for r in str(row.get("cnflc_at_nm_list") or "").split(",")] if row.get("cnflc_at_nm_list") else []
    zones = []
    for i, code in enumerate(codes):
        zones.append({"code": code, "name": names[i] if i < len(names) and names[i] else None, "relation": rels[i] if i < len(rels) and rels[i] else None})
    # 이름 목록은 .dbf 열 길이에 잘릴 수 있다 (코드 수보다 이름 수가 적으면 잘린 것)
    return {"plan_zones": zones, "plan_zones_truncated": len(names) < len(codes)}


def _ownership_attrs(row: dict) -> dict:
    return {
        "ownership_kind_code": _str(row.get("posesn_se_code")), "ownership_kind": _str(row.get("lbl")), "co_owner_count": _num(row.get("cnrs_psn_co"), int),
        "ownership_changed_on": _date8(row.get("ownship_chg_de")), "ownership_change_cause_code": _str(row.get("ownship_chg_cause_code")),
        "national_institution_code": _str(row.get("nation_instt_se_code")),
    }


def read_attrs(vw: VWorldSet, *, encoding: str = "utf-8") -> tuple[dict[str, dict], list[dict]]:
    """선택 자료(d194·d154·d160)를 PNU 별 속성으로. 반환 (pnu → attrs, 출처 목록). 자료가 없으면 ({}, [])."""
    attrs: dict[str, dict] = {}
    sources: list[dict] = []
    for kind, fn in (("land_feature", _feature_attrs), ("land_plan", _plan_attrs), ("land_ownership", _ownership_attrs)):
        src_path = vw.datasets.get(kind)
        if not src_path:
            continue
        try:
            src = open_source(src_path)
            fields, num_records, _, _ = read_dbf_fields(src.dbf)
        except ShapeError as e:
            raise VWorldError(e.code, f"{KIND_LABEL[kind]}: {e.message}") from None
        mapping = vw.mappings.get(kind) or read_mapping(src_path)
        names = [mapping.get(f.name, f.name) for f in fields]
        if "pnu" not in names:
            raise VWorldError("pnu_missing", f"{KIND_LABEL[kind]} 에 pnu 열이 없다 (변경컬럼정보.csv 가 없거나 형식이 다르다). 열: {names}")
        n = 0
        try:
            for row in _rows(src, mapping, encoding):
                pnu = _str(row.get("pnu"))
                if not pnu or not re.fullmatch(r"\d{19}", pnu):
                    continue
                attrs.setdefault(pnu, {}).update(fn(row))
                n += 1
        except ShapeError as e:
            raise VWorldError(e.code, f"{KIND_LABEL[kind]}: {e.message}") from None
        sources.append({"kind": kind, "name": KIND_LABEL[kind], "file": Path(src.members[".shp"]).name, "shp_sha256": src.shp_sha256, "dbf_sha256": src.dbf_sha256,
                        "record_count": num_records, "rows_used": n, "fields": [f for f in names if f not in PRIVATE_FIELDS]})
    return attrs, sources
