"""정본 필지·물건 구성 (J5-013B-2). 데이터 사전 §2 parcels / asset_components, §6, ADR-13.

- `load_bundle`: `j5 parcels convert` 가 만든 번들(.j5parcels.json)의 필지를 정본 `parcels` 에 반영한다. PNU 가 외부 ID, parcel_id 가 내부 UUID.
  같은 PNU·같은 내용은 변화 없음, 더 새로운 도형 기준일이면 갱신(도형 버전 교체), 같은 기준일·다른 내용은 거절, 더 오래된 기준일은 거절.
  번들의 data_mode(synthetic/real)는 정본(synthetic/private_real)과 맞아야 한다. 반영한 번들은 source_documents 에 출처로 남긴다.
- `suggest_links`: 물건의 위치점을 품는 필지를 찾아 연결 제안 파일(asset_components_input)을 만든다. 제안은 정본에 쓰지 않는다.
- `apply_links`: 검토한 연결 파일을 정본 `asset_components` 에 반영한다(물건·필지가 정본에 있어야 한다). 같은 (물건, 필지, 시작일)은 갱신.
정본 변경이 있으면 dataset_version 을 1 올린다. 필지 번들·연결 파일은 정본 시각을 정하지 못한다(recorded_at 은 저장소가 부여).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from j5.db.store import Db, DbError
from j5.db.validate import ValidationError, parse_date
from j5.parcels.convert import point_in_ring
from j5.schemas_loader import schema_errors

BUNDLE_SCHEMA = "parcels_bundle.schema.json"
LINKS_SCHEMA = "asset_components_input.schema.json"
BUNDLE_MODE_TO_DB = {"synthetic": "synthetic", "real": "private_real"}


@dataclass
class ParcelLoadResult:
    outcome: str = "unchanged"          # applied / unchanged
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    dataset_version: int = 0
    source_document_id: str | None = None
    source_name: str = ""
    geometry_version: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged, "dataset_version": self.dataset_version,
                "source_document_id": self.source_document_id, "source_name": self.source_name, "geometry_version": self.geometry_version, "message": self.message}

    def to_text(self) -> str:
        return (f"필지 반영: {'반영됨' if self.outcome == 'applied' else '변화 없음'} · 신규 {self.inserted}, 갱신 {self.updated}, 변화 없음 {self.unchanged}"
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
            "source_shp_sha256": src["shp_sha256"], "source_license": src["license"]}


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
            cur = db.conn.execute("SELECT parcel_id, content_hash, geometry_version FROM parcels WHERE pnu = ?", (feature["id"],)).fetchone()
            if cur is None:
                plan.append(("insert", content, h, None))
            elif cur["content_hash"] == h:
                plan.append(("unchanged", content, h, cur))
            elif content["geometry_version"] > cur["geometry_version"]:
                plan.append(("update", content, h, cur))
            elif content["geometry_version"] == cur["geometry_version"]:
                raise DbError("parcel_conflict", f"PNU {feature['id']}: 같은 도형 기준일({cur['geometry_version']})인데 내용이 다르다. 새 기준일의 자료로 다시 변환하거나 원본을 확인한다")
            else:
                raise DbError("parcel_older", f"PNU {feature['id']}: 번들 기준일 {content['geometry_version']} 이 정본의 {cur['geometry_version']} 보다 오래됐다. 반영하지 않는다")
        changed = any(a in ("insert", "update") for a, *_ in plan)
        # 2) 출처 문서를 먼저 남기고(필지 행이 참조), 필지를 반영한다
        if changed:
            db.add_source_document({"document_id": doc_id, "document_kind": "official_file", "title": f"{src['name']} (도형 기준일 {src['geometry_version']}, {src['file']})",
                                    "terms": src["license"], "location": src["file"], "sha256": src["shp_sha256"], "source_published_at": src["geometry_version"],
                                    "collected_at": doc["generated_at"], "notes": f"j5parcels {doc['j5parcels']} · EPSG:{src['crs'].get('epsg')} · {doc['count']}필지"})
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
                    " source_name, source_crs, source_shp_sha256, source_license, source_document_id, data_mode, content_hash, recorded_at, updated_at)"
                    " VALUES (:parcel_id, :pnu, :label, :emd_code, :emd_name, :mountain, :bon, :bu, :jimok, :jibun_raw, :jibun_mismatch,"
                    " NULL, 'not_collected', :geom_area_m2, :geom_area_missing_reason, :geometry_json, :geometry_version, :bbox_json,"
                    " :source_name, :source_crs, :source_shp_sha256, :source_license, :source_document_id, :data_mode, :content_hash, :now, :now)",
                    {**row, "parcel_id": pid})
                r.inserted += 1
            else:
                db.conn.execute(
                    "UPDATE parcels SET label = :label, emd_code = :emd_code, emd_name = :emd_name, mountain = :mountain, bon = :bon, bu = :bu, jimok = :jimok,"
                    " jibun_raw = :jibun_raw, jibun_mismatch = :jibun_mismatch, geom_area_m2 = :geom_area_m2, geom_area_missing_reason = :geom_area_missing_reason,"
                    " geometry_json = :geometry_json, geometry_version = :geometry_version, bbox_json = :bbox_json, source_name = :source_name, source_crs = :source_crs,"
                    " source_shp_sha256 = :source_shp_sha256, source_license = :source_license, source_document_id = :source_document_id, content_hash = :content_hash,"
                    " updated_at = :now WHERE pnu = :pnu", row)
                r.updated += 1
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
        if on_date is not None and (d["effective_from"] > on_date or (d["effective_to"] is not None and d["effective_to"] < on_date)):
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
            if l["effective_to"] is not None and l["effective_to"] < l["effective_from"]:
                raise ValidationError([f"links[{i}]: effective_to 가 effective_from 보다 앞선다"])
            if db.conn.execute("SELECT 1 FROM assets WHERE asset_id = ?", (l["asset_id"],)).fetchone() is None:
                raise ValidationError([f"links[{i}]: 물건 {l['asset_id']} 이 정본에 없다 (시드를 먼저 반영한다)"])
            p = db.conn.execute("SELECT parcel_id FROM parcels WHERE pnu = ?", (l["pnu"],)).fetchone()
            if p is None:
                raise ValidationError([f"links[{i}]: PNU {l['pnu']} 가 정본 parcels 에 없다 (parcels-load 를 먼저 한다)"])
            cur = db.conn.execute("SELECT component_id, effective_to, basis, note FROM asset_components WHERE asset_id = ? AND component_subject_id = ? AND effective_from = ?",
                                  (l["asset_id"], p["parcel_id"], l["effective_from"])).fetchone()
            new = {"effective_to": l["effective_to"], "basis": l["basis"], "note": l.get("note")}
            # 같은 (물건, 필지) 의 다른 기간과 겹치면 거절한다 (한 시점에 같은 연결이 두 행이면 유효 연결이 모호해진다)
            for o in db.conn.execute("SELECT effective_from, effective_to FROM asset_components WHERE asset_id = ? AND component_subject_id = ? AND effective_from <> ?",
                                     (l["asset_id"], p["parcel_id"], l["effective_from"])):
                a0, a1 = l["effective_from"], l["effective_to"]
                b0, b1 = o["effective_from"], o["effective_to"]
                if (a1 is None or a1 >= b0) and (b1 is None or b1 >= a0):
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


# ---------------------------------------------------------------- 파생본용 조회

def parcels_bundle_from_db(db: Db, *, generated_at: str) -> dict | None:
    """정본 parcels 전체를 폰이 읽는 번들 형식(j5parcels 1.0.0)으로. 필지가 없으면 None. 물건 연결은 feature.properties.asset_ids (유효한 연결만).
    출처 요약은 가장 최근 도형 기준일의 출처를 쓰고, 다른 출처가 섞여 있으면 warnings 에 적는다."""
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM parcels ORDER BY pnu")]
    if not rows:
        return None
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
        features.append({"type": "Feature", "id": p["pnu"], "geometry": json.loads(p["geometry_json"]), "properties": props})
    latest = max(rows, key=lambda p: (p["geometry_version"], p["updated_at"]))
    sources = sorted({(p["source_name"], p["geometry_version"]) for p in rows})
    warnings = []
    if len(sources) > 1:
        warnings.append("출처·도형 기준일이 섞여 있다: " + "; ".join(f"{n} {v}" for n, v in sources))
    bboxes = [json.loads(p["bbox_json"]) for p in rows]
    bbox = [min(b[0] for b in bboxes), min(b[1] for b in bboxes), max(b[2] for b in bboxes), max(b[3] for b in bboxes)]
    epsg = None
    if latest["source_crs"] and latest["source_crs"].startswith("EPSG:") and latest["source_crs"][5:].isdigit():
        epsg = int(latest["source_crs"][5:])
    return {
        "type": "FeatureCollection", "j5parcels": "1.0.0", "data_mode": "synthetic" if db.data_mode == "synthetic" else "real", "generated_at": generated_at,
        "source": {"name": latest["source_name"], "file": "j5.sqlite3 (정본 parcels)", "shp_sha256": latest["source_shp_sha256"] or "0" * 64, "dbf_sha256": "0" * 64,
                   "crs": {"epsg": epsg, "name": latest["source_crs"] or "unknown", "ellipsoid": "WGS84", "datum_shift": None, "detected_from": "argument"},
                   "encoding": "utf-8", "record_count": len(rows), "geometry_version": latest["geometry_version"], "license": latest["source_license"], "fields": []},
        "clip": {"bbox": bbox, "center": None, "radius_m": None}, "count": len(rows), "bbox": bbox, "warnings": warnings, "features": features,
    }
