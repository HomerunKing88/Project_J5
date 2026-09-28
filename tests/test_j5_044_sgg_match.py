"""J5-044: 지번 대조에 시군구 코드를 넣는다 (여러 지역을 한 정본에, ADR-23).

법정동 이름은 시군구마다 겹칠 수 있다. 거래의 시군구 코드(lawd_cd)와 필지 PNU 앞 5자리가 다르면 이름·지번이 같아도 이 필지의 거래가 아니다.
시험: 필지 이력(PC)은 다른 시군구의 같은 이름 법정동 거래를 넣지 않고 수만 센다(취소 확정 제외), 연결 후보 CSV 도 후보를 붙이지 않는다,
수동으로 확정 연결한 거래는 시군구가 달라도 연결 물건의 거래로 들고 그 줄에 적는다. 폰 규칙은 tests/web/view.test.mjs. 가상 서버·가상자료만 쓴다.
"""

from __future__ import annotations

from j5.collect.rt import collect_months
from j5.db.parcel_timeline import parcel_transactions, with_transactions, years_text
from j5.db.parcels import attribute_history
from j5.db.transactions import load_run
from j5.db.txlinks import apply_decisions, candidates
from tests.test_j5_014_collect import KEY, _Handler, server  # noqa: F401 (fixture)
from tests.test_j5_014b2_txlinks import A, db, home, load_month, row, sequential_run_ids, tid_of  # noqa: F401 (fixture)

P1 = "9999900100100010000"
OTHER = "11110"   # 가상 필지(99999)와 다른 시군구. 법정동 이름은 같은 "가상동"


def other_row(i: int, **over) -> dict:
    return row(i, sggCd=OTHER, **over)


def load_other(db, home, server, items, ym="202608"):
    _Handler.scenarios = {ym: {"kind": "pages", "items": items}}
    run = collect_months(home, key=KEY, key_source="test", lawd_cd=OTHER, months=[ym], endpoint=server, sleep=lambda s: None)
    load_run(db, home, OTHER, run.run_id)


def test_other_sgg_same_emd_name_is_not_this_parcel(db, home, server):
    load_month(db, home, server, [row(0, jibun="1")])
    load_other(db, home, server, [other_row(1, jibun="1"), other_row(2, jibun="1-*"), other_row(3, jibun="1", cdealType="O", cdealDay="26.08.30")])
    r = parcel_transactions(db, P1)
    assert [(t["lawd_cd"], t["jibun"], t["match"]) for t in r["transactions"]] == [("99999", "1", "exact")]
    assert r["other_sgg"] == 2 and r["cancelled"] == 0, "다른 시군구의 거래는 수만 센다. 취소 확정은 어느 쪽에도 세지 않는다"
    text = years_text(with_transactions(db, attribute_history(db, P1)))
    assert "실거래 1건: 같은 필지 1" in text and "다른 시군구의 같은 이름 법정동 2건 제외" in text
    # 연결 후보 CSV: 같은 이름 법정동·같은 지번이어도 시군구가 다르면 후보가 아니다
    assert [(r_["jibun"], r_["candidate_asset_ids"]) for r_ in candidates(db, OTHER, ["202608"])] == [("1", ""), ("1-*", "")]
    assert [(r_["jibun"], r_["candidate_asset_ids"]) for r_ in candidates(db, "99999", ["202608"])] == [("1", A[0])]


def test_manual_link_across_sgg_is_shown_as_linked(db, home, server):
    load_other(db, home, server, [other_row(1, jibun="1"), other_row(2, jibun="1-*")])
    t = tid_of(db, other_row(1, jibun="1"))
    apply_decisions(db, [{"_line": 2, "transaction_id": t, "decision": "confirmed", "asset_id": A[0], "basis_kind": "document", "reviewed_on": "2026-09-28", "note": "등기 확인 (가상)"}])
    r = parcel_transactions(db, P1)
    assert [(x["transaction_id"], x["match"], x["sgg_differs"]) for x in r["transactions"]] == [(t, "linked", True)], "사람이 확정한 연결은 시군구가 달라도 보인다"
    assert r["other_sgg"] == 1
    text = years_text(with_transactions(db, attribute_history(db, P1)))
    assert "실거래 연결 물건의 거래" in text and "시군구 코드 다름(거래 11110)" in text and "다른 시군구의 같은 이름 법정동 1건 제외" in text
