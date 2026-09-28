"""J5-043: PC 필지 이력에 이 필지에 닿는 실거래를 연도별로 (db parcels-history).

시험: 같은 필지(지번 일치)·번지대(부번 마스킹)·연결 물건의 확정 거래(지번 불일치)를 모으고, 다른 필지·다른 법정동·앞자리 없는 마스킹(*)은 넣지 않음,
취소 확정은 목록에서 빼고 수만, 계약연도별 묶음(최근 먼저), 시군구 코드가 다르면 표시, 정본에 쓰지 않음, CLI 글·JSON. 가상 서버·가상자료만 쓴다.
"""

from __future__ import annotations

import json

from j5 import cli
from j5.db.parcel_timeline import fmt_krw, parcel_transactions, with_transactions, years_text
from j5.db.parcels import attribute_history
from j5.db.txlinks import apply_decisions, jibun_matches, parse_jibun
from tests.test_j5_014_collect import server  # noqa: F401 (fixture)
from tests.test_j5_014b2_txlinks import A, db, home, load_month, row, sequential_run_ids, tid_of  # noqa: F401 (fixture)

P1, P42 = "9999900100100010000", "9999900100100040002"


def old_row(i: int, **over) -> dict:
    return row(i, dealYear="2021", dealMonth="9", **over)


def loaded(db, home, server):
    load_month(db, home, server, [row(0, jibun="1"), row(1, jibun="1-*"), row(2, jibun="1-1"), row(3, jibun="산1-2"), row(4, jibun="*"), row(5, jibun="9"),
                                  row(6, jibun="1", cdealType="O", cdealDay="26.08.30"), row(7, jibun="1", umdNm="다른동")])
    load_month(db, home, server, [old_row(0, jibun="1"), old_row(1, jibun="4-*")], ym="202109")
    t9 = tid_of(db, row(5, jibun="9"))
    apply_decisions(db, [{"_line": 2, "transaction_id": t9, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual", "reviewed_on": "2026-09-25", "note": "현장 확인 (가상)"}])
    return t9


def test_transactions_touching_parcel(db, home, server):
    t9 = loaded(db, home, server)
    v = db.status()["dataset_version"]
    r = parcel_transactions(db, P1)
    got = [(t["deal_ymd"], t["jibun"], t["match"]) for t in r["transactions"]]
    assert got == [("202109", "1", "exact"), ("202608", "1", "exact"), ("202608", "1-*", "prefix"), ("202608", "9", "linked")], got
    assert r["cancelled"] == 1, "취소 확정 거래는 목록에 없고 수만 센다"
    linked = next(t for t in r["transactions"] if t["match"] == "linked")
    assert linked["transaction_id"] == t9 and linked["asset_id"] == A[0] and linked["linked_here"]
    assert all(t["sgg_differs"] and t["lawd_cd"] == "11110" for t in r["transactions"]), "가상 필지(99999)와 거래(11110)의 시군구 코드가 다르다는 표시"
    assert r["sgg_differs"] == 4
    # 4-2 필지: 2021 의 4-* 는 번지대, 물건 3 에 연결됐지만 확정 연결 거래가 없다
    assert [(t["deal_ymd"], t["match"]) for t in parcel_transactions(db, P42)["transactions"]] == [("202109", "prefix")]
    assert db.status()["dataset_version"] == v, "정본에 쓰지 않는다"


def test_years_and_text(db, home, server):
    loaded(db, home, server)
    h = with_transactions(db, attribute_history(db, P1))
    assert [(y["year"], y["counts"]) for y in h["years"] if y["transactions"]] == [("2026", {"exact": 1, "prefix": 1, "linked": 1}), ("2021", {"exact": 1, "prefix": 0, "linked": 0})]
    text = years_text(h)
    assert "실거래 4건: 같은 필지 2 · 번지대 1 · 연결 물건의 거래 1 · 취소 확정 1건 제외" in text
    assert text.index("  2026:") < text.index("  2021:"), "최근 연도 먼저"
    assert "2026-08-02 실거래 번지대 (필지 미확정)" in text and "(가상동 1-*)" in text and "확정 연결" in text and "시군구 코드 다름(거래 11110)" in text
    assert "이 필지의 거래로 확정하지 않는다" in text
    assert [fmt_krw(x) for x in (1e9, 1.23e9, 15e8, 85e6, 9000, None)] == ["10억", "12.3억", "15억", "8,500만", "9,000원", "금액 미확인"], "폰 fmtKrw 와 같다"


def test_fmt_krw_rounds_half_up_like_phone():
    """리뷰 반영 PR #90: 억 자리의 반은 폰 toFixed 처럼 올린다. 기대값은 node 로 web/app/view.js fmtKrw 를 돌린 결과다."""
    cases = {10_050_000_000: "101억", 1_225_000_000: "12.3억", 1_235_000_000: "12.3억", 1_215_000_000: "12.2억", 12_345_678_901: "123억",
             105_000: "11만", 15_000: "2만", 100_000_000: "1억", 104_000_000: "1억", 99_999: "10만"}
    assert {v: fmt_krw(v) for v in cases} == cases


def test_no_transactions_table_rows(db):
    h = with_transactions(db, attribute_history(db, P1))
    assert h["transactions"] == [] and h["transactions_cancelled"] == 0
    assert years_text(h) == "실거래 0건: 같은 필지 0 · 번지대 0 · 연결 물건의 거래 0\n"


def test_cli(db, home, server, capsys):
    loaded(db, home, server)
    path = str(db.path)
    db.close()
    assert cli.main(["db", "--db", path, "parcels-history", P1]) == 0
    out = capsys.readouterr().out
    assert out.startswith("필지 가상동 1 (PNU 9999900100100010000") and "실거래 4건" in out and "  2021: 같은 필지 1" in out
    assert cli.main(["db", "--db", path, "parcels-history", P1, "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert len(j["transactions"]) == 4 and j["transactions_cancelled"] == 1 and j["years"][0]["year"] == "2026" and "price_series" in j


# tests/web/view.test.mjs 의 실제 마스킹 표기 표와 같다: 폰 jibunMatch 와 PC jibun_matches 가 같은 결과를 낸다
SHARED_CASES = [("1**", (0, 160, 0), "prefix"), ("1**", (0, 160, 3), None), ("1**", (0, 16, 0), None), ("1**-*", (0, 160, 3), "prefix"), ("16*-3", (0, 160, 3), "prefix"),
                ("16*-3", (0, 160, 4), None), ("8*", (0, 85, 0), "prefix"), ("8*", (0, 8, 0), None), ("1-*", (0, 1, 12), None), ("산1**", (1, 123, 0), "prefix"),
                ("산1**", (0, 123, 0), None), ("*-1", (0, 1, 1), None), ("1", (0, 1, 0), "exact"), ("1", (0, 1, 1), None), ("1-*", (0, 1, 0), "prefix"), ("*", (0, 1, 0), None)]


def test_masking_rule_matches_phone_table():
    for jibun, (mountain, bon, bu), want in SHARED_CASES:
        got = jibun_matches(parse_jibun(jibun), {"mountain": mountain, "bon": bon, "bu": bu})
        assert {"jibun_exact": "exact", "jibun_prefix": "prefix"}.get(got or "") == want, (jibun, mountain, bon, bu, got)
