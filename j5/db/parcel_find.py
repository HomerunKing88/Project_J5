"""조건으로 필지 찾기, PC 판 (J5-037). 폰 조건 찾기(J5-033·036, web/app/parcels.js filterParcels)와 같은 규칙으로 정본을 거른다.

- 조건: 용도지역 분류(zone_category, 폰 zoneCategory 와 같은 이름 규칙), 공부면적 범위(㎡), 공시지가 범위(원/㎡, CLI 는 만원/㎡ 로 받는다),
  소유구분 값, 저촉 규제 있음(지역지구 관계 중 저촉이 하나라도), 물건 조건(none·any·watch).
- 값이 없는 필지는 그 조건에 맞는 것으로 보지 않고 켠 조건마다 따로 센다(결측을 0 이나 불일치로 채우지 않는다).
  확인된 불일치 없이 결측만 있는 필지는 undetermined 로 따로 센다.
- 필지 안의 물건: 오늘 유효한 정본 연결(asset_components) 또는 위치점 포함. 폰 필지 패널과 같은 기준이며 근거를 함께 적는다.
  관찰목록은 관심 단계 watch·detailed_review·purchase_ready.
- 정본에 쓰지 않는다(읽기 전용으로 열어도 된다). 결과는 공부면적 큰 순, 같으면 PNU 순. 매입 가능성·규제 판단이 아니다.
"""

from __future__ import annotations

import csv
import io
import json

from j5.db.parcels import active_links, geometry_contains
from j5.db.schema import WATCHLIST_STATUSES
from j5.db.store import Db, DbError

ZONE_LABELS = {"res1": "전용주거", "res2": "일반주거", "res3": "준주거", "com": "상업", "ind": "공업", "green": "녹지", "rural": "관리·농림·자연환경", "other": "기타 용도지역"}
ASSET_CRITERIA = {"none": "물건 없는 필지", "any": "물건 있는 필지", "watch": "관찰목록 물건이 있는 필지"}
UNKNOWN_KEYS = ("zone", "area", "price", "owner", "plan")
CSV_FIELDS = ("pnu", "parcel", "use_zone_1", "zone_category", "registered_area_m2", "official_land_price_krw_m2", "price_base_year", "price_base_month",
              "ownership_kind", "restricted_codes", "assets", "watchlist_assets", "asset_ids", "attrs_as_of")


def zone_category(name: str | None) -> str | None:
    """용도지역 이름 → 분류 키. 폰 zoneCategory 와 같은 규칙(공백 제거 뒤 포함 여부, 위에서부터)."""
    if not isinstance(name, str) or not name.strip():
        return None
    n = "".join(name.split())
    for key, words in (("res1", ("전용주거",)), ("res2", ("일반주거",)), ("res3", ("준주거",)), ("com", ("상업",)), ("ind", ("공업",)), ("green", ("녹지",)),
                       ("rural", ("관리", "농림", "자연환경"))):
        if any(w in n for w in words):
            return key
    return "other"


def check_criteria(c: dict) -> list[str]:
    errs = []
    for k, label in (("area_min", "공부면적 최소"), ("area_max", "공부면적 최대"), ("price_min", "공시지가 최소"), ("price_max", "공시지가 최대")):
        v = c.get(k)
        if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or v != v or v < 0):
            errs.append(f"{label}은 0 이상의 숫자")
    if c.get("zone") and c["zone"] not in ZONE_LABELS:
        errs.append(f"용도지역 분류가 목록에 없다 (가능: {', '.join(ZONE_LABELS)})")
    if c.get("assets") and c["assets"] not in ASSET_CRITERIA:
        errs.append(f"물건 조건이 목록에 없다 (가능: {', '.join(ASSET_CRITERIA)})")
    if not errs and c.get("area_min") is not None and c.get("area_max") is not None and c["area_min"] > c["area_max"]:
        errs.append("공부면적 최소가 최대보다 크다")
    if not errs and c.get("price_min") is not None and c.get("price_max") is not None and c["price_min"] > c["price_max"]:
        errs.append("공시지가 최소가 최대보다 크다")
    if not errs and not any(_given(c.get(k)) for k in ("zone", "area_min", "area_max", "price_min", "price_max", "owner", "restricted", "assets")):
        errs.append("조건을 하나 이상 준다")
    return errs


