"""J5-047: 파생본 transactions.json 의 수집 개월(coverage)과 범위 규칙(zone_rule). 폰 필지 연도별 요약이 0 건과 미수집·범위 밖을 가르는 근거다.

시험: 시군구·연도별 수집 개월(완전·전체, 실패한 달은 전체에만), 범위 규칙의 법정동 목록과 판, PC `parcels-years` 와 같은 수집 개월,
게시 전 확인이 잘못된 coverage·zone_rule 을 거절(해시를 맞춘 변조 뒤에도). 폰 쪽 규칙은 tests/web/view.test.mjs. 가상 서버·가상자료만 쓴다.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from j5.collect.rt import collect_months
from j5.db.projection import ProjectionError, build_projection, verify_projection_dir
from j5.db.transactions import coverage_by_year, coverage_rows, load_run
from j5.db.zones import apply_rules, load_rules
from tests.test_j5_014_collect import KEY, _Handler, server  # noqa: F401 (fixture)
from tests.test_j5_014b3_zones import db, home, load_month, row, rules_file, sequential_run_ids  # noqa: F401 (fixture)


def tamper(out, change):
    listed = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    tx = json.loads((out / "transactions.json").read_text(encoding="utf-8"))
    change(tx)
    data = (json.dumps(tx, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    (out / "transactions.json").write_bytes(data)
    for f in listed["files"]:
        if f["path"] == "transactions.json":
            f["bytes"], f["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    (out / "manifest.json").write_text(json.dumps(listed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_coverage_and_zone_rule_in_transactions_file(db, home, server, tmp_path):
    load_month(db, home, server, [row(0, umdNm="종로5가", jibun="1"), row(1, umdNm="장사동")])
    load_month(db, home, server, [], ym="202607")
    _Handler.scenarios = {"202508": {"kind": "http_error"}}
    run = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=["202508"], endpoint=server, sleep=lambda s: None)
    load_run(db, home, "11110", run.run_id)
    assert coverage_by_year(db, "11110") == {"2026": {"complete": 2, "any": 2}, "2025": {"complete": 0, "any": 1}}
    assert coverage_rows(db) == [{"lawd_cd": "11110", "year": 2025, "months_complete": 0, "months_any": 1},
                                 {"lawd_cd": "11110", "year": 2026, "months_complete": 2, "months_any": 2}]
    apply_rules(db, load_rules(rules_file(tmp_path)))
    r = build_projection(db, home)
    assert r.outcome == "published", r.findings
    out = home / r.output_dir
    tx = json.loads((out / "transactions.json").read_text(encoding="utf-8"))
    assert tx["coverage"] == coverage_rows(db)
    assert tx["zone_rule"] == {"version": 1, "core": ["종로5가", "종로6가", "효제동"], "comparison": ["예지동", "장사동"]}
    for bad, code in ((lambda t: t.update(coverage=[{"lawd_cd": "11110", "year": 2026, "months_complete": 3, "months_any": 2}]), "verify_transactions_coverage"),
                      (lambda t: t.update(coverage=[t["coverage"][0], t["coverage"][0]]), "verify_transactions_coverage"),
                      (lambda t: t.update(coverage=[{"lawd_cd": "1111", "year": 2026, "months_complete": 1, "months_any": 1}]), "verify_transactions_coverage"),
                      (lambda t: t.update(zone_rule={"version": 1, "core": "종로5가", "comparison": []}), "verify_transactions_zone_rule")):
        r2 = build_projection(db, home)
        o2 = home / r2.output_dir
        tamper(o2, bad)
        with pytest.raises(ProjectionError) as e:
            verify_projection_dir(o2)
        assert e.value.code == code
    # 이전 형식(두 키 없음)은 받아들인다: 폰은 모름으로 표시한다
    r3 = build_projection(db, home)
    o3 = home / r3.output_dir
    tamper(o3, lambda t: (t.pop("coverage"), t.pop("zone_rule")))
    assert verify_projection_dir(o3)["counts"]["transactions"] == 2


def test_file_with_zero_transactions_keeps_coverage(db, home, server, tmp_path):
    """리뷰 반영 PR #94: 범위 거래가 0 건이어도(수집했고 결과 없음·범위 밖·취소만) 수집 개월을 싣는 파일을 만든다. 폰이 0 건과 모름을 가른다."""
    apply_rules(db, load_rules(rules_file(tmp_path)))
    r0 = build_projection(db, home)
    assert r0.outcome == "published" and not (home / r0.output_dir / "transactions.json").exists(), "수집 기록도 거래도 없으면 파일이 없다"
    load_month(db, home, server, [row(0, umdNm="당주동"), row(1, umdNm="종로5가", cdealType="O", cdealDay="26.08.20")])   # 범위 밖 1, 취소 확정 1
    load_month(db, home, server, [], ym="202607")                                                                           # 수집했고 결과 0
    r = build_projection(db, home)
    assert r.outcome == "published" and r.counts["transactions"] == 0, r.findings
    out = home / r.output_dir
    tx = json.loads((out / "transactions.json").read_text(encoding="utf-8"))
    assert tx["count"] == 0 and tx["transactions"] == [] and tx["coverage"] == [{"lawd_cd": "11110", "year": 2026, "months_complete": 2, "months_any": 2}]
    assert verify_projection_dir(out)["counts"]["transactions"] == 0
