"""J5-045: 여러 필지의 연도별 표 (db parcels-years).

시험: 필지 × 연도 한 줄, 그 연도 기준의 공시지가와 전년 대비(두 해 값이 있을 때만), 자료 없는 연도는 null + no_snapshot,
거래 수는 필지 이력과 같은 규칙(같은 필지·번지대·연결)으로 계약연도별, 수집 실행이 없는 연도는 null + not_collected, 실패한 수집만 있으면 collection_incomplete(0 건과 구분),
수집했지만 거래가 없는 연도는 0, 수집 개월 수, 소유 변동일은 그 연도에만, 기본 범위(자료가 있는 첫 해~올해 또는 자료의 마지막 해),
관찰목록 필지, 입력 거절(형식·없는 필지·연도 범위), CSV(빈 칸, 덮어쓰지 않음)·글·JSON, 정본 무변화. 가상 서버·가상자료만 쓴다.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from j5 import cli
from j5.db.parcel_years import CSV_FIELDS, parcel_years, watchlist_pnus, years_csv, years_text
from j5.db.parcels import load_bundle
from j5.db.store import DbError
from j5.db.tracking import set_status
from j5.collect.rt import collect_months
from j5.db.transactions import load_run
from tests.test_j5_014_collect import KEY, _Handler, server  # noqa: F401 (fixture)
from tests.test_j5_014b2_txlinks import A, db, home, load_month, row, sequential_run_ids  # noqa: F401 (fixture)
from tests.test_j5_026_attr_history import later_bundle, vw_bundle

P1, P11 = "9999900100100010000", "9999900100100010001"
TODAY = "2026-09-28"


def loaded(db, home, server):
    load_bundle(db, vw_bundle())                                    # 2026년 기준 공시지가 (필지 1: 12,340,000), 소유 변동일 2017-01-01
    load_bundle(db, later_bundle("2027-06-01", price=13_000_000))   # 2027년 기준 (필지 1 만 바뀜)
    load_month(db, home, server, [row(0, jibun="1"), row(1, jibun="1-*"), row(2, jibun="1-1")])   # 2026-08
    load_month(db, home, server, [], ym="202109")                   # 2021-09: 수집했고 거래 0


def by_year(r, pnu):
    return {x["year"]: x for x in r["rows"] if x["pnu"] == pnu}


def test_rows_per_parcel_year(db, home, server):
    loaded(db, home, server)
    v = db.status()["dataset_version"]
    r = parcel_years(db, [P1, P11, P1], on_date=TODAY)
    assert (r["parcels"], r["year_from"], r["year_to"]) == (2, 2021, 2027), "기본 범위: 자료가 있는 첫 해(2021 수집) ~ 자료의 마지막 해(2027 공시지가)"
    assert len(r["rows"]) == 2 * 7
    y = by_year(r, P1)
    assert (y[2026]["price_krw_m2"], y[2026]["price_delta_pct"], y[2027]["price_krw_m2"], y[2027]["price_delta_pct"]) == (12_340_000, None, 13_000_000, 5.35)
    assert y[2025]["price_krw_m2"] is None and y[2025]["price_missing_reason"] == "no_snapshot"
    assert (y[2026]["tx_exact"], y[2026]["tx_prefix"], y[2026]["tx_linked"], y[2026]["rt_months_complete"]) == (1, 1, 0, 1)
    assert y[2026]["tx_exact_last_date"] == "2026-08-01" and y[2026]["tx_exact_last_amount_krw"] == 1_000_000_000
    assert (y[2021]["tx_exact"], y[2021]["tx_missing_reason"], y[2021]["rt_months_complete"]) == (0, None, 1), "수집했지만 거래가 없으면 0"
    assert (y[2024]["tx_exact"], y[2024]["tx_missing_reason"], y[2024]["rt_months_any"]) == (None, "not_collected", 0), "수집하지 않은 연도는 0 이 아니다"
    assert y[2027]["tx_exact"] is None and y[2027]["tx_missing_reason"] == "not_collected"
    assert all(x["ownership_snapshot"] and x["ownership_changed_on"] == [] for x in y.values()), "2017 변동일은 기본 범위 밖"
    y11 = by_year(r, P11)
    assert y11[2026]["tx_exact"] == 1 and y11[2026]["tx_prefix"] == 1 and y11[2027]["price_krw_m2"] is None, "필지 1-1 은 2027 기준 자료가 없다"
    r2 = parcel_years(db, [P1], year_from=2017, year_to=2018, on_date=TODAY)
    assert [x["ownership_changed_on"] for x in r2["rows"]] == [["2017-01-01"], []]
    assert db.status()["dataset_version"] == v, "정본에 쓰지 않는다"


def test_text_csv_and_rejections(db, home, server):
    loaded(db, home, server)
    r = parcel_years(db, [P1], on_date=TODAY)
    t = years_text(r)
    assert t.index("  2027:") < t.index("  2026:") < t.index("  2021:"), "최근 먼저"
    assert "  2027: 공시지가 13,000,000원/㎡ (+5.3%) · 거래 미수집" in t
    assert "  2026: 공시지가 12,340,000원/㎡ · 거래 수집 1/12개월 · 같은 필지 1 (10억, 2026-08-01) · 번지대 1 · 연결 0" in t
    assert "  2024: 공시지가 자료 없음 · 거래 미수집" in t and "0 건이 아니다" in t
    # 실패한 수집만 있는 연도는 0 건이 아니라 수집 실패로 둔다
    _Handler.scenarios = {"202208": {"kind": "http_error"}}
    run = collect_months(home, key=KEY, key_source="test", lawd_cd="99999", months=["202208"], endpoint=server, sleep=lambda s: None)
    load_run(db, home, "99999", run.run_id)
    y22 = by_year(parcel_years(db, [P1], on_date=TODAY), P1)[2022]
    assert (y22["tx_exact"], y22["tx_missing_reason"], y22["rt_months_any"], y22["rt_months_complete"]) == (None, "collection_incomplete", 1, 0)
    assert "  2022: 공시지가 자료 없음 · 거래 수집 실패·부분만 (1개월 시도)" in years_text(parcel_years(db, [P1], on_date=TODAY))
    rows = list(csv.DictReader(io.StringIO(years_csv(r))))
    assert list(rows[0]) == list(CSV_FIELDS) and len(rows) == 7
    r24 = next(x for x in rows if x["year"] == "2024")
    assert (r24["price_krw_m2"], r24["tx_exact"], r24["tx_missing_reason"], r24["ownership_snapshot"]) == ("", "", "not_collected", "1"), "모름은 빈 칸과 사유"
    for pnus, kw, code in (([], {}, "no_pnu"), (["123"], {}, "bad_pnu"), (["9999900100199990000"], {}, "parcel_missing"),
                           ([P1], {"year_from": 2027, "year_to": 2026}, "bad_years"), ([P1], {"year_from": 1980, "year_to": 2026}, "too_many_years")):
        with pytest.raises(DbError) as e:
            parcel_years(db, pnus, on_date=TODAY, **kw)
        assert e.value.code == code


def test_watchlist_and_cli(db, home, server, tmp_path, capsys):
    loaded(db, home, server)
    assert watchlist_pnus(db, TODAY) == []
    set_status(db, A[0], "watch", changed_on=TODAY)
    assert watchlist_pnus(db, TODAY) == [P1], "관찰목록 물건 1 이 필지 1 에 연결돼 있다"
    path = str(db.path)
    db.close()
    out = tmp_path / "years.csv"
    assert cli.main(["db", "--db", path, "parcels-years", "--watchlist", "--pnu", P11, "--from", "2025", "--to", "2027", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "필지 2개 · 2025~2027년" in text and f"CSV: {out} (6행)" in text
    assert [(x["pnu"], x["year"]) for x in csv.DictReader(out.open(encoding="utf-8"))][::3] == [(P11, "2025"), (P1, "2025")], "입력 순서(--pnu, --csv, --watchlist), 필지 안은 연도 순"
    assert cli.main(["db", "--db", path, "parcels-years", "--pnu", P1, "--out", str(out)]) == cli.USAGE_ERROR, "덮어쓰지 않는다"
    assert cli.main(["db", "--db", path, "parcels-years", "--pnu", P1, "--out", str(tmp_path / "없음" / "a.csv")]) == cli.USAGE_ERROR
    assert cli.main(["db", "--db", path, "parcels-years", "--pnu", P1, "--json", "--from", "2026", "--to", "2026"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["rows"][0]["tx_exact"] == 1 and j["year_from"] == j["year_to"] == 2026
    assert cli.main(["db", "--db", path, "parcels-years"]) == 1 and "no_pnu" in capsys.readouterr().err


def test_review_fixes_latest_deal_and_text_order(db, home, server, monkeypatch):
    """리뷰 반영 PR #92: 마지막 거래는 계약월 → 계약일 순으로 고른다(형식을 섞어 비교하지 않음). 글 출력은 입력 순서를 지킨다."""
    loaded(db, home, server)
    from j5.db import parcel_years as PY
    real = PY.parcels_transactions

    def fake(db_, pnus, on_date=None):
        r = real(db_, pnus, on_date=on_date)
        base = dict(r[P1]["transactions"][0])
        r[P1]["transactions"] = [dict(base, transaction_id="t-sep", deal_ymd="202609", deal_date=None, amount_krw=1),
                                 dict(base, transaction_id="t-oct", deal_ymd="202610", deal_date="2026-10-01", amount_krw=2)]
        return r
    monkeypatch.setattr(PY, "parcels_transactions", fake)
    y = by_year(parcel_years(db, [P1], on_date=TODAY), P1)[2026]
    assert (y["tx_exact_last_date"], y["tx_exact_last_amount_krw"]) == ("2026-10-01", 2), "10월 거래가 9월(계약일 없음)보다 나중이다"
    monkeypatch.setattr(PY, "parcels_transactions", real)
    t = years_text(parcel_years(db, [P11, P1], on_date=TODAY))
    assert t.index(f"(PNU {P11})") < t.index(f"(PNU {P1})"), "입력 순서 (CSV·JSON 과 같다)"
