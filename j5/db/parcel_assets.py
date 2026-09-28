"""필지 목록으로 물건 만들기, PC 판 (J5-038). `db parcels-find` 로 고른 후보 필지(또는 PNU 목록)마다 매입 검토 단위를 만들어
필지에 연결하고, 원하면 관심 단계를 준다. 폰의 "이 필지를 물건으로 기록"(J5-028) 은 기기 임시 단위(pending)이고 이것은 PC 정본에서 바로 확정된 물건이다.

- 물건: 새 asset_id(UUID), 이름 "동 지번", 위치점은 필지 안의 점(폰 interiorPoint 와 같은 규칙), created_at 은 오늘, notes 에 만든 경위.
  시드 반영과 같은 경로(`Db.load_seed`)로 넣는다.
- 연결: asset_components 에 basis manual, 시작일(기본 오늘)부터 끝없음. `apply_links` 와 같은 검사를 거친다.
- 관심 단계: `--track` 을 주면 `tracking.set_status`(사유 규칙·이력 표 그대로). purchase_ready 는 여기서도 줄 수 없다.
- 이미 물건이 있는 필지(기준일에 유효한 정본 연결 또는 위치점 포함)는 건너뛰고 결과에 적는다. 정본에 없는 PNU 가 하나라도 있으면 전체를 거절한다.
- 전체를 한 트랜잭션으로 한다(중간 실패면 아무것도 남지 않는다). 한 번에 MAX_PARCELS 필지까지 (관찰목록 30~50개 규모).
"""

from __future__ import annotations

import csv
import json
import re
import uuid
from pathlib import Path

from j5.db.parcel_find import assets_by_parcel
from j5.db.parcels import apply_links, geometry_contains
from j5.db.store import Db, DbError
from j5.db.tracking import set_status
from j5.db.validate import parse_date

MAX_PARCELS = 50
PNU_RE = re.compile(r"^\d{19}$")


def _polygons(geometry: dict) -> list:
    return [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]


def _ring_area(ring) -> float:
    return sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)) / 2


def _label_point(geometry: dict):
    """가장 큰 조각 외곽 링의 면적 중심 (폰 labelPoint 와 같다: 첫 꼭짓점 기준 상대 좌표로 계산)."""
    best, best_area = None, -1.0
    for poly in _polygons(geometry):
        a = abs(_ring_area(poly[0]))
        if a > best_area:
            best, best_area = poly[0], a
    if not best:
        return None
    ox, oy = best[0]
    rel = [(x - ox, y - oy) for x, y in best]
    a = _ring_area(rel)
    if abs(a) < 1e-18:
        return [best[0][0], best[0][1]]
    cx = cy = 0.0
    for i in range(len(rel) - 1):
        w = rel[i][0] * rel[i + 1][1] - rel[i + 1][0] * rel[i][1]
        cx += (rel[i][0] + rel[i + 1][0]) * w
        cy += (rel[i][1] + rel[i + 1][1]) * w
    return [ox + cx / (6 * a), oy + cy / (6 * a)]


def interior_point(geometry: dict, bbox: list) -> list | None:
    """필지 안의 점 [경도, 위도] (소수 7자리). 면적 중심이 안이면 그것, 아니면 bbox 격자(5·11·23)에서 처음 안에 드는 점. 폰 interiorPoint 와 같다."""
    lp = _label_point(geometry)
    if lp and geometry_contains(geometry, lp[0], lp[1]):
        return [round(lp[0], 7), round(lp[1], 7)]
    for n in (5, 11, 23):
        for i in range(1, n):
            for j in range(1, n):
                p = (bbox[0] + (bbox[2] - bbox[0]) * i / n, bbox[1] + (bbox[3] - bbox[1]) * j / n)
                if geometry_contains(geometry, p[0], p[1]):
                    return [round(p[0], 7), round(p[1], 7)]
    return None


