"""연속지적도 SHP → 필지 번들(.j5parcels.json) 변환 (J5-013B-1, ADR-13).

흐름: 입력(.shp 또는 ZIP) 읽기 → .prj 또는 --crs 로 좌표계 확정 → 레코드마다 PNU·지번 정규화 → WGS84 로 변환 →
      조사 범위(bbox 또는 중심·반경)와 겹치는 필지만 남김 → 같은 PNU 는 MultiPolygon 으로 합침 → 스키마 검증 → 파일 작성.
원본 파일은 수정하지 않는다. 실제 번들은 실데이터 홈에 두고 저장소에 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from j5.parcels.crs import CrsError, GeographicCrs, TmCrs, Transformer, parse_crs_arg, parse_prj
from j5.parcels.shp import ShapeError, ShapeSource, guess_encoding, iter_dbf_records, iter_shapes, open_source, read_dbf_fields, read_shp_header
from j5.schemas_loader import schema_errors

BUNDLE_FORMAT = "1.0.0"
SCHEMA = "parcels_bundle.schema.json"
MAX_FEATURES = 8000          # 스키마 상한. 폰 SVG 성능 시험값(릴리스 계획 §10 배경 필지 3,000개)의 여유
COORD_DECIMALS = 7           # 약 1cm
PNU_RE = re.compile(r"^[0-9]{19}$")
JIBUN_RE = re.compile(r"^\s*(산)?\s*(\d+)(?:\s*-\s*(\d+))?\s*(\D*?)\s*$")
PNU_FIELD_CANDIDATES = ("PNU", "pnu", "A1")
JIBUN_FIELD_CANDIDATES = ("JIBUN", "jibun", "A2")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ConvertError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Clip:
    bbox: tuple[float, float, float, float]          # WGS84 minlon, minlat, maxlon, maxlat
    center: tuple[float, float] | None = None
    radius_m: float | None = None

    @staticmethod
    def from_bbox(text: str) -> "Clip":
        try:
            vals = [float(v) for v in text.split(",")]
        except ValueError:
            raise ConvertError("bad_bbox", f"--bbox 는 minlon,minlat,maxlon,maxlat 형식이어야 한다: {text!r}") from None
        if len(vals) != 4 or not (vals[0] < vals[2] and vals[1] < vals[3]):
            raise ConvertError("bad_bbox", f"--bbox 는 minlon<maxlon, minlat<maxlat 이어야 한다: {text!r}")
        if not (-180 <= vals[0] <= 180 and -180 <= vals[2] <= 180 and -90 <= vals[1] <= 90 and -90 <= vals[3] <= 90):
            raise ConvertError("bad_bbox", f"--bbox 가 경위도 범위를 벗어난다: {text!r}")
        return Clip((vals[0], vals[1], vals[2], vals[3]))

    @staticmethod
    def from_center(text: str, radius_m: float) -> "Clip":
        try:
            lon, lat = (float(v) for v in text.split(","))
        except ValueError:
            raise ConvertError("bad_center", f"--center 는 lon,lat 형식이어야 한다: {text!r}") from None
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ConvertError("bad_center", f"--center 가 경위도 범위를 벗어난다: {text!r}")
        if not (radius_m > 0):
            raise ConvertError("bad_radius", "--radius-m 은 0 보다 커야 한다")
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-6))
        return Clip((lon - dlon, lat - dlat, lon + dlon, lat + dlat), (lon, lat), radius_m)

    def to_dict(self) -> dict:
        return {"bbox": [round(v, COORD_DECIMALS) for v in self.bbox], "center": list(self.center) if self.center else None, "radius_m": self.radius_m}


@dataclass
class ConvertOptions:
    clip: Clip
    geometry_version: str
    source_name: str
    crs_arg: str | None = None
    encoding: str | None = None
    license: str | None = None
    emd_names: dict[str, str] = field(default_factory=dict)
    pnu_field: str | None = None
    jibun_field: str | None = None
    max_features: int = MAX_FEATURES
    data_mode: str = "real"
    layer: str | None = None
    now: datetime | None = None


# ---------------------------------------------------------------- 도형 유틸

def ring_signed_area(ring: list[tuple[float, float]]) -> float:
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _closed(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return ring if ring and ring[0] == ring[-1] else ring + [ring[0]]


def point_in_ring(pt: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = pt
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def rings_to_polygons(rings: list[list[tuple[float, float]]]) -> list[list[list[tuple[float, float]]]]:
    """Shapefile 규칙(외곽 시계방향, 구멍 반시계)으로 링을 폴리곤 단위로 묶고 GeoJSON 방향(외곽 반시계, 구멍 시계)으로 뒤집는다.
    구멍은 링 순서가 아니라 포함 관계로 소속을 정한다(구멍의 꼭짓점을 품는 가장 작은 외곽). 어느 외곽에도 안 들어가는 반시계 링은
    외곽으로 취급한다. 반환: [[outer, hole, ...], ...] (원본 좌표계)."""
    outers: list[tuple[float, list[tuple[float, float]]]] = []   # (면적, 반시계 링)
    holes: list[list[tuple[float, float]]] = []
    for ring in rings:
        r = _closed([p for p in ring])
        if len(r) < 4:
            continue
        area = ring_signed_area(r)
        if area == 0:
            continue
        if area < 0:            # 시계방향 = 외곽 → GeoJSON 방향으로 뒤집음
            outers.append((-area, list(reversed(r))))
        else:                   # 반시계 = 구멍 후보
            holes.append(r)
    polys: list[list[list[tuple[float, float]]]] = [[o] for _, o in outers]
    orphan: list[list[list[tuple[float, float]]]] = []
    for h in holes:
        best = None
        for idx, (area, outer) in enumerate(outers):
            if point_in_ring(h[0], outer) and (best is None or area < outers[best][0]):
                best = idx
        if best is None:
            orphan.append([h])                      # 외곽으로 취급 (이미 반시계)
        else:
            polys[best].append(list(reversed(h)))   # 구멍은 시계방향
    return polys + orphan


def polygons_area(polys: list[list[list[tuple[float, float]]]]) -> float:
    total = 0.0
    for poly in polys:
        for i, ring in enumerate(poly):
            a = abs(ring_signed_area(ring))
            total += a if i == 0 else -a
    return max(total, 0.0)


# ---------------------------------------------------------------- PNU·지번

def normalize_pnu(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if isinstance(value, float) and value.is_integer():
        s = str(int(value))
    return s if PNU_RE.match(s) else None


def parse_jibun(raw) -> dict:
    """원본 JIBUN 문자열(예: '123-4대', '산12임') → {mountain, bon, bu, jimok}. 못 읽으면 값들이 None."""
    out = {"mountain": None, "bon": None, "bu": None, "jimok": None}
    if raw is None:
        return out
    m = JIBUN_RE.match(str(raw))
    if not m:
        return out
    out["mountain"] = m.group(1) == "산"
    out["bon"] = int(m.group(2))
    out["bu"] = int(m.group(3)) if m.group(3) else 0
    out["jimok"] = m.group(4) or None
    return out


def parcel_props(pnu: str, jibun_raw, emd_names: dict[str, str]) -> dict:
    mountain = pnu[10] == "2"
    bon, bu = int(pnu[11:15]), int(pnu[15:19])
    label = ("산" if mountain else "") + str(bon) + (f"-{bu}" if bu else "")
    j = parse_jibun(jibun_raw)
    mismatch = j["bon"] is not None and (j["bon"] != bon or j["bu"] != bu or bool(j["mountain"]) != mountain)
    return {
        "pnu": pnu, "label": label, "emd_code": pnu[:10], "emd_name": emd_names.get(pnu[:10]),
        "mountain": mountain, "bon": bon, "bu": bu, "jimok": j["jimok"],
        "jibun_raw": (str(jibun_raw).strip() or None) if jibun_raw is not None else None, "jibun_mismatch": bool(mismatch),
    }


def _pick_field(names: list[str], override: str | None, candidates: tuple[str, ...], what: str) -> str:
    if override:
        if override not in names:
            raise ConvertError("field_missing", f"{what} 필드 {override!r} 가 .dbf 에 없다. 필드: {names}")
        return override
    for c in candidates:
        if c in names:
            return c
    raise ConvertError("field_missing", f"{what} 필드를 찾지 못했다 (후보 {candidates}). --{what.lower()}-field 로 지정한다. 필드: {names}")


# ---------------------------------------------------------------- 좌표계

def resolve_crs(src: ShapeSource, crs_arg: str | None) -> tuple[TmCrs | GeographicCrs, str]:
    if crs_arg:
        return parse_crs_arg(crs_arg), "argument"
    if src.prj is None:
        raise ConvertError("crs_unknown", ".prj 가 없다. --crs EPSG:xxxx 로 좌표계를 지정한다 (연속지적도는 보통 EPSG:5186 또는 5174)")
    try:
        return parse_prj(src.prj), "prj"
    except CrsError as e:
        raise ConvertError(e.code, e.message) from None


# ---------------------------------------------------------------- inspect

def inspect_source(path: Path, *, crs_arg: str | None = None, encoding: str | None = None, layer: str | None = None, sample: int = 3) -> dict:
    """필드·레코드 수·도형 종류·원본 bbox·좌표계 판정·WGS84 bbox·표본 레코드. 변환 전에 확인용."""
    try:
        src = open_source(path, layer=layer)
    except ShapeError as e:
        raise ConvertError(e.code, e.message) from None
    header = read_shp_header(src.shp)
    fields, num_records, _, _ = read_dbf_fields(src.dbf)
    enc = guess_encoding(src, encoding)
    out = {
        "input": src.origin, "name": src.name, "members": src.members, "shp_sha256": src.shp_sha256, "dbf_sha256": src.dbf_sha256,
        "shape_type": header.shape_type, "bbox_source": list(header.bbox), "record_count": num_records,
        "fields": [{"name": f.name, "type": f.type, "length": f.length, "decimals": f.decimals} for f in fields],
        "encoding": enc, "cpg": src.cpg, "prj": (src.prj or "")[:400] or None, "crs": None, "crs_error": None, "bbox_wgs84": None,
        "pnu_field": None, "jibun_field": None, "samples": [], "sample_errors": [],
    }
    names = [f.name for f in fields]
    for key, cands in (("pnu_field", PNU_FIELD_CANDIDATES), ("jibun_field", JIBUN_FIELD_CANDIDATES)):
        out[key] = next((c for c in cands if c in names), None)
    try:
        crs, how = resolve_crs(src, crs_arg)
        tr = Transformer(crs)
        out["crs"] = {**tr.describe(), "detected_from": how}
        x0, y0, x1, y1 = header.bbox
        lons, lats = zip(*(tr.to_wgs84(x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))))
        out["bbox_wgs84"] = [round(min(lons), COORD_DECIMALS), round(min(lats), COORD_DECIMALS), round(max(lons), COORD_DECIMALS), round(max(lats), COORD_DECIMALS)]
    except (ConvertError, CrsError) as e:
        out["crs_error"] = f"[{e.code}] {e.message}"
    try:
        for i, row in enumerate(iter_dbf_records(src.dbf, enc)):
            if row is not None:
                out["samples"].append(row)
            if len(out["samples"]) >= sample or i >= 50:
                break
    except ShapeError as e:
        out["sample_errors"].append(f"[{e.code}] {e.message}")
    return out


def inspect_text(r: dict) -> str:
    lines = [f"입력: {r['input']} (레코드 {r['record_count']}, shape type {r['shape_type']})",
             f"필드: " + ", ".join(f"{f['name']}({f['type']}{f['length']})" for f in r["fields"]),
             f"문자 인코딩: {r['encoding']}" + (f" (.cpg {r['cpg']})" if r["cpg"] else " (.cpg 없음, 기본값)"),
             f"PNU 필드: {r['pnu_field'] or '못 찾음 (--pnu-field)'} · 지번 필드: {r['jibun_field'] or '못 찾음 (--jibun-field)'}",
             f"원본 bbox: {r['bbox_source']}"]
    if r["crs"]:
        c = r["crs"]
        lines.append(f"좌표계: EPSG:{c['epsg']} {c['name']} ({c['ellipsoid']}, 데이텀 변환 {c['datum_shift'] or '없음'}, {'.prj' if c['detected_from'] == 'prj' else '--crs'} 에서)")
        lines.append(f"WGS84 bbox: {r['bbox_wgs84']}")
    else:
        lines.append(f"좌표계 미확정: {r['crs_error']}")
    for s in r["samples"]:
        lines.append("표본: " + json.dumps(s, ensure_ascii=False))
    for e in r["sample_errors"]:
        lines.append(f"표본 읽기 실패: {e}")
    lines.append("이 출력은 확인용이다. 변환은 `j5 parcels convert` 로 한다.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- convert

def _bbox_of(polys) -> tuple[float, float, float, float]:
    xs = [p[0] for poly in polys for ring in poly for p in ring]
    ys = [p[1] for poly in polys for ring in poly for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _intersects(a, b) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def convert(path: Path, opts: ConvertOptions) -> dict:
    """번들 dict 를 만든다(파일은 쓰지 않음). 스키마 검증까지 통과한 결과만 돌려준다."""
    if not DATE_RE.match(opts.geometry_version or ""):
        raise ConvertError("bad_geometry_version", "--geometry-version 은 YYYY-MM-DD (자료 기준일) 이어야 한다")
    try:
        datetime.strptime(opts.geometry_version, "%Y-%m-%d")
    except ValueError:
        raise ConvertError("bad_geometry_version", f"달력에 없는 날짜: {opts.geometry_version}") from None
    if opts.data_mode not in ("synthetic", "real"):
        raise ConvertError("bad_data_mode", "data_mode 는 synthetic 또는 real")
    if not (1 <= opts.max_features <= MAX_FEATURES):
        raise ConvertError("bad_max_features", f"--max-features 는 1~{MAX_FEATURES} (번들 계약·폰 검증기의 상한과 같다)")
    if not opts.source_name.strip():
        raise ConvertError("bad_source_name", "--source-name 이 필요하다 (예: '연속지적도 서울특별시 종로구')")
    try:
        src = open_source(path, layer=opts.layer)
        fields, num_records, _, _ = read_dbf_fields(src.dbf)
        names = [f.name for f in fields]
        pnu_field = _pick_field(names, opts.pnu_field, PNU_FIELD_CANDIDATES, "PNU")
        jibun_field = _pick_field(names, opts.jibun_field, JIBUN_FIELD_CANDIDATES, "JIBUN") if (opts.jibun_field or any(c in names for c in JIBUN_FIELD_CANDIDATES)) else None
        crs, how = resolve_crs(src, opts.crs_arg)
        tr = Transformer(crs)
        enc = guess_encoding(src, opts.encoding)
        warnings: list[str] = []
        by_pnu: dict[str, dict] = {}
        order: list[str] = []
        stats = {"records": 0, "null_shapes": 0, "deleted": 0, "bad_pnu": 0, "outside": 0, "merged_duplicates": 0, "jibun_mismatch": 0}
        shape_count = sum(1 for _ in iter_shapes(src.shp))
        if shape_count != num_records:
            raise ConvertError("count_mismatch", f".shp 레코드 {shape_count}개와 .dbf 레코드 {num_records}개가 다르다 (짝이 맞지 않는 파일)")
        shapes = iter_shapes(src.shp)
        rows = iter_dbf_records(src.dbf, enc)
        for (rec_no, rings), row in zip(shapes, rows):
            stats["records"] += 1
            if row is None:
                stats["deleted"] += 1
                continue
            if rings is None:
                stats["null_shapes"] += 1
                continue
            pnu = normalize_pnu(row.get(pnu_field))
            if pnu is None:
                stats["bad_pnu"] += 1
                if stats["bad_pnu"] <= 5:
                    warnings.append(f"레코드 {rec_no}: PNU 가 19자리 숫자가 아니다: {row.get(pnu_field)!r}")
                continue
            polys_src = rings_to_polygons(rings)
            if not polys_src:
                stats["null_shapes"] += 1
                continue
            polys = [[[tr.to_wgs84(x, y) for x, y in ring] for ring in poly] for poly in polys_src]
            bbox = _bbox_of(polys)
            if not _intersects(bbox, opts.clip.bbox):
                stats["outside"] += 1
                continue
            area = polygons_area(polys_src) if not crs.geographic else None
            if pnu in by_pnu:
                stats["merged_duplicates"] += 1
                by_pnu[pnu]["polys"].extend(polys)
                if area is not None:
                    by_pnu[pnu]["area"] += area
            else:
                props = parcel_props(pnu, row.get(jibun_field) if jibun_field else None, opts.emd_names)
                if props["jibun_mismatch"]:
                    stats["jibun_mismatch"] += 1
                by_pnu[pnu] = {"polys": polys, "area": area, "props": props}
                order.append(pnu)
            if len(by_pnu) > opts.max_features:
                hint = "범위를 좁힌다" if opts.max_features >= MAX_FEATURES else f"범위를 좁히거나 --max-features 를 올린다 (계약 상한 {MAX_FEATURES})"
                raise ConvertError("too_many_features", f"범위 안 필지가 {opts.max_features}개를 넘는다. {hint}")
    except ShapeError as e:
        raise ConvertError(e.code, e.message) from None
    except CrsError as e:
        raise ConvertError(e.code, e.message) from None

    features = []
    for pnu in order:
        item = by_pnu[pnu]
        polys = [[[[round(x, COORD_DECIMALS), round(y, COORD_DECIMALS)] for x, y in ring] for ring in poly] for poly in item["polys"]]
        geom = {"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1 else {"type": "MultiPolygon", "coordinates": polys}
        bbox = _bbox_of(item["polys"])
        area = None if item["area"] is None else round(item["area"], 1)
        props = {**item["props"], "area_m2_geom": area, "area_missing_reason": None if area is not None else "source_geographic_crs",
                 "bbox": [round(v, COORD_DECIMALS) for v in bbox]}
        features.append({"type": "Feature", "id": pnu, "geometry": geom, "properties": props})
    if crs.geographic:
        warnings.append("원본이 지리좌표라 도형면적(area_m2_geom)을 null (사유 source_geographic_crs) 로 두었다")
    if stats["bad_pnu"] > 5:
        warnings.append(f"PNU 형식 오류 레코드 총 {stats['bad_pnu']}건 (앞 5건만 표시)")
    if stats["jibun_mismatch"]:
        warnings.append(f"JIBUN 과 PNU 번호가 다른 필지 {stats['jibun_mismatch']}건 (PNU 를 라벨로 씀, jibun_mismatch=true)")
    if crs.datum_shift == "korean1985":
        warnings.append("Bessel(Korean 1985) 자료를 EPSG 공식 매개변수로 옮겼다. 공식 정확도 수 m 급이며 지도에서 위치점과 대조한다")
    all_bbox = None
    if features:
        xs0 = [f["properties"]["bbox"] for f in features]
        all_bbox = [min(b[0] for b in xs0), min(b[1] for b in xs0), max(b[2] for b in xs0), max(b[3] for b in xs0)]
    now = opts.now or datetime.now(timezone.utc)
    bundle = {
        "type": "FeatureCollection", "j5parcels": BUNDLE_FORMAT, "data_mode": opts.data_mode,
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {
            "name": opts.source_name.strip(), "file": Path(src.members[".shp"]).name, "shp_sha256": src.shp_sha256, "dbf_sha256": src.dbf_sha256,
            "crs": {**tr.describe(), "detected_from": how}, "encoding": enc, "record_count": num_records,
            "geometry_version": opts.geometry_version, "license": opts.license, "fields": names,
        },
        "clip": opts.clip.to_dict(), "count": len(features), "bbox": all_bbox, "warnings": warnings, "features": features,
    }
    bundle["source"]["crs"].pop("geographic", None)
    bundle["stats"] = stats
    errs = schema_errors(SCHEMA, {k: v for k, v in bundle.items() if k != "stats"})
    if errs:
        raise ConvertError("schema", "만든 번들이 스키마에 맞지 않는다: " + "; ".join(errs[:5]))
    return bundle


def write_bundle(bundle: dict, out: Path) -> dict:
    """번들을 쓴다(덮어쓰기 금지). 다시 읽어 스키마 검증하고 sha256 을 돌려준다."""
    if out.exists():
        raise ConvertError("exists", f"출력 파일이 이미 있다 (덮어쓰지 않음): {out}")
    if not out.name.endswith(".j5parcels.json"):
        raise ConvertError("bad_out_name", "출력 파일 이름은 .j5parcels.json 으로 끝나야 한다")
    doc = {k: v for k, v in bundle.items() if k != "stats"}
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    # 같은 폴더의 임시 파일에 쓰고 다시 읽어 검증한 뒤 원자적으로 교체한다. 중간에 실패하면 최종 이름의 파일은 남지 않는다.
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        back = json.loads(tmp.read_text(encoding="utf-8"))
        errs = schema_errors(SCHEMA, back)
        if errs or back["count"] != len(back["features"]):
            raise ConvertError("verify", "쓴 파일을 다시 읽어 검증하는 데 실패했다: " + "; ".join(errs[:3]))
        if out.exists():
            raise ConvertError("exists", f"출력 파일이 이미 있다 (덮어쓰지 않음): {out}")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": str(out), "bytes": len(text.encode("utf-8")), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def convert_text(bundle: dict, written: dict | None) -> str:
    s = bundle.get("stats", {})
    src = bundle["source"]
    c = src["crs"]
    lines = [f"필지 번들: {bundle['count']}개 (원본 {src['record_count']}개 중 범위 밖 {s.get('outside', 0)}, PNU 오류 {s.get('bad_pnu', 0)}, 빈 도형 {s.get('null_shapes', 0)}, 같은 PNU 합침 {s.get('merged_duplicates', 0)})",
             f"자료: {src['name']} · {src['file']} · 도형 기준일 {src['geometry_version']} · 이용허락 {src['license'] or '미확인(null)'}",
             f"좌표계: EPSG:{c['epsg']} {c['name']} ({c['ellipsoid']}, 데이텀 변환 {c['datum_shift'] or '없음'}) · 인코딩 {src['encoding']}",
             f"범위: {bundle['clip']['bbox']}" + (f" (중심 {bundle['clip']['center']}, 반경 {bundle['clip']['radius_m']} m)" if bundle['clip']['center'] else ""),
             f"data_mode {bundle['data_mode']} · 도형면적은 공부면적이 아니다 · PNU 는 asset_id 가 아니다"]
    for w in bundle.get("warnings", []):
        lines.append(f"  [warn] {w}")
    if written:
        lines.append(f"파일: {written['path']} ({written['bytes']} 바이트, sha256 {written['sha256']})")
        lines.append("폰의 '필지 파일 불러오기' 로 넣는다. 실제 번들은 저장소·공개 배포에 넣지 않는다.")
    return "\n".join(lines) + "\n"
