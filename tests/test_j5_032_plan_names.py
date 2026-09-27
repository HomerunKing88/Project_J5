"""J5-032 토지이용계획 이름 목록 정정 시험 (ADR-19 정정).

실측(VWorld 3구역 2,724행): 코드·관계 목록은 온전하고 1:1, 이름 목록은 모든 행이 열 한도(254바이트)에서 잘렸고 순서가 코드와 맞지 않는 행이 있다.
- 파서: 코드·관계만 짝(plan_zones, name null), 이름은 적힌 순서 그대로(plan_zone_names), 열 한도에 닿으면 잘림·끊긴 마지막 조각 제외, 관계 수가 다르면 관계 null.
- 정본(마이그레이션 18): 옛 형식(J5-025, 코드에 이름이 위치로 붙음) 행은 새 형식 번들을 같은 기준일로 다시 넣으면 교체된다. 이름 목록이 빈 옛 번들은 다시 넣어도 변화 없음(해시 안정).
- 파생본·PC 글: 이름 목록이 실리고 코드는 관계와 함께 보인다. 묶음 넣기의 번들 키에 해석 규칙의 판이 든다.
가상자료만 쓴다.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from j5.db import ingest as ING
from j5.db import schema as S
from j5.db.parcels import _fmt_attr_value, load_bundle, parcels_bundle_from_db
from j5.parcels import vworld
from j5.parcels.vworld import _plan_attrs
from j5.schemas_loader import schema_errors
from tests.test_j5_026_attr_history import P1, db, home, vw_bundle  # noqa: F401 (fixture)


def test_plan_parser_rules():
    # 실측 모양: 이름이 코드보다 많고(한 코드의 이름이 두 번) 목록이 254바이트 한도에서 끊김
    names = "과밀억제권역,정비구역(가상1구역),가축사육제한구역,상대보호구역,상대보호구역,도시지역,일반상업지역,지구단위계획구역(종로"
    raw = names + "가" * ((254 - len(names.encode("utf-8"))) // 3)
    row = {"prpos_area_dstrc_code_list": "UBA100,UDT100,UMZ100,UOA120,UQA01X,UQA220,UQQ300,UQQ600,UQQ600", "cnflc_at_nm_list": "포함,포함,포함,저촉,포함,포함,포함,포함,접함",
           "prpos_area_dstrc_nm_list": raw}
    a = _plan_attrs(row, 254)
    assert a["plan_zones_truncated"] is True
    assert [z["code"] for z in a["plan_zones"]] == ["UBA100", "UDT100", "UMZ100", "UOA120", "UQA01X", "UQA220", "UQQ300", "UQQ600", "UQQ600"]
    assert all(z["name"] is None for z in a["plan_zones"]) and a["plan_zones"][3]["relation"] == "저촉" and a["plan_zones"][8]["relation"] == "접함"
    assert a["plan_zone_names"] == ["과밀억제권역", "정비구역(가상1구역)", "가축사육제한구역", "상대보호구역", "상대보호구역", "도시지역", "일반상업지역"], "끊긴 마지막 조각은 뺀다"
    # 한도에 닿지 않으면 잘리지 않았다 (이름 수가 코드 수와 달라도)
    b = _plan_attrs({"prpos_area_dstrc_code_list": "A,B", "cnflc_at_nm_list": "포함,저촉", "prpos_area_dstrc_nm_list": "가,나,다"}, 254)
    assert b["plan_zones_truncated"] is False and b["plan_zone_names"] == ["가", "나", "다"]
    # 관계 수가 코드 수와 다르면 관계를 붙이지 않는다. 열 길이를 모르면 잘림 판정도 하지 않는다
    c = _plan_attrs({"prpos_area_dstrc_code_list": "A,B", "cnflc_at_nm_list": "포함", "prpos_area_dstrc_nm_list": "가"}, None)
    assert [z["relation"] for z in c["plan_zones"]] == [None, None] and c["plan_zones_truncated"] is False
    # 빈 행
    e = _plan_attrs({}, 254)
    assert e == {"plan_zones": [], "plan_zone_names": [], "plan_zones_truncated": False}


def _old_format(bundle: dict) -> dict:
    """J5-025 형식: 코드에 이름을 위치로 붙이고 이름 목록 필드가 없다. 잘린 행에는 J5-025 가 남긴 끊긴 이름 조각이 코드에 붙어 있다."""
    b = copy.deepcopy(bundle)
    for f in b["features"]:
        a = f["properties"]["attrs"]
        names = list(a.pop("plan_zone_names", []))
        if a.get("plan_zones_truncated"):
            names.append("토지거래계약에관한허가구역(가상 아주")   # J5-025 는 열 한도에서 끊긴 조각을 이름으로 남겼다
        a["plan_zones"] = [dict(z, name=names[i] if i < len(names) else None) for i, z in enumerate(a["plan_zones"])]
    return b


def test_schema_18_and_old_rows_are_replaced_by_new_format(db):
    assert S.DB_SCHEMA_VERSION == 18 and db.schema_version() == 18 and db.status()["ok"]
    old = _old_format(vw_bundle())
    assert schema_errors("parcels_bundle.schema.json", old) == [], "옛 형식 번들도 받는다"
    load_bundle(db, old)
    assert load_bundle(db, _old_format(vw_bundle())).outcome == "unchanged", "이름 목록이 빈 옛 번들은 다시 넣어도 변화 없음 (해시 안정)"
    # 옛 형식 번들은 반영할 때 정규화한다 (리뷰 반영 PR #79): 코드의 이름을 떼어 이름 목록으로, 잘렸으면 끊긴 마지막 조각 제외
    row = db.conn.execute("SELECT a.plan_zones_json, a.plan_zone_names_json FROM parcel_attributes a JOIN parcels p USING (parcel_id) WHERE p.pnu = ?", (P1,)).fetchone()
    assert json.loads(row[1]) == ["도시지역", "일반상업지역", "지구단위계획구역(가상)"] and all(z["name"] is None for z in json.loads(row[0]))
    # 새 형식 번들을 같은 기준일로 넣으면: 옛 형식에서 잃은 이름(코드 수보다 많은 이름)만 채워진다. 나머지는 이미 같다
    r = load_bundle(db, vw_bundle())
    assert r.outcome == "applied" and r.attrs_updated == 1 and r.snapshots_replaced == 1 and r.snapshots_inserted == 0, r.to_text()
    row = db.conn.execute("SELECT a.plan_zones_json, a.plan_zone_names_json FROM parcel_attributes a JOIN parcels p USING (parcel_id) WHERE p.pnu = ?", (P1,)).fetchone()
    assert json.loads(row[1]) == ["도시지역", "일반상업지역", "지구단위계획구역(가상)"] and all(z["name"] is None for z in json.loads(row[0]))
    snap = db.conn.execute("SELECT s.values_json FROM parcel_attribute_snapshots s JOIN parcels p USING (parcel_id) WHERE p.pnu = ? AND s.kind = 'land_plan'", (P1,)).fetchone()
    assert json.loads(snap[0])["plan_zone_names"] == ["도시지역", "일반상업지역", "지구단위계획구역(가상)"]
    assert load_bundle(db, vw_bundle()).outcome == "unchanged"
    # 파생본 필지 번들에 이름 목록이 실리고 스키마를 통과한다. 이력에는 이름 목록 변화가 보인다
    b = parcels_bundle_from_db(db, generated_at="2026-09-28T00:00:00Z", source_dataset_version=int(db.meta("dataset_version")))
    assert schema_errors("parcels_bundle.schema.json", b) == []
    f1 = next(f for f in b["features"] if f["id"] == P1)["properties"]
    assert f1["attrs"]["plan_zone_names"] == ["도시지역", "일반상업지역", "지구단위계획구역(가상)"]


def test_pc_text_shows_codes_with_relation_and_names():
    assert _fmt_attr_value("plan_zones", [{"code": "UQA01X", "name": None, "relation": "포함"}, {"code": "UQS100", "name": "옛 이름", "relation": "저촉"}]) == "UQA01X, UQS100(저촉)"
    assert _fmt_attr_value("plan_zone_names", ["도시지역", "상대보호구역"]) == "도시지역, 상대보호구역"
    assert _fmt_attr_value("plan_zone_names", []) == "없음"


def test_ingest_bundle_key_includes_parser_version(tmp_path, monkeypatch):
    src = tmp_path / "zone.zip"
    src.write_bytes(b"x")
    a = ING._bundle_path(tmp_path, src, "2026-09-05", name="n", license=None, bundle_mode="real")
    monkeypatch.setattr(vworld, "PLAN_PARSER_VERSION", "999")
    b = ING._bundle_path(tmp_path, src, "2026-09-05", name="n", license=None, bundle_mode="real")
    assert a != b and a.parent == b.parent


def test_legacy_bundle_does_not_erase_corrected_names(db):
    """리뷰 반영 PR #79 (P1): 정정된 정본에 같은 기준일의 옛 형식 번들이 들어와도 이름 목록을 지우거나 위치 짝을 되살리지 않는다.
    더 새로운 기준일의 옛 형식 번들은 그 자료의 이름을 정규화해 쓴다."""
    load_bundle(db, vw_bundle())
    r = load_bundle(db, _old_format(vw_bundle()))
    p3 = "9999900100100030000"
    names = lambda pnu: json.loads(db.conn.execute("SELECT a.plan_zone_names_json FROM parcel_attributes a JOIN parcels p USING (parcel_id) WHERE p.pnu = ?", (pnu,)).fetchone()[0])
    zones = lambda pnu: json.loads(db.conn.execute("SELECT a.plan_zones_json FROM parcel_attributes a JOIN parcels p USING (parcel_id) WHERE p.pnu = ?", (pnu,)).fetchone()[0])
    assert r.outcome == "unchanged", r.to_text()
    assert names(p3) == ["도시지역", "상대보호구역", "상대보호구역(가상)", "준공업지역"], "코드 수보다 많은 이름도 지키고"
    assert all(z["name"] is None for z in zones(p3)), "위치 짝을 되살리지 않는다"
    newer = _old_format(vw_bundle())
    newer["source"]["geometry_version"] = "2026-10-01"
    load_bundle(db, newer)
    assert names(P1) == ["도시지역", "일반상업지역", "지구단위계획구역(가상)"] and all(z["name"] is None for z in zones(P1))
    assert names(p3) == ["도시지역", "상대보호구역", "상대보호구역(가상)"], "새 기준일의 옛 형식은 그 자료의 이름(코드 수까지)을 쓴다"
    p2 = "9999900100100020000"
    assert names(p2) == ["도시지역", "제3종일반주거지역", "준주거지역"], "잘림 표시가 있으면 끊긴 조각은 이미 없다"
