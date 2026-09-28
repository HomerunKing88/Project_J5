"""J5-046: 여러 필지의 거래를 한 번에 모으기 (parcels_transactions). 필지마다 따로 모으던 규칙과 결과가 같아야 한다.

시험: 가상 필지 6개 × 여러 지번 표기(온전·부번 마스킹·동 전체 마스킹·산·다른 법정동)·취소 확정·다른 시군구·수동 확정 연결을 넣고,
한 번에 모은 결과가 필지마다 거래 전체를 하나씩 대조하는 기준 구현(J5-043·044 규칙을 그대로 옮김)과 목록·순서·수까지 같다.
정본에 없는 PNU 는 빈 결과. 가상 서버·가상자료만 쓴다.
"""

from __future__ import annotations

from j5.db.parcel_timeline import parcel_transactions, parcels_transactions
from j5.db.parcels import active_links
from j5.db.txlinks import apply_decisions, jibun_matches, parse_jibun
from tests.test_j5_014_collect import server  # noqa: F401 (fixture)
from tests.test_j5_014b2_txlinks import A, db, home, load_month, row, sequential_run_ids, tid_of  # noqa: F401 (fixture)
from tests.test_j5_044_sgg_match import load_other, other_row

TODAY = "2026-09-28"


def reference(db, pnu: str) -> dict:
    """필지 하나씩 정본 거래 전체를 대조하는 기준 구현 (J5-043·044 의 규칙)."""
    p = db.conn.execute("SELECT pnu, emd_code, emd_name, mountain, bon, bu FROM parcels WHERE pnu = ?", (pnu,)).fetchone()
    here = {l["asset_id"] for l in active_links(db, TODAY) if l["pnu"] == pnu}
    out = {"ids": [], "cancelled": 0, "other_sgg": 0}
    rows = db.conn.execute("SELECT t.*, l.asset_id AS linked_asset_id, l.status AS link_state FROM transactions t"
                           " LEFT JOIN transaction_links l ON l.transaction_id = t.transaction_id AND l.status <> 'withdrawn'").fetchall()
    for t in sorted(rows, key=lambda t: (t["deal_ymd"], t["deal_date"] or "", t["jibun_raw"] or "", t["ordinal"])):
        match, other = None, False
        if t["emd_name"] == p["emd_name"]:
            m = jibun_matches(parse_jibun(t["jibun_raw"]), p)
            if m:
                match = {"jibun_exact": "exact", "jibun_prefix": "prefix"}[m]
                if t["lawd_cd"] != p["emd_code"][:5]:
                    match, other = None, True
        if match is None and t["link_state"] == "confirmed" and t["linked_asset_id"] in here:
            match = "linked"
        if match is None:
            out["other_sgg"] += int(other and t["cancel_status"] != "cancelled")
        elif t["cancel_status"] == "cancelled":
            out["cancelled"] += 1
        else:
            out["ids"].append((t["transaction_id"], match))
    return out


def test_batch_equals_reference(db, home, server):
    load_month(db, home, server, [row(0, jibun="1"), row(1, jibun="1-*"), row(2, jibun="1-1"), row(3, jibun="산1-2"), row(4, jibun="*"), row(5, jibun="9"),
                                  row(6, jibun="1", cdealType="O", cdealDay="26.08.30"), row(7, jibun="4-2", umdNm="다른동"), row(8, jibun="4-*"), row(9, jibun="산1-*"),
                                  row(10, jibun="2"), row(11, jibun="3-0"), row(12, jibun="1*-*")])
    load_other(db, home, server, [other_row(20, jibun="1"), other_row(21, jibun="2"), other_row(22, jibun="1-*", cdealType="O", cdealDay="26.08.30")], ym="202607")
    t9 = tid_of(db, row(5, jibun="9"))
    to = tid_of(db, other_row(21, jibun="2"))
    apply_decisions(db, [{"_line": 2, "transaction_id": t9, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual", "reviewed_on": TODAY},
                         {"_line": 3, "transaction_id": to, "decision": "confirmed", "asset_id": A[3], "basis_kind": "document", "reviewed_on": TODAY}])
    pnus = [r[0] for r in db.conn.execute("SELECT pnu FROM parcels ORDER BY pnu")]
    batch = parcels_transactions(db, pnus + ["9999900100199990000"], on_date=TODAY)
    total = 0
    for pnu in pnus:
        ref = reference(db, pnu)
        got = batch[pnu]
        assert [(t["transaction_id"], t["match"]) for t in got["transactions"]] == ref["ids"], pnu
        assert (got["cancelled"], got["other_sgg"]) == (ref["cancelled"], ref["other_sgg"]), pnu
        assert got == parcel_transactions(db, pnu, on_date=TODAY), "필지 하나와 여러 필지가 같은 결과"
        total += len(ref["ids"])
    assert total >= 8 and any(t["match"] == "linked" and t["sgg_differs"] for t in batch["9999900100100020000"]["transactions"]), "다른 시군구 확정 연결 포함"
    assert batch["9999900100199990000"] == {"transactions": [], "cancelled": 0, "other_sgg": 0}, "정본에 없는 PNU 는 빈 결과"
