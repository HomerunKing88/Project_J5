"""J5-050: 운영 점검이 거래 응답의 시군구(sggCd)를 요청 시군구(lawd_cd)와 대조한다 (J5-044 미확인 변수).

시험: 모두 같으면 안내 없음과 "거래 응답 시군구" 줄, 다른 거래·응답에 없는 거래를 따로 셈, 다르면 안내(조치 항목 아님, 점검 통과),
다시 받은 달도 거래마다 한 번만 셈. 가상자료만 쓴다(네트워크 없음).
"""

from __future__ import annotations

from j5.collect.rt import collect_months
from j5.db.ops import ops_text
from j5.db.transactions import load_run
from tests.test_j5_014_collect import KEY, item
from tests.test_j5_017a_ops import db, home  # noqa: F401 (fixture)
from tests.test_j5_042_ops_pending import ENDPOINT, make_ok, opener


def collect_load(db, home, rows, ym="202608"):
    rows = [dict(r, landUse="일반상업", shareDealingType="") for r in rows]
    run = collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=[ym], endpoint=ENDPOINT, opener=opener(rows), sleep=lambda s: None)
    load_run(db, home, "11110", run.run_id)


def test_response_sgg_compared_with_requested_sgg(db, home):
    collect_load(db, home, [dict(item(i), sggCd="11110") for i in range(2)])
    r = make_ok(db, home)
    assert r["transaction_sgg"] == {"same": 2, "differs": 0, "missing": 0, "pairs": []}
    assert "거래 응답 시군구: 요청과 같음 2건 · 다름 0건 · 응답에 없음 0건" in ops_text(r)
    assert not any("sggCd" in w for w in r["warnings"])
    # 같은 달을 다시 받아도 거래마다 한 번만 센다
    collect_load(db, home, [dict(item(i), sggCd="11110") for i in range(2)])
    assert make_ok(db, home)["transaction_sgg"]["same"] == 2
    # 요청과 다른 응답 시군구·응답에 없는 시군구는 따로 센다. 고칠 명령이 없으므로 안내이며 점검은 통과한다
    collect_load(db, home, [dict(item(2), sggCd="11140"), dict(item(3), sggCd="11140"), dict(item(4), sggCd=" ")], ym="202607")
    r2 = make_ok(db, home)
    assert r2["transaction_sgg"] == {"same": 2, "differs": 2, "missing": 1, "pairs": [{"lawd_cd": "11110", "sgg_cd": "11140", "count": 2}]}
    assert "다름 2건 · 응답에 없음 1건" in ops_text(r2)
    assert any("거래 2건의 응답 시군구(sggCd)" in w and "요청 11110 → 응답 11140 2건" in w for w in r2["warnings"])
