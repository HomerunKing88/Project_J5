"""J5-072: PC `db parcels-comps` 가 폰 필지 카드의 주변 사례·연도별 시세(J5-064·066·071)와 같은 값을 낸다.

가상 파생본 생성기(tests/fixtures/make_view.py)의 정본에 거래를 더해(여러 해의 상업·업무용, 토지, 지분, 면적 없음, 공시지가와 견줄 수 있는 2026년 거래)
PC(parcel_comps)와 같은 정본에서 만든 파생본 파일로 폰(web/app/view.js 의 nearbyComps·compsByYear, Node 실행)이 낸 값을 필지·기간·종류마다 대조한다.
가상자료만 쓴다. 실제 API·기기 검증이 아니다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "fixtures"))

from j5 import cli  # noqa: E402
from j5.db.parcel_comps import comps_text, median, parcel_comps  # noqa: E402
from j5.db.store import DbError  # noqa: E402

ON = "2026-09-30"   # make_view.FIXED_NOW 의 날짜
P1 = "9999900100100010000"
YEARS = (1, 3, 5, 0)
KINDS = ("all", "nrg", "land")


def _extra(db, home):
    """대조할 갈래를 만든다: 2021년 상업·업무용 3건(지분 1건 더), 2026년 공시지가와 견줄 거래(지번 2·1), 면적 없는 거래, 기간 시작일의 거래, 2026년 토지 거래 2건."""
    import make_view
    from j5.collect.rt import collect_months
    from j5.db.transactions import load_run
    from tests.test_j5_014_collect import KEY, xml_page
    from tests.test_j5_065_rt_kinds import land_row

    def run(kind, by_month):
        def opener(url):
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            items = by_month.get(q["DEAL_YMD"], [])
            if items is None:   # 응답 오류 (실패한 달)
                return 500, b"boom"
            page, rows = int(q["pageNo"]), int(q["numOfRows"])
            return 200, xml_page(items[(page - 1) * rows:page * rows], len(items), page, rows)
        r = collect_months(home, key=KEY, key_source="fixture", lawd_cd="99999", months=sorted(by_month), endpoint="http://127.0.0.1:9/fake", opener=opener,
                           sleep=lambda s: None, kind=kind)
        load_run(db, home, "99999", r.run_id, kind=kind)

    R = make_view._row
    run("nrg", {
        "202104": [R(20, "202104", jibun="3", plottageAr="100.0"), R(21, "202104", jibun="2", plottageAr="80.0"), R(22, "202104", jibun="1-*", plottageAr="120.0"),
                   R(23, "202104", jibun="4-2", plottageAr="60.0", shareDealingType="지분")],
        "202603": [R(24, "202603", jibun="2", plottageAr="90.0"), R(25, "202603", jibun="3", plottageAr="")],
        "202605": [R(26, "202605", jibun="1", plottageAr="110.0")],
        "202509": [R(27, "202509", jibun="2", plottageAr="85.0", dealDay="30")],   # 최근 1년 기간이 시작하는 날(2025-09-30)의 거래: 기간 안
        "202301": None,   # 상업·업무용만 시도했다 실패한 해 (리뷰 반영 PR #119: 미수집과 가른다)
    })
    run("land", {"202402": None})   # 토지만 시도했다 실패한 해
    run("land", {"202602": [land_row(0, sggCd="99999", umdNm="가상동", jibun="4-2", dealArea="70.5", dealYear="2026", dealMonth="2"),
                            land_row(1, sggCd="99999", umdNm="가상동", jibun="3", dealArea="95.0", dealYear="2026", dealMonth="2")]})


NODE = """
import { readFileSync } from "node:fs";
import { nearbyComps, compsByYear, dealsOfKind, dealSince } from "./web/app/view.js";
const [file, today] = process.argv.slice(1);
const d = JSON.parse(readFileSync(file, "utf8"));
const feats = d.parcels.features, txs = d.tx.transactions;
const out = {};
for (const f of feats) for (const years of [1, 3, 5, 0]) for (const kind of ["all", "nrg", "land"]) {
  const list = dealsOfKind(txs, kind);
  const since = dealSince(years, today);
  const c = nearbyComps(f, list, feats, { since });
  const from = since ? Number(since.slice(0, 4)) : null;
  const y = compsByYear(nearbyComps(f, list, feats, { since: from ? `${from}-01-01` : null }), d.tx, f, { from, today, kind });
  out[`${f.id}|${years}|${kind}`] = { count: c.count, land: c.land, floor: c.floor, ratio: c.ratio, reference: c.reference,
    comps: c.comps.map((x) => ({ id: x.t.transaction_id, landUnit: x.landUnit, floorUnit: x.floorUnit, ratio: x.ratio, priceYear: x.priceYear, mine: x.mine })), byYear: y };
}
console.log(JSON.stringify(out));
"""

close = lambda a, b: (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= 1e-6 * max(1, abs(b)))   # noqa: E731


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    import make_view
    tmp = tmp_path_factory.mktemp("j5-072")

    def inspect(db, home, proj):
        pnus = [r["pnu"] for r in db.conn.execute("SELECT pnu FROM parcels ORDER BY pnu")]
        pc = {f"{p}|{y}|{k}": parcel_comps(db, p, years=y, kind=k, on_date=ON) for p in pnus for y in YEARS for k in KINDS}
        errors = {}
        for args in (("123", 5, "all", ON), (P1, 2, "all", ON), (P1, 5, "apt", ON), (P1, 5, "all", "2026-02-31")):
            try:
                parcel_comps(db, args[0], years=args[1], kind=args[2], on_date=args[3])
            except DbError as e:
                errors[args] = e.code
        return {"pc": pc, "errors": errors, "text": comps_text(pc[f"{P1}|0|all"])}

    b = make_view.build(tmp, inspect=inspect, extra=_extra)
    with zipfile.ZipFile(b["zip"]) as z:
        data = {"parcels": json.loads(z.read("parcels.geojson")), "tx": json.loads(z.read("transactions.json"))}
    return b, data, tmp


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_phone_comps_match_pc(built):
    b, data, tmp = built
    src = tmp / "phone-input.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(["node", "--input-type=module", "-e", NODE, str(src), ON], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    phone = json.loads(res.stdout)
    pc = b["inspect"]["pc"]
    assert set(phone) == set(pc)
    seen = {"comps": 0, "ratio": 0, "reference": 0, "mine": 0, "share": 0, "change": 0, "not_collected": 0, "incomplete": 0, "unknown": 0, "kinds": 0, "land_kind": 0, "years": 0}
    for key, ph in phone.items():
        r = pc[key]
        assert ph["count"] == r["count"], key
        for s in ("land", "floor"):
            assert ph[s]["n"] == r[s]["n"] and all(close(ph[s][k], r[s][k]) for k in ("median", "min", "max")), (key, s, ph[s], r[s])
        assert ph["ratio"]["n"] == r["ratio"]["n"] and close(ph["ratio"]["median"], r["ratio"]["median"]), key
        assert ("missing" in ph["reference"]) == ("missing" in r["reference"]), (key, ph["reference"], r["reference"])
        if "missing" not in r["reference"]:
            ref = r["reference"]
            assert close(ph["reference"]["unit"], ref["unit_krw_py"]) and ph["reference"]["priceYear"] == ref["price_year"] and ph["reference"]["n"] == ref["n"], key
            seen["reference"] += 1
        assert [c["id"] for c in ph["comps"]] == [c["transaction_id"] for c in r["comps"]], (key, "사례 순서")
        for a, c in zip(ph["comps"], r["comps"]):
            assert close(a["landUnit"], c["land_unit_krw_py"]) and close(a["floorUnit"], c["floor_unit_krw_py"]) and close(a["ratio"], c["ratio"]), (key, a, c)
            assert a["priceYear"] == c["price_year"] and a["mine"] == c["mine"], (key, a, c)
            seen["comps"] += 1
            seen["ratio"] += c["ratio"] is not None
            seen["mine"] += c["mine"]
            seen["share"] += c["share_deal"]
        py, ry = ph["byYear"], r["by_year"]
        assert (py is None) == (ry is None), key
        if ry is None:
            continue
        assert ("missing" in py["change"]) == ("missing" in ry["change"]), (key, py["change"], ry["change"])
        if "missing" not in ry["change"]:
            assert [py["change"]["from"]["year"], py["change"]["to"]["year"]] == [ry["change"]["from"], ry["change"]["to"]] and close(py["change"]["pct"], ry["change"]["pct"]), key
            seen["change"] += 1
        assert [y["year"] for y in py["years"]] == [y["year"] for y in ry["years"]], key
        for a, c in zip(py["years"], ry["years"]):
            for k in ("count", "n", "months", "kinds", "partial"):
                assert a[k] == c[k], (key, a["year"], k, a[k], c[k])
            # 폰은 종류를 고르면 그 종류의 시도 개월을 모른다(파일에 없음): 받은 달이 없고 그해 시도가 있으면 모름. PC 는 종류별로 가른다
            if a["counted"] == "unknown":
                assert not key.endswith("|all") and c["counted"] in ("incomplete", "not_collected"), (key, a, c)
                seen["unknown"] += 1
            else:
                assert a["counted"] == c["counted"], (key, a["year"], a["counted"], c["counted"])
            if key.endswith("|all"):
                assert a["monthsAny"] == c["months_any"], (key, a, c)
            assert all(close(a[k], c[k]) for k in ("median", "min", "max")), (key, a, c)
            seen["years"] += 1
            seen["not_collected"] += c["counted"] == "not_collected"
            seen["incomplete"] += a["counted"] == "incomplete"
            seen["kinds"] += bool(c["kinds"])
            seen["land_kind"] += key.endswith("|land") and c["months"] > 0
    # 빈 비교로 통과하지 않게: 갈래마다 한 번 이상
    for k, v in seen.items():
        assert v >= 1, (k, seen)


def test_pc_values_and_errors(built):
    b, _, _ = built
    pc, errors = b["inspect"]["pc"], b["inspect"]["errors"]
    assert errors == {("123", 5, "all", ON): "bad_pnu", (P1, 2, "all", ON): "bad_years", (P1, 5, "apt", ON): "bad_kind",
                      (P1, 5, "all", "2026-02-31"): "bad_date"}, "리뷰 반영(PR #119): 달력에 없는 기준일 거절"
    r = pc[f"{P1}|0|all"]
    # 지분 거래는 사례에는 들고 단가에서 빠진다. 면적 없는 거래도 단가가 없다
    share = [c for c in r["comps"] if c["share_deal"]]
    assert share and all(c["land_unit_krw_py"] is None and c["ratio"] is None for c in share)
    assert r["land"]["n"] == sum(c["land_unit_krw_py"] is not None for c in r["comps"]) < r["count"]
    assert r["land"]["median"] == median([c["land_unit_krw_py"] for c in r["comps"] if c["land_unit_krw_py"] is not None])
    # 배율은 그 거래 연도 이하 공시지가로만 (2026년 1월 기준 공시지가뿐이라 2021·2025년 거래는 배율이 없다)
    assert all(c["price_year"] == 2026 for c in r["comps"] if c["ratio"] is not None)
    assert not any(c["ratio"] is not None for c in r["comps"] if c["deal_date"] < "2026")
    # 취소 확정 거래(2026-08 지번 1)는 사례에 없다
    assert not any(c["deal_date"].startswith("2026-08") and c["jibun"] == "1" for c in r["comps"])
    by = {y["year"]: y for y in r["by_year"]["years"]}
    assert by[2022]["counted"] == "not_collected" and by[2022]["months"] == 0, "받지 않은 해는 0 건이 아니다"
    assert by[2023]["counted"] == "incomplete" and by[2023]["months_any"] == 1, "리뷰 반영(PR #119): 시도했다 실패한 해는 미수집과 가른다"
    nrg = {y["year"]: y for y in pc[f"{P1}|0|nrg"]["by_year"]["years"]}
    land_y = {y["year"]: y for y in pc[f"{P1}|3|land"]["by_year"]["years"]}   # 최근 3년은 2023년부터 (전체 기간은 첫 토지 거래의 해 2026년부터)
    assert (nrg[2023]["counted"], nrg[2024]["counted"], land_y[2024]["counted"]) == ("incomplete", "not_collected", "incomplete"), "종류마다 시도 개월을 따로 본다"
    assert by[2026]["partial"] and by[2026]["kinds"], "2026년은 종류마다 받은 개월이 다르다"
    land = pc[f"{P1}|0|land"]
    assert {c["deal_kind"] for c in land["comps"]} == {"land"} and all(c["floor_unit_krw_py"] is None for c in land["comps"])
    text = b["inspect"]["text"]
    assert "주변 사례: 가상동, 전체 기간" in text and "연도별 시세" in text and "감정평가가 아니다" in text
    assert "2022: 거래 미수집 (0 건이 아니다)" in text and "2023: 거래 수집 실패·부분만 (1개월 시도) (0 건이 아니다)" in text
    one = pc[f"{P1}|1|all"]
    assert any(c["deal_date"] == "2025-09-30" for c in one["comps"]), "기간이 시작하는 날의 거래는 기간 안 (폰 dealsSince 와 같다)"


def test_cli_parcels_comps(built, tmp_path, monkeypatch, capsys):
    """CLI: 정본을 만들고 명령으로 읽는다 (make_view 정본은 닫혔으므로 작은 정본을 새로 쓴다). 없는 필지는 오류."""
    from j5.db.store import Db
    from j5.db.parcels import load_bundle
    import make_view
    home = tmp_path / "home"
    home.mkdir()
    db = Db.create(home / "db" / "j5.sqlite3", study_id="t", data_mode="synthetic")
    load_bundle(db, json.loads(make_view.PARCELS_VW.read_text(encoding="utf-8")))
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "parcels-comps", P1, "--years", "0", "--on", ON, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 0 and out["by_year"] is None and out["reference"]["missing"].startswith("공시지가와 견줄 수 있는 사례가 0건")
    assert cli.main(["db", "parcels-comps", P1, "--on", ON]) == 0
    assert "거래 0건" in capsys.readouterr().out
    assert cli.main(["db", "parcels-comps", "9999900100100099999", "--on", ON]) != 0


def test_trend_change_zero_baseline():
    """리뷰 반영(PR #119): 시작 해 중간값이 0(금액 0 신고)이면 나누지 않고 사유를 적는다."""
    from j5.db.parcel_comps import trend_change
    rows = [{"year": 2021, "n": 3, "median": 0.0}, {"year": 2022, "n": 1, "median": 5.0}, {"year": 2023, "n": 4, "median": 10.0}]
    assert trend_change(rows) == {"missing": "시작 해(2021년)의 대지 평당 중간값이 0 이하라 변동을 적지 않는다"}
    rows[0]["median"] = 8.0
    assert trend_change(rows) == {"from": 2021, "to": 2023, "pct": 25.0}
    assert "missing" in trend_change(rows[:1])