def _given(v) -> bool:
    """조건이 주어졌는가. 0 은 주어진 값이다 (0 == False 이므로 `in (None, False, "")` 로 가리지 않는다)."""
    return v is not None and v is not False and v != ""


def _in_range(v: float, lo, hi) -> bool:
    return (lo is None or v >= lo) and (hi is None or v <= hi)


def _parcel_assets(db: Db, parcels: list[dict], on_date: str) -> dict[str, list[dict]]:
    """필지(parcel_id)마다 안의 물건 [{asset_id, label, tracking_status, basis}]. 정본 연결 + 위치점 포함 (폰 parcelAssets 와 같은 기준)."""
    linked: dict[str, set[str]] = {}
    for lk in active_links(db, on_date):
        linked.setdefault(lk["pnu"], set()).add(lk["asset_id"])
    assets = db.list_assets()
    out: dict[str, list[dict]] = {}
    for p in parcels:
        b = json.loads(p["bbox_json"])
        geom = None
        inside = set()
        for a in assets:
            if a["lon"] is None or a["lat"] is None or not (b[0] <= a["lon"] <= b[2] and b[1] <= a["lat"] <= b[3]):
                continue
            geom = geom or json.loads(p["geometry_json"])
            if geometry_contains(geom, a["lon"], a["lat"]):
                inside.add(a["asset_id"])
        ln = linked.get(p["pnu"], set())
        out[p["pnu"]] = [{"asset_id": a["asset_id"], "label": a["label"], "tracking_status": a["tracking_status"],
                          "basis": "both" if a["asset_id"] in ln and a["asset_id"] in inside else "linked" if a["asset_id"] in ln else "inside"}
                         for a in assets if a["asset_id"] in ln or a["asset_id"] in inside]
    return out


def find_parcels(db: Db, criteria: dict, *, on_date: str | None = None) -> dict:
    """조건에 맞는 필지. 반환 {criteria, total, rows, unknown, undetermined, with_attrs, parcels, on_date}. rows 는 전부(상한 없음), 공부면적 큰 순."""
    errs = check_criteria(criteria)
    if errs:
        raise DbError("bad_criteria", " · ".join(errs))
    if not db._has_table("parcel_attributes"):
        raise DbError("no_attrs", "정본에 필지 속성 표가 없다 (db_schema 15 이상). 도구로 한 번 열어 마이그레이션을 적용한다")
    on_date = on_date or db.now()[:10]
    c = criteria
    parcels = [dict(r) for r in db.conn.execute(
        "SELECT p.parcel_id, p.pnu, p.label, p.emd_name, p.emd_code, p.bbox_json, p.geometry_json, a.as_of, a.use_zone_1, a.registered_area_m2, a.official_land_price_krw_m2,"
        " a.price_base_year, a.price_base_month, a.ownership_kind, a.plan_zones_json FROM parcels p JOIN parcel_attributes a ON a.parcel_id = p.parcel_id ORDER BY p.pnu")]
    total_parcels = db.conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
    inside = _parcel_assets(db, parcels, on_date)   # 물건 조건이 없어도 결과 줄의 물건 수에 쓴다
    unknown = {k: 0 for k in UNKNOWN_KEYS}
    rows, undetermined = [], 0
    for p in parcels:
        zones = json.loads(p["plan_zones_json"] or "[]")
        checks = []
        if c.get("zone"):
            z = zone_category(p["use_zone_1"])
            checks.append(("zone", z is None, z == c["zone"]))
        if c.get("area_min") is not None or c.get("area_max") is not None:
            v = p["registered_area_m2"]
            checks.append(("area", v is None, v is not None and _in_range(v, c.get("area_min"), c.get("area_max"))))
        if c.get("price_min") is not None or c.get("price_max") is not None:
            v = p["official_land_price_krw_m2"]
            checks.append(("price", v is None, v is not None and _in_range(v, c.get("price_min"), c.get("price_max"))))
        if c.get("owner"):
            k = p["ownership_kind"]
            checks.append(("owner", not k, bool(k) and k == c["owner"]))
        if c.get("restricted"):
            checks.append(("plan", not zones, bool(zones) and any(isinstance(z, dict) and z.get("relation") == "저촉" for z in zones)))
        missing = mismatch = False
        for key, miss, ok in checks:
            if miss:
                unknown[key] += 1
                missing = True
            elif not ok:
                mismatch = True
        mine = inside.get(p["pnu"], [])
        watch = [a for a in mine if a["tracking_status"] in WATCHLIST_STATUSES]
        if c.get("assets") and not mismatch:
            want = c["assets"]
            if not (len(mine) == 0 if want == "none" else len(mine) > 0 if want == "any" else len(watch) > 0):
                mismatch = True
        if missing and not mismatch:
            undetermined += 1
        if missing or mismatch:
            continue
        rows.append({"pnu": p["pnu"], "parcel": f"{p['emd_name'] or p['emd_code']} {p['label']}", "use_zone_1": p["use_zone_1"], "zone_category": zone_category(p["use_zone_1"]),
                     "registered_area_m2": p["registered_area_m2"], "official_land_price_krw_m2": p["official_land_price_krw_m2"],
                     "price_base_year": p["price_base_year"], "price_base_month": p["price_base_month"], "ownership_kind": p["ownership_kind"],
                     "restricted_codes": [z.get("code") for z in zones if isinstance(z, dict) and z.get("relation") == "저촉"],
                     "assets": [{"asset_id": a["asset_id"], "label": a["label"], "tracking_status": a["tracking_status"], "basis": a["basis"]} for a in mine],
                     "watchlist_assets": len(watch), "attrs_as_of": p["as_of"]})
    rows.sort(key=lambda r: (-(r["registered_area_m2"] if r["registered_area_m2"] is not None else -1), r["pnu"]))
    return {"criteria": {k: v for k, v in c.items() if _given(v)}, "on_date": on_date, "total": len(rows), "rows": rows, "unknown": unknown,
            "undetermined": undetermined, "with_attrs": len(parcels), "parcels": total_parcels,
            "note": "정본 필지 속성 값 그대로 거른 목록이다. 값이 없는 필지는 맞는 것으로 보지 않고 따로 센다. 매입 가능성·규제 판단이 아니다"}


