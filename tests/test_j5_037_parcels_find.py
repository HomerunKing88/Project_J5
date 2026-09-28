"""J5-037: PC 조건으로 필지 찾기 (db parcels-find). 폰 조건 찾기(tests/web/parcels.test.mjs 의 J5-033·036 시험)와 같은 가상 번들·같은 조건에서 같은 결과를 낸다.

시험: 용도지역 분류 규칙, 조건별 결과와 공부면적 큰 순, 결측을 켠 조건마다 셈·판단 불가 수, 물건 조건(정본 연결·위치점 포함, 관찰목록),
잘못된 조건 거절, 정본 무변화, CLI(만원/㎡ 환산, CSV 덮어쓰지 않음, 종료 코드). 가상자료만 쓴다.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from j5 import cli
from j5.db.parcel_find import check_criteria, find_csv, find_parcels, find_text, zone_category
from j5.db.parcels import apply_links, load_bundle
from j5.db.store import DbError
from j5.db.tracking import set_status
from tests.test_j5_026_attr_history import P1, db, home, vw_bundle  # noqa: F401 (fixture)

A = [f"7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a5{i}" for i in range(5)]


def labels(r: dict) -> list[str]:
    return [row["parcel"].split(" ", 1)[1] for row in r["rows"]]


def test_zone_category_matches_phone_rules():
    assert [zone_category(n) for n in ("제1종전용주거지역", "제2종 일반주거지역", "준주거지역", "일반상업지역", "준공업지역", "자연녹지지역", "보전관리지역", "개발제한구역", "", None)] == \
        ["res1", "res2", "res3", "com", "ind", "green", "rural", "other", None, None]


def test_same_results_as_phone_filter(db):
    load_bundle(db, vw_bundle())
    run = lambda **c: find_parcels(db, c, on_date="2026-09-28")
    assert labels(run(zone="com")) == ["1"]
    big = run(area_min=1100)
    assert labels(big) == ["산1-2", "1", "2", "3"] and big["with_attrs"] == 6, "공부면적 큰 순"
    price = run(price_min=9_000_000)
    assert labels(price) == ["1", "1-1"] and price["unknown"]["price"] == 1
    own = run(owner="개인")
    assert labels(own) == ["1", "2"] and own["unknown"]["owner"] == 2
    assert labels(run(restricted=True)) == ["2", "3", "4-2"]
    assert labels(run(zone="res2", restricted=True)) == ["2"]
    assert labels(run(area_min=1000, area_max=1100, price_max=5_000_000)) == ["4-2"]
    ind = run(zone="ind", price_min=10_000)
    assert ind["total"] == 0 and ind["unknown"]["price"] == 1 and ind["undetermined"] == 1
    multi = run(zone="com", owner="개인", price_min=10_000)
    assert labels(multi) == ["1"] and multi["unknown"] == {"zone": 0, "area": 0, "price": 1, "owner": 2, "plan": 0} and multi["undetermined"] == 0, \
        "결측은 앞 조건에서 빠진 필지도 센다 (폰 리뷰 반영 PR #80 과 같다)"
    row = run(zone="com")["rows"][0]
    assert row["registered_area_m2"] == 1770.5 and row["official_land_price_krw_m2"] == 12_340_000 and row["ownership_kind"] == "개인" and row["attrs_as_of"]
    assert run(restricted=True)["rows"][0]["restricted_codes"], "저촉 코드가 함께 나온다"


def test_asset_criteria_linked_and_inside(db):
    load_bundle(db, vw_bundle())
    set_status(db, A[0], "watch", reason=None)
    set_status(db, A[3], "hold", reason="가상 보류")
    run = lambda **c: find_parcels(db, c, on_date="2026-09-28")
    # 연결 전: 위치점 포함만으로 센다 (폰 필지 패널과 같은 기준)
    assert labels(run(assets="none")) == ["산1-2", "4-2"]
    assert labels(run(assets="any")) == ["1", "2", "3", "1-1"]
    assert labels(run(assets="watch")) == ["1"], "보류는 관찰목록이 아니다"
    r = run(assets="watch")["rows"][0]
    assert r["watchlist_assets"] == 1 and r["assets"][0]["basis"] == "inside"
    # 정본 연결: 위치점이 없는 물건 3 을 필지 4-2 에 연결하면 4-2 는 물건 있는 필지가 된다
    apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[2], "pnu": "9999900100100040002", "effective_from": "2026-01-01", "effective_to": None, "basis": "manual"},
                                                         {"asset_id": A[0], "pnu": P1, "effective_from": "2026-01-01", "effective_to": None, "basis": "manual"}]})
    assert labels(run(assets="none")) == ["산1-2"]
    assert run(assets="watch")["rows"][0]["assets"][0]["basis"] == "both"
    assert labels(run(assets="none", restricted=True)) == [], "다른 조건과 모두 만족"
    # 끝난 연결은 기준일에 따라 빠진다
    assert labels(find_parcels(db, {"assets": "none"}, on_date="2025-12-31")) == ["산1-2", "4-2"]


def test_bad_criteria_and_no_write(db):
    load_bundle(db, vw_bundle())
    for c, part in (({"area_max": float("inf")}, "공부면적 최대"), ({"price_min": float("nan")}, "공시지가 최소"), ({}, "하나 이상"), ({"zone": "x"}, "용도지역"), ({"assets": "x"}, "물건 조건"), ({"area_min": -1}, "공부면적 최소"),
                    ({"area_min": 5, "area_max": 1}, "최소가 최대보다"), ({"price_min": 2, "price_max": 1}, "공시지가 최소가")):
        assert any(part in e for e in check_criteria(c)), c
        with pytest.raises(DbError):
            find_parcels(db, c)
    v = db.meta("dataset_version")
    find_parcels(db, {"zone": "com"})
    assert db.meta("dataset_version") == v, "정본에 쓰지 않는다"


def test_text_and_csv(db):
    load_bundle(db, vw_bundle())
    r = find_parcels(db, {"area_min": 0}, on_date="2026-09-28")
    t = find_text(r, limit=2)
    assert "속성 있는 필지 6개 (정본 필지 6개) 중 6개 일치" in t and "앞 2개만" in t and "매입 가능성·규제 판단이 아니다" in t
    rows = list(csv.DictReader(io.StringIO(find_csv(r))))
    assert len(rows) == 6 and rows[0]["pnu"] and rows[0]["registered_area_m2"] == "2500.0"
    p3 = next(x for x in rows if x["parcel"].endswith(" 3"))
    assert p3["official_land_price_krw_m2"] == "" and p3["ownership_kind"] == "", "결측은 빈 칸 (0 으로 채우지 않는다)"


def test_cli(db, home, tmp_path, capsys, monkeypatch):
    load_bundle(db, vw_bundle())
    path = str(db.path)
    db.close()
    rc = cli.main(["db", "--db", path, "parcels-find", "--price-min", "900"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert "2개 일치" in out.out and "공시지가 1" in out.out, "만원/㎡ 를 원/㎡ 로 바꾼다"
    out_csv = tmp_path / "found.csv"
    assert cli.main(["db", "--db", path, "parcels-find", "--assets", "none", "--csv", str(out_csv)]) == 0
    assert f"CSV: {out_csv} (2행)" in capsys.readouterr().out and len(out_csv.read_text(encoding="utf-8").splitlines()) == 3
    assert cli.main(["db", "--db", path, "parcels-find", "--assets", "none", "--csv", str(out_csv)]) == cli.USAGE_ERROR, "CSV 를 덮어쓰지 않는다"
    assert cli.main(["db", "--db", path, "parcels-find", "--zone", "com", "--csv", str(tmp_path / "none" / "x.csv")]) == cli.USAGE_ERROR
    assert "출력 폴더가 없다" in capsys.readouterr().err
    assert cli.main(["db", "--db", path, "parcels-find"]) == cli.USAGE_ERROR
    assert "하나 이상" in capsys.readouterr().err
    assert cli.main(["db", "--db", path, "parcels-find", "--zone", "nope"]) == cli.USAGE_ERROR
    # 유한하지 않은 숫자·환산에서 넘치는 값은 예외가 아니라 사용 오류 (리뷰 반영 PR #84)
    for flag, v in (("--price-min", "nan"), ("--price-max", "inf"), ("--price-min", "1e305"), ("--area-max", "inf"), ("--area-min", "-inf")):
        assert cli.main(["db", "--db", path, "parcels-find", f"{flag}={v}"]) == cli.USAGE_ERROR, (flag, v)
        assert "유한한 숫자" in capsys.readouterr().err
    assert cli.main(["db", "--db", path, "parcels-find", "--zone", "com", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["total"] == 1 and doc["criteria"] == {"zone": "com"}
