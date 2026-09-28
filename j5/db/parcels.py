"""정본 필지·물건 구성 (J5-013B-2). 데이터 사전 §2 parcels / asset_components, §6, ADR-13.

- `load_bundle`: `j5 parcels convert` 가 만든 번들(.j5parcels.json)의 필지를 정본 `parcels` 에 반영한다. PNU 가 외부 ID, parcel_id 가 내부 UUID.
  같은 PNU·같은 내용은 변화 없음, 더 새로운 도형 기준일이면 갱신(도형 버전 교체), 같은 기준일·다른 내용은 거절, 더 오래된 기준일은 거절.
  번들의 data_mode(synthetic/real)는 정본(synthetic/private_real)과 맞아야 한다. 반영한 번들은 source_documents 에 출처로 남긴다.
  번들에 필지 속성(properties.attrs, J5-025·ADR-19)이 있으면 `parcel_attributes` 에 필지마다 한 행으로 넣는다: 같은 내용은 변화 없음, 기준일(as_of = 번들 도형 기준일)이
  같거나 새로우면 갱신(속성은 소유 변동처럼 도형과 별개로 바뀐다), 더 오래된 기준일은 거절. 공부면적은 parcels.registered_area_m2 에도 채운다.
- `suggest_links`: 물건의 위치점을 품는 필지를 찾아 연결 제안 파일(asset_components_input)을 만든다. 제안은 정본에 쓰지 않는다.
- `apply_links`: 검토한 연결 파일을 정본 `asset_components` 에 반영한다(물건·필지가 정본에 있어야 한다). 같은 (물건, 필지, 시작일)은 갱신.
정본 변경이 있으면 dataset_version 을 1 올린다. 필지 번들·연결 파일은 정본 시각을 정하지 못한다(recorded_at 은 저장소가 부여).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from j5.db.store import Db, DbError
from j5.db.validate import ValidationError, parse_date
from j5.parcels.convert import MAX_FEATURES, point_in_ring

CANONICAL_MAX_PARCELS = 100_000   # 정본 상한 (J5-039, ADR-23): 여러 지역을 담는다. 폰 파생본은 MAX_FEATURES 와 폰 범위로 따로 줄인다
PHONE_SCOPE_KEY = "phone_parcel_scope"
from j5.schemas_loader import schema_errors

BUNDLE_SCHEMA = "parcels_bundle.schema.json"
BESSEL_WARNING = "Bessel(Korean 1985) 자료를 EPSG 공식 매개변수로 옮겼다. 공식 정확도 수 m 급이며 지도에서 위치점과 대조한다"
LINKS_SCHEMA = "asset_components_input.schema.json"
BUNDLE_MODE_TO_DB = {"synthetic": "synthetic", "real": "private_real"}


@dataclass
class ParcelLoadResult:
    outcome: str = "unchanged"          # applied / unchanged
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    attrs_inserted: int = 0             # J5-025 필지 속성
    attrs_updated: int = 0
    attrs_unchanged: int = 0
    snapshots_inserted: int = 0         # J5-026 기준일별 스냅샷 (자료 종류마다)
    snapshots_replaced: int = 0         # 같은 (필지, 종류, 기준일) 에 다른 값 → 값 교체
    dataset_version: int = 0
    source_document_id: str | None = None
    source_name: str = ""
    geometry_version: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged,
                "attrs_inserted": self.attrs_inserted, "attrs_updated": self.attrs_updated, "attrs_unchanged": self.attrs_unchanged,
                "snapshots_inserted": self.snapshots_inserted, "snapshots_replaced": self.snapshots_replaced, "dataset_version": self.dataset_version,
                "source_document_id": self.source_document_id, "source_name": self.source_name, "geometry_version": self.geometry_version, "message": self.message}

    def to_text(self) -> str:
        attrs = f" · 필지 속성 신규 {self.attrs_inserted}, 갱신 {self.attrs_updated}, 변화 없음 {self.attrs_unchanged}" if (self.attrs_inserted or self.attrs_updated or self.attrs_unchanged) else ""
        if self.snapshots_inserted or self.snapshots_replaced:
            attrs += f" · 속성 스냅샷 신규 {self.snapshots_inserted}, 같은 기준일 교체 {self.snapshots_replaced}"
        return (f"필지 반영: {'반영됨' if self.outcome == 'applied' else '변화 없음'} · 신규 {self.inserted}, 갱신 {self.updated}, 변화 없음 {self.unchanged}{attrs}"
                f" · dataset_version {self.dataset_version} · {self.source_name} (도형 기준일 {self.geometry_version})\n{self.message}\n")


@dataclass
class LinkResult:
    outcome: str = "unchanged"
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    dataset_version: int = 0
    ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged, "dataset_version": self.dataset_version, "ids": self.ids}

    def to_text(self) -> str:
        return (f"물건↔필지 연결: {'반영됨' if self.outcome == 'applied' else '변화 없음'} · 신규 {self.inserted}, 갱신 {self.updated}, 변화 없음 {self.unchanged}"
                f" · dataset_version {self.dataset_version}\n")


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(obj) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def load_json(path: Path, schema: str) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValidationError([f"입력 파일을 읽을 수 없다: {e}"]) from e
    errs = schema_errors(schema, doc)
    if errs:
        raise ValidationError([f"입력이 {schema} 에 맞지 않는다"] + errs[:10])
    return doc


# ---------------------------------------------------------------- 필지 반영

def _parcel_content(feature: dict, src: dict) -> dict:
    p = feature["properties"]
    return {"pnu": feature["id"], "label": p["label"], "emd_code": p["emd_code"], "emd_name": p["emd_name"], "mountain": p["mountain"], "bon": p["bon"], "bu": p["bu"],
            "jimok": p["jimok"], "jibun_raw": p["jibun_raw"], "jibun_mismatch": p["jibun_mismatch"], "geom_area_m2": p["area_m2_geom"],
            "geom_area_missing_reason": p["area_missing_reason"], "geometry": feature["geometry"], "bbox": p["bbox"], "geometry_version": src["geometry_version"],
            "source_name": src["name"], "source_crs": f"EPSG:{src['crs']['epsg']}" if src["crs"].get("epsg") else src["crs"]["name"],
            "source_ellipsoid": src["crs"]["ellipsoid"], "source_datum_shift": src["crs"]["datum_shift"],
            "source_shp_sha256": src["shp_sha256"], "source_license": src["license"]}


ATTR_FIELDS = ("jimok_name", "registered_area_m2", "official_land_price_krw_m2", "price_base_year", "price_base_month", "use_zone_1", "use_zone_2", "land_use_situation",
               "road_side", "terrain_height", "terrain_form", "plan_zones", "plan_zone_names", "plan_zones_truncated", "ownership_kind_code", "ownership_kind", "co_owner_count",
               "ownership_changed_on", "ownership_change_cause_code", "national_institution_code")
# 목록·불리언처럼 열이 따로인 필드 (행 변환에서 따로 다룬다). plan_zone_names(J5-032)는 비어 있으면 내용에서 뺀다:
# 그 키가 없던 J5-025·026 정본 행·스냅샷과 해시·값이 같게 유지되어, 옛 번들을 다시 넣어도 헛된 갱신이 생기지 않는다
_LIST_FIELDS = ("plan_zones", "plan_zone_names", "plan_zones_truncated")


# 자료 종류별 필드 묶음 (리뷰 반영, PR #71): 번들에 든 자료(attrs_sources.kind)의 묶음만 반영하고 없는 자료의 값은 기존 행을 유지한다.
# 자료가 없는 것은 "값이 null 이라는 관측" 이 아니다. 자료가 있는데 그 필지의 행이 없으면 null 이다.
ATTR_GROUPS = {
    "land_feature": ("jimok_name", "registered_area_m2", "official_land_price_krw_m2", "price_base_year", "price_base_month", "use_zone_1", "use_zone_2", "land_use_situation",
                     "road_side", "terrain_height", "terrain_form"),
    "land_plan": ("plan_zones", "plan_zone_names", "plan_zones_truncated"),
    "land_ownership": ("ownership_kind_code", "ownership_kind", "co_owner_count", "ownership_changed_on", "ownership_change_cause_code", "national_institution_code"),
}


def _empty_attrs() -> dict:
    c = {k: None for k in ATTR_FIELDS if k != "plan_zone_names"}
    c["plan_zones"] = []
    c["plan_zones_truncated"] = False
    return c


def _attrs_content(attrs: dict, as_of: str, kinds: set[str], base: dict | None = None, corrected_names: list | None = None) -> dict:
    """번들 attrs → 정본 행 내용. kinds 에 든 자료 묶음의 필드만 번들 값으로 두고 나머지는 base(기존 행) 또는 null. 해시는 이 내용으로 계산한다.
    corrected_names 는 같은 기준일 토지이용계획 스냅샷의 정정된 이름 목록(빈 목록 포함, 없으면 None, _corrected_plan_names)이다."""
    c = dict(base) if base else _empty_attrs()
    for kind in kinds:
        for k in ATTR_GROUPS[kind]:
            c[k] = attrs.get(k)
    if "land_plan" in kinds and "plan_zone_names" not in attrs:
        # J5-025 형식(이름 목록 키 없음, 코드에 이름이 위치로 붙음)의 옛 번들 (리뷰 반영 PR #79): 위치 짝을 되살리지 않는다.
        # 코드에 붙은 이름을 떼어 순서대로 이름 목록으로 옮기고(잘림 표시가 있으면 끊긴 마지막 조각은 뺀다), 같은 기준일에 정정된 이름 목록이 이미 있으면
        # 빈 목록이어도 그것을 지킨다 (리뷰 반영 PR #80: 판정은 행 전체의 as_of 가 아니라 같은 기준일의 토지이용계획 스냅샷으로 한다)
        zones = attrs.get("plan_zones") or []
        legacy = [z["name"] for z in zones if isinstance(z, dict) and z.get("name")]
        if attrs.get("plan_zones_truncated") and legacy:
            legacy = legacy[:-1]
        c["plan_zone_names"] = list(corrected_names) if corrected_names is not None else legacy
        c["plan_zones"] = [dict(z, name=None) for z in zones]
    c["plan_zones"] = c["plan_zones"] or []
    names = c.pop("plan_zone_names", None) or []
    if names:
        c["plan_zone_names"] = list(names)
    c["plan_zones_truncated"] = bool(c["plan_zones_truncated"])
    c["as_of"] = as_of
    return c


def _corrected_plan_names(db: Db, pnu: str, as_of: str) -> list | None:
    """같은 기준일 토지이용계획 스냅샷이 정정된 형식(코드에 이름을 붙이지 않음, J5-032)이면 그 이름 목록(키가 없으면 빈 목록), 아니면 None.
    옛 형식 스냅샷(J5-025·026, 코드에 이름이 붙음)이나 그 기준일의 스냅샷이 없으면 정정된 값이 없는 것이다."""
    row = db.conn.execute("SELECT s.values_json FROM parcel_attribute_snapshots s JOIN parcels p ON p.parcel_id = s.parcel_id"
                          " WHERE p.pnu = ? AND s.kind = 'land_plan' AND s.as_of = ?", (pnu, as_of)).fetchone()
    if row is None:
        return None
    values = json.loads(row["values_json"])
    if any(isinstance(z, dict) and z.get("name") for z in values.get("plan_zones") or []):
        return None
    return list(values.get("plan_zone_names") or [])


def _attrs_content_from_row(row) -> dict:
    c = {k: row[k] for k in ATTR_FIELDS if k not in _LIST_FIELDS}
    c["plan_zones"] = json.loads(row["plan_zones_json"])
    names = json.loads(row["plan_zone_names_json"]) if "plan_zone_names_json" in row.keys() else []
    if names:
        c["plan_zone_names"] = names
    c["plan_zones_truncated"] = bool(row["plan_zones_truncated"])
    c["as_of"] = row["as_of"]
    return c


def _attrs_row(parcel_id: str, content: dict, h: str, sources: list[dict], doc_id: str | None, now: str) -> dict:
    row = {k: content.get(k) for k in ATTR_FIELDS if k not in _LIST_FIELDS}
    row.update({"parcel_id": parcel_id, "as_of": content["as_of"], "plan_zones_json": _canon(content["plan_zones"]), "plan_zone_names_json": _canon(content.get("plan_zone_names") or []),
                "plan_zones_truncated": int(content["plan_zones_truncated"]),
                "sources_json": _canon(sources), "source_document_id": doc_id, "content_hash": h, "now": now})
    return row


_ATTR_COLS = ("as_of", "jimok_name", "registered_area_m2", "official_land_price_krw_m2", "price_base_year", "price_base_month", "use_zone_1", "use_zone_2", "land_use_situation",
              "road_side", "terrain_height", "terrain_form", "plan_zones_json", "plan_zone_names_json", "plan_zones_truncated", "ownership_kind_code", "ownership_kind", "co_owner_count",
              "ownership_changed_on", "ownership_change_cause_code", "national_institution_code", "sources_json", "source_document_id", "content_hash")
_ATTR_INSERT = (f"INSERT INTO parcel_attributes (parcel_id, {', '.join(_ATTR_COLS)}, recorded_at, updated_at)"
                f" VALUES (:parcel_id, {', '.join(':' + c for c in _ATTR_COLS)}, :now, :now)")
_ATTR_UPDATE = f"UPDATE parcel_attributes SET {', '.join(f'{c} = :{c}' for c in _ATTR_COLS)}, updated_at = :now WHERE parcel_id = :parcel_id"


_SOURCE_KEYS = ("source_name", "source_crs", "source_ellipsoid", "source_datum_shift", "source_shp_sha256", "source_license")


def _parcel_core(content: dict) -> dict:
    """출처 표기를 뺀 필지 내용 (도형·지번·면적). 같은 기준일 충돌 판정에 쓴다."""
    return {k: v for k, v in content.items() if k not in _SOURCE_KEYS}


def _parcel_content_from_row(row) -> dict:
    return {"pnu": row["pnu"] if "pnu" in row.keys() else None, "label": row["label"], "emd_code": row["emd_code"], "emd_name": row["emd_name"], "mountain": bool(row["mountain"]),
            "bon": row["bon"], "bu": row["bu"], "jimok": row["jimok"], "jibun_raw": row["jibun_raw"], "jibun_mismatch": bool(row["jibun_mismatch"]), "geom_area_m2": row["geom_area_m2"],
            "geom_area_missing_reason": row["geom_area_missing_reason"], "geometry": json.loads(row["geometry_json"]), "bbox": json.loads(row["bbox_json"]), "geometry_version": row["geometry_version"]}


def load_bundle(db: Db, doc: dict) -> ParcelLoadResult:
    errs = schema_errors(BUNDLE_SCHEMA, doc)
    if errs:
        raise ValidationError(errs[:10])
    if parse_date(doc["source"]["geometry_version"]) is None:
        raise ValidationError([f"달력상 존재하지 않는 도형 기준일: {doc['source']['geometry_version']}"])
    if doc["count"] != len(doc["features"]):
        raise ValidationError([f"count({doc['count']})와 features 수({len(doc['features'])})가 다르다"])
    ids = [f["id"] for f in doc["features"]]
    if len(set(ids)) != len(ids):
        raise ValidationError(["번들 안에 같은 PNU 가 두 번 있다"])
    want = BUNDLE_MODE_TO_DB[doc["data_mode"]]
    if want != db.data_mode:
        raise DbError("data_mode_mismatch", f"번들 data_mode {doc['data_mode']} 는 정본 {db.data_mode} 에 넣지 않는다")
    src = doc["source"]
    r = ParcelLoadResult(source_name=src["name"], geometry_version=src["geometry_version"])
    now = db.now()
    with db.transaction():
        doc_id = str(uuid.uuid4())
        # 1) 계획: 필지마다 신규·변화 없음·갱신·거절을 정한다 (거절이 하나라도 있으면 전체 반영하지 않는다)
        plan = []
        for feature in doc["features"]:
            content = _parcel_content(feature, src)
            h = _hash(content)
            cur = db.conn.execute("SELECT parcel_id, pnu, content_hash, geometry_version, label, emd_code, emd_name, mountain, bon, bu, jimok, jibun_raw, jibun_mismatch,"
                                  " geom_area_m2, geom_area_missing_reason, geometry_json, bbox_json FROM parcels WHERE pnu = ?", (feature["id"],)).fetchone()
            if cur is None:
                plan.append(("insert", content, h, None))
            elif cur["content_hash"] == h:
                plan.append(("unchanged", content, h, cur))
            elif content["geometry_version"] > cur["geometry_version"]:
                plan.append(("update", content, h, cur))
            elif content["geometry_version"] == cur["geometry_version"]:
                # 같은 기준일에 필지 내용(도형·지번)은 같고 출처 표기(자료명·파일 해시)만 다르면 변화 없음으로 둔다: 구역을 나눠 내려받은 자료는 경계의 필지가 양쪽에 들어 있다 (J5-025 실측)
                if _parcel_core(content) == _parcel_core(_parcel_content_from_row(cur)):
                    plan.append(("unchanged", content, h, cur))
                else:
                    raise DbError("parcel_conflict", f"PNU {feature['id']}: 같은 도형 기준일({cur['geometry_version']})인데 내용이 다르다. 새 기준일의 자료로 다시 변환하거나 원본을 확인한다")
            else:
                raise DbError("parcel_older", f"PNU {feature['id']}: 번들 기준일 {content['geometry_version']} 이 정본의 {cur['geometry_version']} 보다 오래됐다. 반영하지 않는다")
        # 1b) 필지 속성(J5-025): 필지마다 신규·변화 없음·갱신·거절. as_of 는 attrs.as_of(파생본) 또는 번들 도형 기준일. 정본 parcel_id 는 필지 반영 뒤에 정해지므로 PNU 로 둔다.
        #     번들에 든 자료 묶음(attrs_sources.kind)만 반영하고, 없는 자료의 값과 출처는 기존 행을 유지한다 (attrs_sources 가 없는 번들은 세 묶음 모두로 본다)
        attrs_plan = []
        attrs_sources = doc.get("attrs_sources") or []
        kinds = {a["kind"] for a in attrs_sources} or set(ATTR_GROUPS)
        for feature in doc["features"]:
            attrs = feature["properties"].get("attrs")
            if not attrs:
                continue
            as_of = attrs.get("as_of") or src["geometry_version"]
            cur = db.conn.execute("SELECT a.* FROM parcel_attributes a JOIN parcels p ON p.parcel_id = a.parcel_id WHERE p.pnu = ?", (feature["id"],)).fetchone()
            corrected = _corrected_plan_names(db, feature["id"], as_of) if "land_plan" in kinds and "plan_zone_names" not in attrs else None
            if cur is None:
                content = _attrs_content(attrs, as_of, kinds, corrected_names=corrected)
                sources = list(attrs_sources)
                attrs_plan.append(("insert", feature["id"], content, _hash(content), sources))
                continue
            if as_of < cur["as_of"]:
                raise DbError("parcel_attrs_older", f"PNU {feature['id']}: 속성 기준일 {as_of} 이 정본의 {cur['as_of']} 보다 오래됐다. 반영하지 않는다")
            content = _attrs_content(attrs, as_of, kinds, base=_attrs_content_from_row(cur), corrected_names=corrected)
            h = _hash(content)
            sources = [s_ for s_ in json.loads(cur["sources_json"]) if s_["kind"] not in kinds] + list(attrs_sources)
            if cur["content_hash"] == h:
                attrs_plan.append(("unchanged", feature["id"], content, h, sources))   # 같은 값이면 출처 파일이 달라도 변화 없음 (필지 규칙과 같다)
            else:
                attrs_plan.append(("update", feature["id"], content, h, sources))
        # 1c) 속성 스냅샷(J5-026): 번들에 든 자료 종류마다 (필지, 종류, 기준일) 한 행. 없으면 신규, 같은 값이면 그대로, 같은 기준일에 다른 값이면 교체(같은 날짜의 재다운로드).
        #     값이 안 바뀐 새 기준일도 신규 스냅샷이다("그 날짜에도 같았다" 는 기록). 현재 값(parcel_attributes)이 변화 없음이어도 스냅샷이 늘면 정본은 바뀐 것이다
        source_by_kind = {a["kind"]: a for a in attrs_sources}
        snap_plan = []
        for _action, pnu, content, _h, _sources in attrs_plan:
            for kind in sorted(kinds):
                values = {k: content[k] for k in ATTR_GROUPS[kind] if k in content}   # 빈 이름 목록은 키 없음 (_LIST_FIELDS 설명)
                row = db.conn.execute("SELECT s.snapshot_id, s.values_json FROM parcel_attribute_snapshots s JOIN parcels p ON p.parcel_id = s.parcel_id WHERE p.pnu = ? AND s.kind = ? AND s.as_of = ?",
                                      (pnu, kind, content["as_of"])).fetchone()
                if row is None:
                    snap_plan.append(("insert", pnu, kind, content["as_of"], values, None))
                elif json.loads(row["values_json"]) != values:
                    snap_plan.append(("replace", pnu, kind, content["as_of"], values, row["snapshot_id"]))
        changed = any(a in ("insert", "update") for a, *_ in plan) or any(a in ("insert", "update") for a, *_ in attrs_plan) or bool(snap_plan)
        n_insert = sum(1 for a, *_ in plan if a == "insert")
        total_after = db.conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0] + n_insert
        if total_after > CANONICAL_MAX_PARCELS:
            raise DbError("parcels_limit", f"반영 후 정본 필지가 {total_after}개로 정본 상한 {CANONICAL_MAX_PARCELS}개를 넘는다. 조사 범위를 좁힌 번들로 다시 만든다 (정본은 바꾸지 않았다)")
        # 2) 출처 문서를 먼저 남기고(필지 행이 참조), 필지를 반영한다
        if changed:
            db.add_source_document({"document_id": doc_id, "document_kind": "official_file", "title": f"{src['name']} (도형 기준일 {src['geometry_version']}, {src['file']})",
                                    "terms": src["license"], "location": src["file"], "sha256": src["shp_sha256"], "source_published_at": src["geometry_version"],
                                    "collected_at": doc["generated_at"],
                                    "notes": f"j5parcels {doc['j5parcels']} · EPSG:{src['crs'].get('epsg')} · {doc['count']}필지"
                                             + (" · 필지 속성: " + ", ".join(f"{a['name']} ({a['dbf_sha256'][:12]})" for a in attrs_sources) if attrs_sources and attrs_plan else "")})
        for action, content, h, cur in plan:
            if action == "unchanged":
                r.unchanged += 1
                continue
            row = {**content, "mountain": int(content["mountain"]), "jibun_mismatch": int(content["jibun_mismatch"]), "geometry_json": _canon(content["geometry"]),
                   "bbox_json": _canon(content["bbox"]), "content_hash": h, "now": now, "data_mode": db.data_mode, "source_document_id": doc_id}
            if action == "insert":
                pid = str(uuid.uuid4())
                db.conn.execute("INSERT INTO subjects (subject_id, subject_type, recorded_at) VALUES (?, 'parcel', ?)", (pid, now))
                db.conn.execute(
                    "INSERT INTO parcels (parcel_id, pnu, label, emd_code, emd_name, mountain, bon, bu, jimok, jibun_raw, jibun_mismatch,"
                    " registered_area_m2, registered_area_missing_reason, geom_area_m2, geom_area_missing_reason, geometry_json, geometry_version, bbox_json,"
                    " source_name, source_crs, source_ellipsoid, source_datum_shift, source_shp_sha256, source_license, source_document_id, data_mode, content_hash, recorded_at, updated_at)"
                    " VALUES (:parcel_id, :pnu, :label, :emd_code, :emd_name, :mountain, :bon, :bu, :jimok, :jibun_raw, :jibun_mismatch,"
                    " NULL, 'not_collected', :geom_area_m2, :geom_area_missing_reason, :geometry_json, :geometry_version, :bbox_json,"
                    " :source_name, :source_crs, :source_ellipsoid, :source_datum_shift, :source_shp_sha256, :source_license, :source_document_id, :data_mode, :content_hash, :now, :now)",
                    {**row, "parcel_id": pid})
                r.inserted += 1
            else:
                db.conn.execute(
                    "UPDATE parcels SET label = :label, emd_code = :emd_code, emd_name = :emd_name, mountain = :mountain, bon = :bon, bu = :bu, jimok = :jimok,"
                    " jibun_raw = :jibun_raw, jibun_mismatch = :jibun_mismatch, geom_area_m2 = :geom_area_m2, geom_area_missing_reason = :geom_area_missing_reason,"
                    " geometry_json = :geometry_json, geometry_version = :geometry_version, bbox_json = :bbox_json, source_name = :source_name, source_crs = :source_crs,"
                    " source_ellipsoid = :source_ellipsoid, source_datum_shift = :source_datum_shift, source_shp_sha256 = :source_shp_sha256, source_license = :source_license, source_document_id = :source_document_id, content_hash = :content_hash,"
                    " updated_at = :now WHERE pnu = :pnu", row)
                r.updated += 1
        # 3) 필지 속성: 필지 행이 모두 있는 상태에서 넣는다. 공부면적이 있으면 parcels.registered_area_m2 도 채운다
        for action, pnu, content, h, sources in attrs_plan:
            if action == "unchanged":
                r.attrs_unchanged += 1
                continue
            pid = db.conn.execute("SELECT parcel_id FROM parcels WHERE pnu = ?", (pnu,)).fetchone()["parcel_id"]
            db.conn.execute(_ATTR_INSERT if action == "insert" else _ATTR_UPDATE, _attrs_row(pid, content, h, sources, doc_id, now))
            if "land_feature" in kinds:   # 공부면적 미러: 토지특성 자료가 든 번들만 바꾼다. 값이 없으면 null + not_collected 로 되돌린다 (리뷰 반영)
                area = content["registered_area_m2"]
                db.conn.execute("UPDATE parcels SET registered_area_m2 = ?, registered_area_missing_reason = ?, updated_at = ? WHERE parcel_id = ?",
                                (area, None if area is not None else "not_collected", now, pid))
            if action == "insert":
                r.attrs_inserted += 1
            else:
                r.attrs_updated += 1
        for action, pnu, kind, as_of, values, sid in snap_plan:
            pid = db.conn.execute("SELECT parcel_id FROM parcels WHERE pnu = ?", (pnu,)).fetchone()["parcel_id"]
            src_json = _canon(source_by_kind[kind]) if kind in source_by_kind else None
            if action == "insert":
                db.conn.execute("INSERT INTO parcel_attribute_snapshots (snapshot_id, parcel_id, kind, as_of, values_json, source_json, source_document_id, recorded_at, updated_at)"
                                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), pid, kind, as_of, _canon(values), src_json, doc_id, now, now))
                r.snapshots_inserted += 1
            else:
                db.conn.execute("UPDATE parcel_attribute_snapshots SET values_json = ?, source_json = ?, source_document_id = ?, updated_at = ? WHERE snapshot_id = ?",
                                (_canon(values), src_json, doc_id, now, sid))
                r.snapshots_replaced += 1
        if changed:
            r.source_document_id = doc_id
            r.outcome = "applied"
            r.dataset_version = db.bump_dataset_version()
            r.message = "필지는 파생본(parcels.geojson)에 포함된다. 물건 연결은 parcels-suggest → 검토 → parcels-link 로 한다"
        else:
            r.dataset_version = int(db.meta("dataset_version") or 0)
            r.message = "같은 내용의 필지가 이미 있다"
    return r


# ---------------------------------------------------------------- 연결 제안·반영

def geometry_contains(geometry: dict, lon: float, lat: float) -> bool:
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    for poly in polys:
        if not point_in_ring((lon, lat), poly[0]):
            continue
        if any(point_in_ring((lon, lat), hole) for hole in poly[1:]):
            continue
        return True
    return False


def active_links(db: Db, on_date: str | None = None) -> list[dict]:
    rows = db.conn.execute(
        "SELECT c.component_id, c.asset_id, c.component_subject_id, c.effective_from, c.effective_to, c.basis, c.note, p.pnu, p.label AS parcel_label"
        " FROM asset_components c JOIN parcels p ON p.parcel_id = c.component_subject_id WHERE c.component_type = 'parcel' ORDER BY c.asset_id, c.effective_from, p.pnu")
    out = []
    for r in rows:
        d = dict(r)
        # 적용 기간은 반개구간 [effective_from, effective_to): 종료일 당일은 유효하지 않다 (데이터 사전 §1)
        if on_date is not None and (d["effective_from"] > on_date or (d["effective_to"] is not None and d["effective_to"] <= on_date)):
            continue
        out.append(d)
    return out


def suggest_links(db: Db, *, effective_from: str) -> dict:
    """위치점을 품는 필지를 물건마다 찾는다. 이미 유효한 연결이 있는 (물건, 필지) 쌍은 뺀다. 정본에 쓰지 않는다."""
    if parse_date(effective_from) is None:
        raise ValidationError([f"effective_from 이 달력상 날짜가 아니다: {effective_from}"])
    linked = {(l["asset_id"], l["pnu"]) for l in active_links(db, effective_from)}
    parcels = [dict(r) for r in db.conn.execute("SELECT pnu, label, emd_name, emd_code, geometry_json, bbox_json FROM parcels ORDER BY pnu")]
    links, unlocated, unmatched = [], [], []
    for a in db.list_assets():
        if a["lon"] is None:
            unlocated.append(a["asset_id"])
            continue
        hits = []
        for p in parcels:
            b = json.loads(p["bbox_json"])
            if not (b[0] <= a["lon"] <= b[2] and b[1] <= a["lat"] <= b[3]):
                continue
            if geometry_contains(json.loads(p["geometry_json"]), a["lon"], a["lat"]):
                hits.append(p)
        if not hits:
            unmatched.append(a["asset_id"])
        for p in hits:
            if (a["asset_id"], p["pnu"]) in linked:
                continue
            links.append({"asset_id": a["asset_id"], "asset_label": a["label"], "pnu": p["pnu"], "parcel_label": f"{p['emd_name'] or p['emd_code']} {p['label']}",
                          "effective_from": effective_from, "effective_to": None, "basis": "location_point", "note": "위치점 포함 제안. 검토 후 parcels-link 로 반영"})
    return {"kind": "asset_components", "generated_at": db.now(), "note": f"위치점 포함 제안 {len(links)}건 · 위치점 없는 물건 {len(unlocated)} · 필지 밖 위치점 {len(unmatched)} · 이미 연결됨 제외",
            "links": links, "_unlocated_asset_ids": unlocated, "_unmatched_asset_ids": unmatched}


def apply_links(db: Db, doc: dict) -> LinkResult:
    errs = schema_errors(LINKS_SCHEMA, doc)
    if errs:
        raise ValidationError(errs[:10])
    bad = [f"links[{i}]: {l['effective_from']} / {l['effective_to']}" for i, l in enumerate(doc["links"])
           if parse_date(l["effective_from"]) is None or (l["effective_to"] is not None and parse_date(l["effective_to"]) is None)]
    if bad:
        raise ValidationError(["달력상 존재하지 않는 날짜: " + "; ".join(bad)])
    r = LinkResult()
    now = db.now()
    with db.transaction():
        for i, l in enumerate(doc["links"]):
            if l["effective_to"] is not None and l["effective_to"] <= l["effective_from"]:
                raise ValidationError([f"links[{i}]: effective_to 는 effective_from 보다 뒤여야 한다 (반개구간, 같은 날이면 빈 기간)"])
            if db.conn.execute("SELECT 1 FROM assets WHERE asset_id = ?", (l["asset_id"],)).fetchone() is None:
                raise ValidationError([f"links[{i}]: 물건 {l['asset_id']} 이 정본에 없다 (시드를 먼저 반영한다)"])
            p = db.conn.execute("SELECT parcel_id FROM parcels WHERE pnu = ?", (l["pnu"],)).fetchone()
            if p is None:
                raise ValidationError([f"links[{i}]: PNU {l['pnu']} 가 정본 parcels 에 없다 (parcels-load 를 먼저 한다)"])
            cur = db.conn.execute("SELECT component_id, effective_to, basis, note FROM asset_components WHERE asset_id = ? AND component_subject_id = ? AND effective_from = ?",
                                  (l["asset_id"], p["parcel_id"], l["effective_from"])).fetchone()
            new = {"effective_to": l["effective_to"], "basis": l["basis"], "note": l.get("note")}
            # 같은 (물건, 필지) 의 다른 기간과 겹치면 거절한다 (한 시점에 같은 연결이 두 행이면 유효 연결이 모호해진다). 기간은 반개구간이라 끝날 = 다음 시작일은 겹침이 아니다
            for o in db.conn.execute("SELECT effective_from, effective_to FROM asset_components WHERE asset_id = ? AND component_subject_id = ? AND effective_from <> ?",
                                     (l["asset_id"], p["parcel_id"], l["effective_from"])):
                a0, a1 = l["effective_from"], l["effective_to"]
                b0, b1 = o["effective_from"], o["effective_to"]
                if (a1 is None or a1 > b0) and (b1 is None or b1 > a0):
                    raise ValidationError([f"links[{i}]: 같은 물건·필지의 기존 연결({b0}~{b1 or '진행 중'})과 기간이 겹친다. 기존 연결의 종료일을 먼저 정한다"])
            if cur is None:
                cid = str(uuid.uuid4())
                db.conn.execute(
                    "INSERT INTO asset_components (component_id, asset_id, component_subject_id, component_type, effective_from, effective_to, basis, note, recorded_at, updated_at)"
                    " VALUES (?, ?, ?, 'parcel', ?, ?, ?, ?, ?, ?)", (cid, l["asset_id"], p["parcel_id"], l["effective_from"], new["effective_to"], new["basis"], new["note"], now, now))
                r.inserted += 1
                r.ids.append(cid)
            elif {k: cur[k] for k in new} == new:
                r.unchanged += 1
                r.ids.append(cur["component_id"])
            else:
                db.conn.execute("UPDATE asset_components SET effective_to = ?, basis = ?, note = ?, updated_at = ? WHERE component_id = ?",
                                (new["effective_to"], new["basis"], new["note"], now, cur["component_id"]))
                r.updated += 1
                r.ids.append(cur["component_id"])
        if r.inserted or r.updated:
            r.outcome = "applied"
            r.dataset_version = db.bump_dataset_version()
        else:
            r.dataset_version = int(db.meta("dataset_version") or 0)
    return r


# ---------------------------------------------------------------- 필지 속성 이력 (J5-026)

MAX_HISTORY_ENTRIES = 200   # 파생본 필지마다 싣는 변화 항목 상한 (스키마 계약과 같다). 넘으면 최근 것만


def _snapshot_changes(snaps: list) -> list[dict]:
    """한 필지의 스냅샷(kind, as_of 오름차순)에서 값이 바뀐 지점만 [{as_of, kind, changes:{field:{from,to}}}]. 종류마다 첫 스냅샷은 from=null 인 '처음 확인' 항목이다."""
    out = []
    prev: dict[str, dict] = {}
    for s_ in sorted(snaps, key=lambda x: (x["as_of"], x["kind"])):
        values = json.loads(s_["values_json"]) if isinstance(s_["values_json"], str) else s_["values_json"]
        before = prev.get(s_["kind"])
        if before is None:
            changes = {k: {"from": None, "to": v} for k, v in values.items() if not (v is None or v is False or v == [])}   # 0 은 값이다 (리뷰 반영)
        else:
            changes = {k: {"from": before.get(k), "to": v} for k, v in values.items() if before.get(k) != v}
        if changes:
            out.append({"as_of": s_["as_of"], "kind": s_["kind"], "first": before is None, "changes": changes})
        prev[s_["kind"]] = values
    return out


def _changes_by_parcel(db: Db) -> dict[str, list[dict]]:
    rows = db.conn.execute("SELECT parcel_id, kind, as_of, values_json FROM parcel_attribute_snapshots ORDER BY parcel_id, as_of, kind").fetchall()
    by: dict[str, list] = {}
    for r_ in rows:
        by.setdefault(r_["parcel_id"], []).append(dict(r_))
    return {pid: _snapshot_changes(snaps) for pid, snaps in by.items()}


def price_series(snapshots: list[dict]) -> list[dict]:
    """토지특성 스냅샷(기준일 오름차순)에서 공시지가가 바뀐 지점만 [{as_of, price_krw_m2, base_year, base_month, delta_pct, same_base}] (J5-030).
    값이 없는 기준일은 건너뛰고, 같은 값·같은 기준연월이 이어지면 하나로 둔다. same_base 는 앞 점과 기준연월이 같은데 값이 다른 경우(정정 등, 확인 필요).
    폰의 priceTrend(web/app/parcels.js)와 같은 규칙이다."""
    out: list[dict] = []
    for s_ in sorted((x for x in snapshots if x["kind"] == "land_feature"), key=lambda x: x["as_of"]):
        v = s_["values"]
        price = v.get("official_land_price_krw_m2")
        if price is None:
            continue
        pt = {"as_of": s_["as_of"], "price_krw_m2": price, "base_year": v.get("price_base_year"), "base_month": v.get("price_base_month")}
        prev = out[-1] if out else None
        if prev and (prev["price_krw_m2"], prev["base_year"], prev["base_month"]) == (price, pt["base_year"], pt["base_month"]):
            continue
        pt["delta_pct"] = round((price - prev["price_krw_m2"]) / prev["price_krw_m2"] * 100, 2) if prev and prev["price_krw_m2"] > 0 else None
        pt["same_base"] = bool(prev) and prev["base_year"] is not None and (prev["base_year"], prev["base_month"]) == (pt["base_year"], pt["base_month"])
        out.append(pt)
    return out


def attribute_history(db: Db, pnu: str) -> dict:
    """PNU 하나의 속성 이력: 현재 값, 스냅샷 목록(종류·기준일·값·출처), 변화 항목. 정본에 없으면 DbError."""
    p = db.conn.execute("SELECT parcel_id, pnu, label, emd_name, emd_code, geometry_version FROM parcels WHERE pnu = ?", (pnu,)).fetchone()
    if p is None:
        raise DbError("parcel_missing", f"PNU {pnu} 가 정본 parcels 에 없다")
    cur = db.conn.execute("SELECT * FROM parcel_attributes WHERE parcel_id = ?", (p["parcel_id"],)).fetchone()
    current = None
    if cur is not None:
        current = _attrs_content_from_row(cur)
        current["as_of"] = cur["as_of"]
    snaps = [dict(r_) for r_ in db.conn.execute("SELECT kind, as_of, values_json, source_json, source_document_id, recorded_at FROM parcel_attribute_snapshots WHERE parcel_id = ? ORDER BY as_of, kind", (p["parcel_id"],))]
    for s_ in snaps:
        s_["values"] = json.loads(s_.pop("values_json"))
        src_json = s_.pop("source_json")
        s_["source"] = json.loads(src_json) if src_json else None
    return {"pnu": pnu, "label": f"{p['emd_name'] or p['emd_code']} {p['label']}", "geometry_version": p["geometry_version"], "current": current,
            "snapshots": snaps, "changes": _snapshot_changes([{"kind": s_["kind"], "as_of": s_["as_of"], "values_json": s_["values"]} for s_ in snaps]),
            "price_series": price_series(snaps)}


ATTR_LABELS = {"jimok_name": "지목", "registered_area_m2": "공부면적(㎡)", "official_land_price_krw_m2": "공시지가(원/㎡)", "price_base_year": "공시 기준연도", "price_base_month": "공시 기준월",
               "use_zone_1": "용도지역 1", "use_zone_2": "용도지역 2", "land_use_situation": "이용상황", "road_side": "도로접면", "terrain_height": "지형 높이", "terrain_form": "지형 형상",
               "plan_zones": "지역지구 코드", "plan_zone_names": "지역지구 이름 목록", "plan_zones_truncated": "이름 목록 잘림", "ownership_kind_code": "소유 구분 코드", "ownership_kind": "소유 구분", "co_owner_count": "공유인수",
               "ownership_changed_on": "소유 변동일", "ownership_change_cause_code": "변동 원인 코드", "national_institution_code": "국가기관 구분"}
KIND_LABELS = {"land_feature": "토지특성", "land_plan": "토지이용계획", "land_ownership": "토지소유"}


def _fmt_attr_value(field: str, v) -> str:
    if v is None:
        return "없음"
    if field == "plan_zones":   # 코드와 관계 (J5-032: 이름은 코드와 짝짓지 않는다. J5-025 형식의 옛 값도 코드로 보인다)
        return ", ".join(z.get("code") + (f"({z['relation']})" if z.get("relation") and z["relation"] != "포함" else "") for z in v) or "없음"
    if field == "plan_zone_names":
        return ", ".join(v) or "없음"
    if field == "official_land_price_krw_m2":
        return f"{int(v):,}"
    return str(v)


def history_text(h: dict) -> str:
    lines = [f"필지 {h['label']} (PNU {h['pnu']}, 도형 기준일 {h['geometry_version']})"]
    if h["current"] is None:
        lines.append("필지 속성 없음 (VWorld 묶음으로 변환한 번들을 parcels-load 로 넣는다)")
    else:
        c = h["current"]
        lines.append(f"현재 (기준일 {c['as_of']}): 지목 {c['jimok_name'] or '없음'} · 공부면적 {c['registered_area_m2'] if c['registered_area_m2'] is not None else '없음'}㎡ · 공시지가 {_fmt_attr_value('official_land_price_krw_m2', c['official_land_price_krw_m2'])}원/㎡"
                     f" ({c['price_base_year'] or '?'}년 {c['price_base_month'] or '?'}월) · 용도지역 {c['use_zone_1'] or '없음'} · 소유 {c['ownership_kind'] or '없음'}")
    lines.append(f"스냅샷 {len(h['snapshots'])}건: " + ", ".join(f"{s_['as_of']} {KIND_LABELS.get(s_['kind'], s_['kind'])}" for s_ in h["snapshots"]) if h["snapshots"] else "스냅샷 없음")
    for ch in h["changes"]:
        head = f"{ch['as_of']} {KIND_LABELS.get(ch['kind'], ch['kind'])}" + (" (처음 확인)" if ch["first"] else "")
        body = "; ".join(f"{ATTR_LABELS.get(k, k)}: {_fmt_attr_value(k, d['from'])} → {_fmt_attr_value(k, d['to'])}" if not ch["first"] else f"{ATTR_LABELS.get(k, k)} {_fmt_attr_value(k, d['to'])}"
                         for k, d in ch["changes"].items())
        lines.append(f"  {head}: {body}")
    ps = h.get("price_series") or []
    if len(ps) >= 2:
        lines.append("공시지가 추이 (원/㎡, 값이 바뀐 지점):")
        for pt in ps:
            base = f"{pt['base_year']}년" + (f" {pt['base_month']}월" if pt["base_month"] else "") + " 기준" if pt["base_year"] else "기준연월 미확인"
            delta = "" if pt["delta_pct"] is None else f" ({pt['delta_pct']:+.1f}%)"
            lines.append(f"  {base} {int(pt['price_krw_m2']):,}{delta} · 확인 {pt['as_of']}" + (" · 같은 기준연월의 값이 바뀜 (정정 여부 확인)" if pt["same_base"] else ""))
    lines.append("값은 자료 표기 그대로이며 확인·판단이 아니다. 기준일은 자료를 내려받아 변환할 때 적은 도형 기준일이다.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 파생본용 조회

# ---------------------------------------------------------------- 폰 파생본 필지 범위 (J5-039, ADR-23)
# 정본은 여러 지역의 필지를 담는다(CANONICAL_MAX_PARCELS). 폰 파생본(parcels.geojson)은 폰 상한 MAX_FEATURES 안에서
# 설정한 법정동 범위(meta phone_parcel_scope)의 필지와, 범위 밖이어도 물건이 연결된 필지를 싣는다. 범위를 정하지 않으면 전체다.

def phone_scope(db: Db) -> list[str] | None:
    """폰 파생본에 실을 법정동 코드 목록. 정하지 않았으면 None (전체)."""
    raw = db.meta(PHONE_SCOPE_KEY)
    if not raw:
        return None
    doc = json.loads(raw)
    return list(doc.get("emd_codes") or []) or None


def set_phone_scope(db: Db, emd_codes: list[str] | None) -> dict:
    """폰 파생본 범위를 정한다. None 이면 전체로 되돌린다. 정본에 없는 법정동 코드는 거절한다. 정본 자료는 바꾸지 않으므로 dataset_version 을 올리지 않는다
    (파생본은 매번 전량 생성하므로 다음 `project` 부터 반영된다)."""
    codes = sorted(dict.fromkeys(c.strip() for c in (emd_codes or []) if c.strip())) or None
    if codes:
        bad = [c for c in codes if not re.fullmatch(r"\d{10}", c)]
        if bad:
            raise ValidationError([f"법정동 코드는 10자리 숫자: {', '.join(bad[:5])}"])
        have = {r[0] for r in db.conn.execute(f"SELECT DISTINCT emd_code FROM parcels WHERE emd_code IN ({','.join('?' * len(codes))})", codes)}
        missing = [c for c in codes if c not in have]
        if missing:
            raise ValidationError([f"정본 필지에 없는 법정동 코드: {', '.join(missing[:5])}. `db phone-scope` 로 법정동별 필지 수를 본다"])
    with db.transaction():
        if codes:
            db.set_meta(PHONE_SCOPE_KEY, json.dumps({"emd_codes": codes}, ensure_ascii=False))
        else:
            db.conn.execute("DELETE FROM meta WHERE key = ?", (PHONE_SCOPE_KEY,))
    return scope_overview(db)


def scope_overview(db: Db) -> dict:
    """법정동별 정본 필지 수와 현재 폰 범위, 범위를 적용했을 때 폰에 실릴 필지 수(연결 필지 포함)."""
    codes = phone_scope(db)
    by_emd = [{"emd_code": r[0], "emd_name": r[1], "parcels": r[2], "in_scope": codes is None or r[0] in codes}
              for r in db.conn.execute("SELECT emd_code, MAX(emd_name), COUNT(*) FROM parcels GROUP BY emd_code ORDER BY emd_code")]
    selected = len(_phone_parcel_pnus(db, codes, db.now()[:10]))
    total = sum(e["parcels"] for e in by_emd)
    return {"scope": codes, "total_parcels": total, "phone_parcels": selected, "phone_limit": MAX_FEATURES, "by_emd": by_emd}


def _phone_parcel_pnus(db: Db, codes: list[str] | None, on_date: str) -> set[str]:
    if codes is None:
        return {r[0] for r in db.conn.execute("SELECT pnu FROM parcels")}
    inside = {r[0] for r in db.conn.execute(f"SELECT pnu FROM parcels WHERE emd_code IN ({','.join('?' * len(codes))})", codes)}
    return inside | {l["pnu"] for l in active_links(db, on_date)}


def scope_text(o: dict) -> str:
    scope = "전체 (범위를 정하지 않음)" if o["scope"] is None else ", ".join(o["scope"])
    lines = [f"폰 파생본 필지 범위: {scope}", f"정본 필지 {o['total_parcels']}개 · 폰에 실릴 필지 {o['phone_parcels']}개 (범위 안 + 물건이 연결된 필지) · 폰 상한 {o['phone_limit']}개"]
    for e in o["by_emd"]:
        lines.append(f"  {'*' if e['in_scope'] else ' '} {e['emd_code']} {e['emd_name'] or ''} · {e['parcels']}필지")
    if o["phone_parcels"] > o["phone_limit"]:
        lines.append(f"폰 상한을 넘어 `project` 가 실패한다. `db phone-scope --emd <코드,…>` 로 범위를 좁힌다")
    return "\n".join(lines) + "\n"


def parcels_bundle_from_db(db: Db, *, generated_at: str, source_dataset_version: int | None = None) -> dict | None:
    """정본 parcels 를 폰이 읽는 번들 형식(j5parcels 1.0.0)으로. 필지가 없으면 None. 물건 연결은 feature.properties.asset_ids (유효한 연결만).
    폰 범위(phone_scope)를 정했으면 그 법정동의 필지와 물건이 연결된 필지만 싣고 warnings 에 적는다 (J5-039). 정하지 않았으면 전체.
    출처 요약은 가장 최근 도형 기준일의 출처를 쓰고, 다른 출처가 섞여 있으면 warnings 에 적는다."""
    all_count = db.conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
    codes = phone_scope(db)
    keep = _phone_parcel_pnus(db, codes, generated_at[:10])
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM parcels ORDER BY pnu") if r["pnu"] in keep]
    if not rows:
        return None
    attrs_by_id: dict[str, dict] = {}
    attrs_sources: list[dict] = []
    if db._has_table("parcel_attributes"):
        seen = set()
        for a in db.conn.execute("SELECT * FROM parcel_attributes"):
            a = dict(a)
            attrs = _attrs_content_from_row(a)
            attrs["as_of"] = a["as_of"]   # 속성 기준일: 도형 기준일과 별개로 폰이 따로 보인다 (리뷰 반영)
            attrs_by_id[a["parcel_id"]] = attrs
            for s_ in json.loads(a["sources_json"]):   # 출처는 모든 속성 행에서 모은다 (구역·부분 묶음마다 다르다, 리뷰 반영 PR #72)
                key = (s_["kind"], s_["dbf_sha256"])
                if key not in seen:
                    seen.add(key)
                    attrs_sources.append(s_)
        attrs_sources.sort(key=lambda s_: (s_["kind"], s_["dbf_sha256"]))
    history_by_id: dict[str, list[dict]] = {}
    if db._has_table("parcel_attribute_snapshots"):
        for pid, entries in _changes_by_parcel(db).items():
            if entries:
                history_by_id[pid] = entries
    today = generated_at[:10]
    links: dict[str, list[str]] = {}
    for l in active_links(db, today):
        links.setdefault(l["pnu"], []).append(l["asset_id"])
    features = []
    for p in rows:
        props = {"pnu": p["pnu"], "label": p["label"], "emd_code": p["emd_code"], "emd_name": p["emd_name"], "mountain": bool(p["mountain"]), "bon": p["bon"], "bu": p["bu"],
                 "jimok": p["jimok"], "jibun_raw": p["jibun_raw"], "jibun_mismatch": bool(p["jibun_mismatch"]), "area_m2_geom": p["geom_area_m2"],
                 "area_missing_reason": p["geom_area_missing_reason"], "bbox": json.loads(p["bbox_json"]), "geometry_version": p["geometry_version"],
                 "asset_ids": sorted(links.get(p["pnu"], []))}
        if p["parcel_id"] in attrs_by_id:
            props["attrs"] = attrs_by_id[p["parcel_id"]]
        if p["parcel_id"] in history_by_id:
            props["attrs_history"] = history_by_id[p["parcel_id"]][-MAX_HISTORY_ENTRIES:]
        features.append({"type": "Feature", "id": p["pnu"], "geometry": json.loads(p["geometry_json"]), "properties": props})
    latest = max(rows, key=lambda p: (p["geometry_version"], p["updated_at"]))
    sources = sorted({(p["source_name"], p["geometry_version"]) for p in rows})
    warnings = []
    if codes is not None:
        warnings.append(f"폰 범위: 정본 필지 {all_count}개 중 법정동 {', '.join(codes)} 과 물건이 연결된 필지 {len(rows)}개만 실었다")
    if len(sources) > 1:
        warnings.append("출처·도형 기준일이 섞여 있다: " + "; ".join(f"{n} {v}" for n, v in sources))
    bboxes = [json.loads(p["bbox_json"]) for p in rows]
    bbox = [min(b[0] for b in bboxes), min(b[1] for b in bboxes), max(b[2] for b in bboxes), max(b[3] for b in bboxes)]
    epsg = None
    if latest["source_crs"] and latest["source_crs"].startswith("EPSG:") and latest["source_crs"][5:].isdigit():
        epsg = int(latest["source_crs"][5:])
    crs_mix = sorted({(p["source_crs"], p["source_ellipsoid"], p["source_datum_shift"]) for p in rows})
    if len(crs_mix) > 1:
        warnings.append("원본 좌표계가 섞여 있다: " + "; ".join(f"{c or '?'} ({e or '?'}, {d or '변환 없음'})" for c, e, d in crs_mix))
    if any(p["source_datum_shift"] == "korean1985" for p in rows):
        warnings.append(BESSEL_WARNING)
    out = {
        "type": "FeatureCollection", "j5parcels": "1.0.0", "data_mode": "synthetic" if db.data_mode == "synthetic" else "real", "generated_at": generated_at,
        "study_id": db.meta("study_id"), "source_dataset_version": source_dataset_version if source_dataset_version is not None else int(db.meta("dataset_version") or 0),
        "source": {"name": latest["source_name"], "file": "j5.sqlite3 (정본 parcels)", "shp_sha256": latest["source_shp_sha256"] or "0" * 64, "dbf_sha256": "0" * 64,
                   "crs": {"epsg": epsg, "name": latest["source_crs"] or "unknown", "ellipsoid": latest["source_ellipsoid"] or "WGS84",
                           "datum_shift": latest["source_datum_shift"], "detected_from": "argument"},
                   "encoding": "utf-8", "record_count": len(rows), "geometry_version": latest["geometry_version"], "license": latest["source_license"], "fields": []},
        "clip": {"bbox": bbox, "center": None, "radius_m": None}, "count": len(rows), "bbox": bbox, "warnings": warnings, "features": features,
    }
    if attrs_sources:
        out["attrs_sources"] = attrs_sources   # 여러 번들(구역)을 반영했으면 자료별로 여러 개 (kind·dbf 해시로 중복 제거)
    return out