def _fmt_int(v) -> str:
    return f"{round(v):,}" if isinstance(v, (int, float)) else "미확인"


def find_text(r: dict, *, limit: int = 50) -> str:
    unk = [f"{label} {r['unknown'][k]}" for k, label in zip(UNKNOWN_KEYS, ("용도지역", "공부면적", "공시지가", "소유구분", "규제")) if r["unknown"][k]]
    lines = [f"조건으로 필지 찾기 ({', '.join(f'{k}={v}' for k, v in r['criteria'].items())}) · 기준일 {r['on_date']}",
             f"속성 있는 필지 {r['with_attrs']}개 (정본 필지 {r['parcels']}개) 중 {r['total']}개 일치 (공부면적 큰 순)"
             + (f" · 값 없는 필지: {', '.join(unk)}" if unk else "") + (f" · 값이 없어 맞는지 모르는 필지 {r['undetermined']}개는 결과에서 뺌" if r["undetermined"] else "")]
    for row in r["rows"][:limit]:
        a = row["assets"]
        lines.append(f"  {row['pnu']} {row['parcel']} · {row['use_zone_1'] or '용도지역 미확인'} · {_fmt_int(row['registered_area_m2'])}㎡ · "
                     f"{_fmt_int(row['official_land_price_krw_m2'])}원/㎡ · {row['ownership_kind'] or '소유 미확인'}"
                     + (f" · 저촉 {', '.join(row['restricted_codes'])}" if row["restricted_codes"] else "")
                     + (f" · 물건 {len(a)}개" + (f" (관찰목록 {row['watchlist_assets']})" if row["watchlist_assets"] else "") if a else " · 물건 없음"))
    if r["total"] > limit:
        lines.append(f"  … 앞 {limit}개만 보였다. 전체는 --csv 로 저장한다")
    lines.append(r["note"])
    return "\n".join(lines) + "\n"


def find_csv(r: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_FIELDS)
    for row in r["rows"]:
        w.writerow([row["pnu"], row["parcel"], row["use_zone_1"] or "", row["zone_category"] or "", "" if row["registered_area_m2"] is None else row["registered_area_m2"],
                    "" if row["official_land_price_krw_m2"] is None else row["official_land_price_krw_m2"], row["price_base_year"] or "", row["price_base_month"] or "",
                    row["ownership_kind"] or "", " ".join(row["restricted_codes"]), len(row["assets"]), row["watchlist_assets"],
                    " ".join(a["asset_id"] for a in row["assets"]), row["attrs_as_of"] or ""])
    return buf.getvalue()
