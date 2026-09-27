"""공공 도형 자료 SHP → 배경 도형 번들(.j5basemap.json) 변환 (J5-022, ADR-16).

층: building(건물 윤곽, 폴리곤) · road_area(실폭도로, 폴리곤) · road(도로 중심선, 폴리라인 + 도로명). 층마다 SHP 하나.
흐름은 필지 변환(convert.py)과 같다: 입력 읽기 → 좌표계 확정 → WGS84 변환 → 조사 범위와 겹치는 도형만 → 스키마 검증 → 파일 작성.
도형과 도로명만 담는다. 건물명·주소·층수·소유 정보는 담지 않는다. 원본은 수정하지 않고 실제 번들은 저장소에 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from j5.parcels.convert import DATE_RE, Clip, ConvertError, _bbox_of, _intersects, resolve_crs, rings_to_polygons
from j5.parcels.crs import CrsError, Transformer
from j5.parcels.shp import POLYGON_TYPES, POLYLINE_TYPES, ShapeError, guess_encoding, iter_dbf_records, iter_shapes, open_source, read_dbf_fields

BUNDLE_FORMAT = "1.0.0"
SCHEMA = "basemap_bundle.schema.json"
LAYERS = ("building", "road_area", "road")
LAYER_LABEL = {"building": "건물 윤곽", "road_area": "실폭도로", "road": "도로 중심선"}
MAX_FEATURES = 20000         # 스키마 상한. 배경은 필지보다 많으므로 여유를 두되 폰 SVG 성능은 실기기 항목
COORD_DECIMALS = 6           # 약 0.1 m (배경은 필지보다 거칠어도 된다)
NAME_MAX = 60
ROAD_NAME_CANDIDATES = ("RN", "ROAD_NM", "RD_NM", "RN_NM", "NAME", "name")


@dataclass
class LayerInput:
    layer: str
    path: Path
    shp_layer: str | None = None       # ZIP 안 .shp 가 여럿일 때 기본 이름
    name_field: str | None = None      # road 층의 도로명 필드 (기본 후보에서 찾음)


@dataclass
class BasemapOptions:
    clip: Clip
    geometry_version: str
    source_name: str
    inputs: list[LayerInput]
    crs_arg: str | None = None
    encoding: str | None = None
    license: str | None = None
    max_features: int = MAX_FEATURES
    data_mode: str = "real"
    now: datetime | None = None


def _round_coords(parts):
    return [[[round(x, COORD_DECIMALS), round(y, COORD_DECIMALS)] for x, y in ring] for ring in parts]


def _line_parts(parts: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """폴리라인 파트에서 점이 2개 미만인 것과 연속 중복점을 정리한다."""
    out = []
    for part in parts:
        pts: list[tuple[float, float]] = []
        for p in part:
            if not pts or pts[-1] != p:
                pts.append(p)
        if len(pts) >= 2:
            out.append(pts)
    return out


def _clean_name(raw) -> str | None:
    if raw is None:
        return None
    s = " ".join(str(raw).split())
    return s[:NAME_MAX] if s else None


def convert_basemap(opts: BasemapOptions) -> dict:
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
        raise ConvertError("bad_source_name", "--source-name 이 필요하다 (예: '도로명주소 전자지도 서울특별시')")
    if not opts.inputs:
        raise ConvertError("no_inputs", "층 입력이 하나도 없다: --buildings, --road-areas, --roads 중 하나 이상")
    seen = set()
    for li in opts.inputs:
        if li.layer not in LAYERS:
            raise ConvertError("bad_layer", f"층 이름은 {LAYERS} 중 하나: {li.layer}")
        if li.layer in seen:
            raise ConvertError("duplicate_layer", f"같은 층을 두 번 넣었다: {li.layer}")
        seen.add(li.layer)

    features: list[dict] = []
    sources: list[dict] = []
    warnings: list[str] = []
    counts = {k: 0 for k in LAYERS}
    stats: dict[str, dict] = {}
    try:
        for li in opts.inputs:
            src = open_source(li.path, layer=li.shp_layer)
            fields, num_records, _, _ = read_dbf_fields(src.dbf)
            names = [f.name for f in fields]
            crs, how = resolve_crs(src, opts.crs_arg)
            tr = Transformer(crs)
            enc = guess_encoding(src, opts.encoding)
            kinds = POLYLINE_TYPES if li.layer == "road" else POLYGON_TYPES
            name_field = None
            if li.layer == "road":
                if li.name_field:
                    if li.name_field not in names:
                        raise ConvertError("field_missing", f"도로명 필드 {li.name_field!r} 가 .dbf 에 없다. 필드: {names}")
                    name_field = li.name_field
                else:
                    name_field = next((c for c in ROAD_NAME_CANDIDATES if c in names), None)
                    if name_field is None:
                        warnings.append(f"road: 도로명 필드를 찾지 못해 이름 없이 넣었다 (후보 {ROAD_NAME_CANDIDATES}, --road-name-field 로 지정). 필드: {names}")
            st = {"records": 0, "null_shapes": 0, "deleted": 0, "outside": 0, "kept": 0}
            shape_count = sum(1 for _ in iter_shapes(src.shp, kinds))
            if shape_count != num_records:
                raise ConvertError("count_mismatch", f"{li.layer}: .shp 레코드 {shape_count}개와 .dbf 레코드 {num_records}개가 다르다 (짝이 맞지 않는 파일)")
            for (rec_no, parts), row in zip(iter_shapes(src.shp, kinds), iter_dbf_records(src.dbf, enc)):
                st["records"] += 1
                if row is None:
                    st["deleted"] += 1
                    continue
                if parts is None:
                    st["null_shapes"] += 1
                    continue
                if li.layer == "road":
                    lines_src = _line_parts(parts)
                    if not lines_src:
                        st["null_shapes"] += 1
                        continue
                    lines = [[tr.to_wgs84(x, y) for x, y in part] for part in lines_src]
                    bbox = _bbox_of([lines])
                    if not _intersects(bbox, opts.clip.bbox):
                        st["outside"] += 1
                        continue
                    coords = _round_coords(lines)
                    geom = {"type": "LineString", "coordinates": coords[0]} if len(coords) == 1 else {"type": "MultiLineString", "coordinates": coords}
                    name = _clean_name(row.get(name_field)) if name_field else None
                else:
                    polys_src = rings_to_polygons(parts)
                    if not polys_src:
                        st["null_shapes"] += 1
                        continue
                    polys = [[[tr.to_wgs84(x, y) for x, y in ring] for ring in poly] for poly in polys_src]
                    bbox = _bbox_of(polys)
                    if not _intersects(bbox, opts.clip.bbox):
                        st["outside"] += 1
                        continue
                    coords = [_round_coords(poly) for poly in polys]
                    geom = {"type": "Polygon", "coordinates": coords[0]} if len(coords) == 1 else {"type": "MultiPolygon", "coordinates": coords}
                    name = None
                st["kept"] += 1
                counts[li.layer] += 1
                features.append({"type": "Feature", "id": f"{li.layer}:{rec_no}", "geometry": geom,
                                 "properties": {"layer": li.layer, "name": name, "bbox": [round(v, COORD_DECIMALS) for v in bbox]}})
                if len(features) > opts.max_features:
                    hint = "범위를 좁힌다" if opts.max_features >= MAX_FEATURES else f"범위를 좁히거나 --max-features 를 올린다 (계약 상한 {MAX_FEATURES})"
                    raise ConvertError("too_many_features", f"범위 안 배경 도형이 {opts.max_features}개를 넘는다. {hint}")
            if crs.datum_shift == "korean1985":
                warnings.append(f"{li.layer}: Bessel(Korean 1985) 자료를 EPSG 공식 매개변수로 옮겼다. 공식 정확도 수 m 급이며 지도에서 필지·위치점과 대조한다")
            stats[li.layer] = st
            sources.append({
                "layer": li.layer, "name": opts.source_name.strip(), "file": Path(src.members[".shp"]).name, "shp_sha256": src.shp_sha256, "dbf_sha256": src.dbf_sha256,
                "crs": {k: v for k, v in {**tr.describe(), "detected_from": how}.items() if k != "geographic"}, "encoding": enc, "record_count": num_records,
                "geometry_version": opts.geometry_version, "license": opts.license, "fields": names, "name_field": name_field,
            })
    except ShapeError as e:
        raise ConvertError(e.code, e.message) from None
    except CrsError as e:
        raise ConvertError(e.code, e.message) from None

    all_bbox = None
    if features:
        bbs = [f["properties"]["bbox"] for f in features]
        all_bbox = [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]
    now = opts.now or datetime.now(timezone.utc)
    bundle = {
        "type": "FeatureCollection", "j5basemap": BUNDLE_FORMAT, "data_mode": opts.data_mode,
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sources": sources, "clip": opts.clip.to_dict(), "count": len(features), "counts": counts, "bbox": all_bbox, "warnings": warnings, "features": features,
    }
    from j5.schemas_loader import schema_errors
    errs = schema_errors(SCHEMA, bundle)
    if errs:
        raise ConvertError("schema", "만든 번들이 스키마에 맞지 않는다: " + "; ".join(errs[:5]))
    bundle["stats"] = stats
    return bundle


def write_basemap(bundle: dict, out: Path) -> dict:
    """번들을 쓴다(덮어쓰기 금지). 다시 읽어 스키마 검증하고 sha256 을 돌려준다."""
    from j5.schemas_loader import schema_errors
    if out.exists():
        raise ConvertError("exists", f"출력 파일이 이미 있다 (덮어쓰지 않음): {out}")
    if not out.name.endswith(".j5basemap.json"):
        raise ConvertError("bad_out_name", "출력 파일 이름은 .j5basemap.json 으로 끝나야 한다")
    doc = {k: v for k, v in bundle.items() if k != "stats"}
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        back = json.loads(tmp.read_text(encoding="utf-8"))
        errs = schema_errors(SCHEMA, back)
        if errs or back["count"] != len(back["features"]) or sum(back["counts"].values()) != back["count"]:
            raise ConvertError("verify", "쓴 파일을 다시 읽어 검증하는 데 실패했다: " + "; ".join(errs[:3]))
        if out.exists():
            raise ConvertError("exists", f"출력 파일이 이미 있다 (덮어쓰지 않음): {out}")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": str(out), "bytes": len(text.encode("utf-8")), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def basemap_text(bundle: dict, written: dict | None) -> str:
    c = bundle["counts"]
    st = bundle.get("stats", {})
    lines = [f"배경 번들: 도형 {bundle['count']}개 (건물 {c['building']} · 실폭도로 {c['road_area']} · 도로 중심선 {c['road']})"]
    for s in bundle["sources"]:
        t = st.get(s["layer"], {})
        crs = s["crs"]
        lines.append(f"  {LAYER_LABEL[s['layer']]}: {s['file']} (원본 {s['record_count']}개 중 범위 밖 {t.get('outside', 0)}, 빈 도형 {t.get('null_shapes', 0)}) · EPSG:{crs['epsg']} {crs['name']} · 인코딩 {s['encoding']}"
                     + (f" · 도로명 필드 {s['name_field']}" if s["layer"] == "road" else ""))
    s0 = bundle["sources"][0]
    lines.append(f"자료: {s0['name']} · 도형 기준일 {s0['geometry_version']} · 이용허락 {s0['license'] or '미확인(null)'}")
    lines.append(f"범위: {bundle['clip']['bbox']}" + (f" (중심 {bundle['clip']['center']}, 반경 {bundle['clip']['radius_m']} m)" if bundle['clip']['center'] else ""))
    lines.append(f"data_mode {bundle['data_mode']} · 도형과 도로명만 담는다 (건물명·주소·소유 정보 없음) · 배경일 뿐 정본이 아니다")
    for w in bundle.get("warnings", []):
        lines.append(f"  [warn] {w}")
    if written:
        lines.append(f"파일: {written['path']} ({written['bytes']} 바이트, sha256 {written['sha256']})")
        lines.append("폰 설정의 자료 관리 → '배경 지도 파일 선택' 으로 넣는다. 실제 번들은 저장소·공개 배포에 넣지 않는다.")
    return "\n".join(lines) + "\n"
