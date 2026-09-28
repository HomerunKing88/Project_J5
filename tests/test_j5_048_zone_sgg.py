"""J5-048: 범위 규칙에 시군구 코드 (여러 지역 정본, ADR-23).

법정동 이름은 시군구마다 겹칠 수 있다. 규칙 파일에 `lawd_cd` 를 주면 그 시군구의 거래만 핵심·비교로 분류하고, 다른 시군구의 같은 이름 법정동은 범위 밖으로 둔다.
시험: 반영 시 재분류·새 거래 분류(rt-load), 시군구 없는 규칙은 이전처럼 이름으로만, 시군구만 바뀌어도 새 판, 형식 오류 거절, 규칙 글,
파생본 zone_rule 의 lawd_cd 와 범위 밖 거래 제외, 게시 전 확인의 형식 검사. 폰 규칙은 tests/web/view.test.mjs. 가상 서버·가상자료만 쓴다.
"""

from __future__ import annotations

import json

import pytest

from j5.collect.rt import collect_months
from j5.db.projection import ProjectionError, build_projection, verify_projection_dir
from j5.db.transactions import load_run
from j5.db.validate import ValidationError
from j5.db.zones import RULES_SCHEMA, apply_rules, current_rules, load_rules, rules_text, zone_counts
from j5.schemas_loader import schema_errors
from tests.test_j5_014_collect import KEY, _Handler, server  # noqa: F401 (fixture)
from tests.test_j5_014b3_zones import RULES, db, home, load_month, row, rules_file, sequential_run_ids  # noqa: F401 (fixture)
from tests.test_j5_047_year_summary_data import tamper

OTHER = "11140"   # 다른 시군구. 같은 이름 "종로5가" 법정동이 있다고 가정한 가상 거래


def load_other(db, home, server, items, ym="202608"):
    _Handler.scenarios = {ym: {"kind": "pages", "items": items}}
    run = collect_months(home, key=KEY, key_source="test", lawd_cd=OTHER, months=[ym], endpoint=server, sleep=lambda s: None)
    load_run(db, home, OTHER, run.run_id)


def zones_by_lawd(db):
    return sorted((r["lawd_cd"], r["emd_name"], r["zone"]) for r in db.conn.execute("SELECT lawd_cd, emd_name, zone FROM transactions"))


def test_rule_with_sgg_classifies_other_sgg_outside(db, home, server, tmp_path):
    load_month(db, home, server, [row(0, umdNm="종로5가"), row(1, umdNm="장사동")])
    load_other(db, home, server, [row(2, umdNm="종로5가", sggCd=OTHER)])
    r = apply_rules(db, load_rules(rules_file(tmp_path, dict(RULES, lawd_cd="11110"))))
    assert r.version == 1 and zones_by_lawd(db) == [("11110", "장사동", "comparison"), ("11110", "종로5가", "core"), (OTHER, "종로5가", "outside")]
    assert current_rules(db)["lawd_cd"] == "11110" and "시군구: 11110 만" in rules_text(current_rules(db), zone_counts(db))
    # 새로 반영하는 거래도 같은 규칙으로 분류한다 (rt-load)
    load_other(db, home, server, [row(3, umdNm="종로5가", sggCd=OTHER)], ym="202607")
    assert [z for (lawd, _, z) in zones_by_lawd(db) if lawd == OTHER] == ["outside", "outside"]
    # 시군구 없는 규칙: 이전처럼 이름으로만 (다른 시군구의 같은 이름 동도 범위에 든다). 시군구만 바뀌어도 새 판이다
    r2 = apply_rules(db, load_rules(rules_file(tmp_path, RULES)))
    assert r2.outcome == "applied" and r2.version == 2 and current_rules(db)["lawd_cd"] is None
    assert [z for (lawd, _, z) in zones_by_lawd(db) if lawd == OTHER] == ["core", "core"]
    assert "정하지 않음" in rules_text(current_rules(db), zone_counts(db))
    assert apply_rules(db, load_rules(rules_file(tmp_path, RULES))).outcome == "unchanged"


def test_rule_file_validation():
    assert schema_errors(RULES_SCHEMA, dict(RULES, lawd_cd="11110")) == [] and schema_errors(RULES_SCHEMA, dict(RULES, lawd_cd=None)) == []
    assert schema_errors(RULES_SCHEMA, dict(RULES, lawd_cd="1111")) and schema_errors(RULES_SCHEMA, dict(RULES, lawd_cd=11110))


def test_rule_file_rejects_bad_sgg(tmp_path):
    with pytest.raises(ValidationError):
        load_rules(rules_file(tmp_path, dict(RULES, lawd_cd="종로구")))


def test_projection_zone_rule_carries_sgg(db, home, server, tmp_path):
    load_month(db, home, server, [row(0, umdNm="종로5가")])
    load_other(db, home, server, [row(2, umdNm="종로5가", sggCd=OTHER)])
    apply_rules(db, load_rules(rules_file(tmp_path, dict(RULES, lawd_cd="11110"))))
    r = build_projection(db, home)
    assert r.outcome == "published" and r.counts["transactions"] == 1, "다른 시군구의 같은 이름 동 거래는 파생본에 없다"
    out = home / r.output_dir
    tx = json.loads((out / "transactions.json").read_text(encoding="utf-8"))
    assert tx["zone_rule"]["lawd_cd"] == "11110" and [t["lawd_cd"] for t in tx["transactions"]] == ["11110"]
    tamper(out, lambda t: t["zone_rule"].update(lawd_cd="1111"))
    with pytest.raises(ProjectionError) as e:
        verify_projection_dir(out)
    assert e.value.code == "verify_transactions_zone_rule"


def test_verifier_requires_ascii_digit_sgg(db, home, server, tmp_path):
    """리뷰 반영 PR #95: 게시 전 확인은 시군구 코드를 ASCII 숫자 다섯 자리로만 받는다(전각 숫자는 폰 검증에서 거절되므로)."""
    load_month(db, home, server, [row(0, umdNm="종로5가")])
    apply_rules(db, load_rules(rules_file(tmp_path, dict(RULES, lawd_cd="11110"))))
    for change, code in ((lambda t: t["zone_rule"].update(lawd_cd="１１１１０"), "verify_transactions_zone_rule"),
                         (lambda t: t["coverage"][0].update(lawd_cd="１１１１０"), "verify_transactions_coverage")):
        r = build_projection(db, home)
        out = home / r.output_dir
        tamper(out, change)
        with pytest.raises(ProjectionError) as e:
            verify_projection_dir(out)
        assert e.value.code == code