def pnus_from_csv(path: Path) -> list[str]:
    """`db parcels-find --csv` 의 파일에서 pnu 열을 읽는다 (순서 유지, 중복 제거)."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise DbError("bad_csv", f"CSV 를 읽을 수 없다: {path}: {e}") from e
    if rows and "pnu" not in rows[0]:
        raise DbError("bad_csv", f"CSV 에 pnu 열이 없다: {path} (parcels-find --csv 의 파일을 준다)")
    out: list[str] = []
    for r in rows:
        p = (r.get("pnu") or "").strip()
        if p and p not in out:
            out.append(p)
    return out


def plan(db: Db, pnus: list[str], *, on_date: str) -> dict:
    """무엇을 만들고 무엇을 건너뛸지 정한다 (쓰지 않는다)."""
    bad = [p for p in pnus if not PNU_RE.match(p)]
    if bad:
        raise DbError("bad_pnu", f"PNU 는 19자리 숫자: {', '.join(bad[:5])}")
    if not pnus:
        raise DbError("no_pnu", "대상 필지가 없다 (--pnu 또는 --csv)")
    if len(pnus) > MAX_PARCELS:
        raise DbError("too_many", f"한 번에 {MAX_PARCELS}필지까지 만든다 ({len(pnus)}필지). 후보를 좁히거나 나눠 만든다")
    rows = {r["pnu"]: dict(r) for r in db.conn.execute(
        f"SELECT pnu, label, emd_name, emd_code, bbox_json, geometry_json FROM parcels WHERE pnu IN ({','.join('?' * len(pnus))})", pnus)}
    missing = [p for p in pnus if p not in rows]
    if missing:
        raise DbError("parcel_missing", f"정본에 없는 필지: {', '.join(missing[:5])}" + (f" 외 {len(missing) - 5}개" if len(missing) > 5 else "") + ". 반영하지 않는다")
    parcels = [rows[p] for p in pnus]
    inside = assets_by_parcel(db, parcels, on_date)
    create, skip = [], []
    for p in parcels:
        label = f"{p['emd_name'] or p['emd_code']} {p['label']}"
        have = inside.get(p["pnu"], [])
        if have:
            skip.append({"pnu": p["pnu"], "parcel": label, "reason": "이미 물건이 있다: " + ", ".join(f"{a['label']}({a['basis']})" for a in have)})
            continue
        pt = interior_point(json.loads(p["geometry_json"]), json.loads(p["bbox_json"]))
        if pt is None:
            skip.append({"pnu": p["pnu"], "parcel": label, "reason": "필지 안의 점을 정하지 못했다"})
            continue
        create.append({"pnu": p["pnu"], "parcel": label, "location_point": pt})
    return {"create": create, "skip": skip}


def create_from_parcels(db: Db, pnus: list[str], *, track: str | None = None, reason: str | None = None, effective_from: str | None = None,
                        dry_run: bool = False) -> dict:
    on_date = effective_from or db.now()[:10]
    if parse_date(on_date) is None:
        raise DbError("bad_date", f"연결 시작일이 달력상 날짜가 아니다: {on_date}")
    p = plan(db, pnus, on_date=on_date)
    result = {"dry_run": dry_run, "effective_from": on_date, "track": track, "created": [], "skipped": p["skip"], "dataset_version": int(db.meta("dataset_version") or 0)}
    if dry_run or not p["create"]:
        result["created"] = [{**c, "asset_id": None} for c in p["create"]] if dry_run else []
        return result
    today = db.now()[:10]
    seed = []
    for c in p["create"]:
        c["asset_id"] = str(uuid.uuid4())
        seed.append({"asset_id": c["asset_id"], "label": c["parcel"], "location_point": c["location_point"], "data_mode": db.data_mode, "created_at": today,
                     "notes": f"PC 에서 필지 {c['pnu']} 로 만든 매입 검토 단위 (db asset-from-parcels)"})
    with db.transaction():
        db.load_seed(seed)
        apply_links(db, {"kind": "asset_components", "links": [{"asset_id": c["asset_id"], "pnu": c["pnu"], "effective_from": on_date, "effective_to": None, "basis": "manual",
                                                                 "note": "필지에서 만든 물건 (asset-from-parcels)"} for c in p["create"]]})
        if track:
            for c in p["create"]:
                set_status(db, c["asset_id"], track, reason=reason, changed_on=today)
    result["created"] = p["create"]
    result["dataset_version"] = int(db.meta("dataset_version") or 0)
    return result


def result_text(r: dict) -> str:
    head = "만들 물건 (미리 보기, 정본에 쓰지 않음)" if r["dry_run"] else "만든 물건"
    lines = [f"{head} {len(r['created'])}개 · 건너뜀 {len(r['skipped'])}개 · 연결 시작일 {r['effective_from']}" + (f" · 관심 단계 {r['track']}" if r["track"] else "")]
    for c in r["created"]:
        lines.append(f"  + {c['pnu']} {c['parcel']}" + (f" → {c['asset_id']}" if c.get("asset_id") else "") + f" · 위치점 {c['location_point'][0]}, {c['location_point'][1]}")
    for s in r["skipped"]:
        lines.append(f"  - {s['pnu']} {s['parcel']}: {s['reason']}")
    if not r["dry_run"] and r["created"]:
        lines.append(f"dataset_version {r['dataset_version']}. 폰에는 project → project-copy 뒤 보인다")
    return "\n".join(lines) + "\n"
