"""J5-052: 운영 점검이 시군구마다 최근 거래 수집(완전히 받은 마지막 계약월·마지막 성공 수집)을 보인다 (릴리스 계획 §11).

시험: 지난달까지 받았으면 안내 없음과 "최근 거래 수집" 줄, 두 달 이상 비면 받을 범위와 명령을 담은 안내(조치 항목 아님, 점검 통과),
완전히 받은 달이 없는 시군구(실패만)는 "완전히 받은 달 없음". 가상자료만 쓴다(네트워크 없음).
"""

from __future__ import annotations

from datetime import datetime, timezone

from j5.collect.rt import collect_months
from j5.db.ops import ops_check, ops_text, recent_collection
from j5.db.transactions import load_run
from tests.test_j5_014_collect import KEY, item
from tests.test_j5_017a_ops import db, home  # noqa: F401 (fixture)
from tests.test_j5_042_ops_pending import ENDPOINT, make_ok, opener


def collect_load(db, home, lawd, months, op=None):
    rows = [dict(item(i), sggCd=lawd, landUse="일반상업", shareDealingType="") for i in range(2)]
    run = collect_months(home, key=KEY, key_source="t", lawd_cd=lawd, months=months, endpoint=ENDPOINT, opener=op or opener(rows), sleep=lambda s: None)
    load_run(db, home, lawd, run.run_id)
    return run


def failing(url):
    return 500, b"server error"


def test_recent_collection_line_and_notice(db, home):
    collect_load(db, home, "11110", ["202607", "202608"])
    make_ok(db, home)
    r = ops_check(home, now="2026-09-30T00:00:00Z")
    c = r["recent_collection"][0]
    assert (c["lawd_cd"], c["latest_complete_ym"], c["closed_ym"], c["months_behind"]) == ("11110", "202608", "202608", 0) and c["last_success_at"]
    assert "최근 거래 수집: 시군구 11110 · 계약월 2026-08 까지 완전 수집 · 마지막 성공 수집" in ops_text(r)
    assert not any("최근 거래:" in w for w in r["warnings"])
    # 두 달 이상 비면 받을 범위와 명령을 안내한다. 조치 항목이 아니므로 점검은 통과한다
    r2 = ops_check(home, now="2026-11-15T00:00:00Z")
    assert r2["ok"] and r2["recent_collection"][0]["months_behind"] == 2
    w = [x for x in r2["warnings"] if "시군구 11110 최근 거래" in x]
    assert w and "계약월 2026-08 뒤로 2026-10 까지 받지 않았다" in w[0] and "--from 2026-09 --to 2026-10" in w[0] and "rt-load --lawd-cd 11110" in w[0]
    # 한 달만 비면 안내 없음 (신고 기한 안의 달)
    assert not any("최근 거래:" in x for x in ops_check(home, now="2026-10-15T00:00:00Z")["warnings"])


def test_sgg_without_complete_month(db, home):
    collect_load(db, home, "11140", ["202608"], op=failing)
    rc = recent_collection(db, datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert [(c["lawd_cd"], c["latest_complete_ym"], c["months_behind"], c["last_success_at"]) for c in rc] == [("11140", None, None, None)]
    r = ops_check(home, now="2026-09-30T00:00:00Z")
    assert "최근 거래 수집: 시군구 11140 · 완전히 받은 달 없음 · 성공한 수집 없음" in ops_text(r)
    assert any("시군구 11140 최근 거래: 완전히 받은 달이 없고 2026-08 까지" in x and "--from 2026-08 --to 2026-08" in x for x in r["warnings"])
