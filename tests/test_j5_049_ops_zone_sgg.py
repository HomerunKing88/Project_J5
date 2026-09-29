"""J5-049: 운영 점검이 범위 규칙의 시군구를 본다 (J5-048 후속).

시험: 거래가 한 시군구뿐이면 시군구 없는 규칙도 통과, 두 시군구 이상인데 규칙에 시군구가 없으면 조치 항목(zone_rule_sgg_missing)과 범위 규칙 줄,
규칙에 시군구를 넣어 새 판으로 반영하면 사라짐, 조치는 백업·파생본 조치보다 먼저. 가상자료만 쓴다(네트워크 없음).
"""

from __future__ import annotations

import json

from j5.collect.rt import collect_months
from j5.db.ops import ops_check, ops_text
from j5.db.transactions import load_run
from j5.db.zones import apply_rules, load_rules
from tests.test_j5_014_collect import KEY, item
from tests.test_j5_017a_ops import db, home  # noqa: F401 (fixture)
from tests.test_j5_042_ops_pending import ENDPOINT, codes, make_ok, opener

RULES = {"kind": "zone_rules", "name": "가상 범위", "core": ["종로5가"], "comparison": [], "note": None}


def collect_load(db, home, lawd):
    rows = [dict(item(i), sggCd=lawd, landUse="일반상업", shareDealingType="") for i in range(2)]
    run = collect_months(home, key=KEY, key_source="t", lawd_cd=lawd, months=["202608"], endpoint=ENDPOINT, opener=opener(rows), sleep=lambda s: None)
    load_run(db, home, lawd, run.run_id)


def rules(tmp_path, **over):
    p = tmp_path / "zones.json"
    p.write_text(json.dumps(dict(RULES, **over), ensure_ascii=False), encoding="utf-8")
    return load_rules(p)


def test_zone_rule_without_sgg_over_many_sggs_is_an_action(db, home, tmp_path):
    collect_load(db, home, "11110")
    apply_rules(db, rules(tmp_path))
    r = make_ok(db, home)
    assert r["zone_rule"] == {"version": 1, "lawd_cd": None, "transaction_sggs": ["11110"]}, "한 시군구뿐이면 시군구 없는 규칙도 통과"
    assert "범위 규칙: v1 · 시군구 정하지 않음 · 거래 시군구 11110" in ops_text(r)
    collect_load(db, home, "11140")
    r2 = ops_check(home)
    assert codes(r2)[0] == "zone_rule_sgg_missing" and "11110, 11140" in r2["actions"][0]["text"] and "rt-zones apply" in r2["actions"][0]["text"]
    assert codes(r2).index("zone_rule_sgg_missing") < codes(r2).index("backup_stale"), "규칙 반영은 백업·파생본 조치보다 먼저"
    apply_rules(db, rules(tmp_path, lawd_cd="11110"))
    r3 = make_ok(db, home)
    assert r3["zone_rule"]["lawd_cd"] == "11110" and "범위 규칙: v2 · 시군구 11110 만" in ops_text(r3)
