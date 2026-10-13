"""J5-068: 여러 시군구의 범위 규칙 (ADR-23 여러 지역 정본, J5-048 후속).

범위 규칙 목록 항목에 시군구 5자리를 붙이면(`11140:을지로4가`) 그 시군구의 그 법정동만 뜻한다. 이름만 쓴 항목은 이전처럼 규칙의 lawd_cd 시군구(없으면 모든 시군구)에 맞는다.
시험: 입력 정리·형식 거절, 분류 진리표, 두 시군구의 같은 이름 법정동을 각각 다른 범위로 반영, 규칙 글, 운영 점검(모두 시군구를 붙였으면 조치 없음),
파생본 zone_rule 과 게시 전 확인. 폰 규칙은 tests/web/view.test.mjs. 가상 서버·가상자료만 쓴다.
"""

from __future__ import annotations

import json

import pytest

from j5.db.ops import ops_check
from j5.db.projection import build_projection, verify_projection_dir
from j5.db.validate import ValidationError
from j5.db.zones import apply_rules, classify, current_rules, has_plain_entries, load_rules, rules_text, split_entry, zone_counts
from tests.test_j5_014_collect import _Handler, server  # noqa: F401 (fixture)
from tests.test_j5_014b3_zones import db, home, load_month, row, rules_file, sequential_run_ids  # noqa: F401 (fixture)
from tests.test_j5_042_ops_pending import codes, make_ok
from tests.test_j5_048_zone_sgg import OTHER, load_other, zones_by_lawd

MULTI = {"kind": "zone_rules", "name": "두 시군구", "core": ["11110:종로5가", " 11140 : 을지로4가 "], "comparison": [f"{OTHER}:종로5가", "예지동"], "note": None}


def test_entries_normalized_and_bad_forms_rejected(tmp_path):
    r = load_rules(rules_file(tmp_path, MULTI))
    assert r["core"] == ["11110:종로5가", "11140:을지로4가"] and r["comparison"] == ["11140:종로5가", "예지동"]
    assert split_entry("11140:을지로4가") == ("11140", "을지로4가") and split_entry("예지동") == (None, "예지동")
    assert has_plain_entries(r) and not has_plain_entries({"core": ["11110:종로5가"], "comparison": []})
    for bad in (["1114:종로5가"], ["11110:"], ["11110:종로5가", "11110 : 종로5가"]):
        with pytest.raises(ValidationError):
            load_rules(rules_file(tmp_path, dict(MULTI, core=bad, comparison=[])))
    with pytest.raises(ValidationError):
        load_rules(rules_file(tmp_path, dict(MULTI, core=["11110:종로5가"], comparison=["11110:종로5가"])))


def test_classify_truth_table():
    r = {"core": ["종로5가", "11140:을지로4가"], "comparison": ["11140:종로5가", "예지동"], "lawd_cd": "11110"}
    cases = {("종로5가", "11110"): "core", ("종로5가", "11140"): "comparison", ("을지로4가", "11140"): "core", ("을지로4가", "11110"): "outside",
             ("예지동", "11140"): "outside", ("예지동", "11110"): "comparison", ("효제동", "11110"): "outside", (None, "11110"): "outside"}
    assert {k: classify(r, *k) for k in cases} == cases
    no_sgg = dict(r, lawd_cd=None)
    assert classify(no_sgg, "예지동", "11140") == "comparison", "규칙에 시군구가 없으면 이름만 쓴 항목은 모든 시군구에 맞는다"
    assert classify(no_sgg, "종로5가", "11140") == "comparison", "시군구를 붙인 항목이 이름만 쓴 항목보다 먼저 맞는다"
    assert classify(no_sgg, "종로5가", "11110") == "core"


def test_same_dong_name_in_two_sggs_gets_two_zones(db, home, server, tmp_path):
    load_month(db, home, server, [row(0, umdNm="종로5가"), row(1, umdNm="예지동")])
    load_other(db, home, server, [row(2, umdNm="종로5가", sggCd=OTHER), row(3, umdNm="예지동", sggCd=OTHER)])
    r = apply_rules(db, load_rules(rules_file(tmp_path, dict(MULTI, core=["11110:종로5가"], comparison=[f"{OTHER}:종로5가", "11110:예지동"]))))
    assert r.version == 1
    assert zones_by_lawd(db) == [("11110", "예지동", "comparison"), ("11110", "종로5가", "core"), (OTHER, "예지동", "outside"), (OTHER, "종로5가", "comparison")]
    text = rules_text(current_rules(db), zone_counts(db))
    assert f"시군구: 법정동마다 시군구를 붙임 (11110, {OTHER})" in text and "핵심: 11110:종로5가" in text
    apply_rules(db, load_rules(rules_file(tmp_path, dict(MULTI, lawd_cd="11110"))))
    assert "시군구: 이름만 쓴 법정동은 11110 만 · 시군구를 붙인 법정동: 11110, 11140" in rules_text(current_rules(db), zone_counts(db))


def test_ops_no_sgg_action_when_every_entry_is_qualified(db, home, server, tmp_path):
    load_month(db, home, server, [row(0, umdNm="종로5가")])
    load_other(db, home, server, [row(1, umdNm="종로5가", sggCd=OTHER)])
    apply_rules(db, load_rules(rules_file(tmp_path, dict(MULTI, core=["11110:종로5가"], comparison=[f"{OTHER}:종로5가"]))))
    r = make_ok(db, home)
    assert "zone_rule_sgg_missing" not in codes(r) and r["zone_rule"]["transaction_sggs"] == ["11110", OTHER]
    apply_rules(db, load_rules(rules_file(tmp_path, dict(MULTI, core=["종로5가"], comparison=[f"{OTHER}:종로5가"]))))
    assert "zone_rule_sgg_missing" in codes(ops_check(home)), "이름만 쓴 항목이 남아 있고 시군구가 없으면 이전처럼 조치 항목"


def test_projection_carries_qualified_entries(db, home, server, tmp_path):
    load_month(db, home, server, [row(0, umdNm="종로5가")])
    load_other(db, home, server, [row(1, umdNm="종로5가", sggCd=OTHER)])
    apply_rules(db, load_rules(rules_file(tmp_path, dict(MULTI, core=["11110:종로5가"], comparison=[f"{OTHER}:종로5가"]))))
    p = build_projection(db, home)
    assert p.outcome == "published", p.findings
    out = home / p.output_dir
    tx = json.loads((out / "transactions.json").read_text(encoding="utf-8"))
    assert tx["zone_rule"]["core"] == ["11110:종로5가"] and tx["zone_rule"]["comparison"] == [f"{OTHER}:종로5가"] and tx["count"] == 2
    assert sorted((t["lawd_cd"], t["zone"]) for t in tx["transactions"]) == [("11110", "core"), (OTHER, "comparison")]
    verify_projection_dir(out)
