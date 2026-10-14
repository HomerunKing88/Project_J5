"""J5-074: 여러 필지의 주변 사례 요약표 (`db parcels-comps --pnu/--csv/--watchlist [--out]`).

필지마다 한 줄이며 값은 그 필지의 `parcel_comps`(J5-072, 폰과 같은 규칙)와 같다. 같은 동의 거래·공시지가 추이를 한 번만 읽는 캐시가
값을 바꾸지 않는지, 이 필지 거래 열, CSV(덮어쓰지 않음)·글, 입력 오류와 CLI 를 본다. 가상자료만 쓴다.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "fixtures"))

from j5 import cli  # noqa: E402
from j5.db.parcel_comps import TABLE_FIELDS, comps_table, parcel_comps, table_csv, table_text  # noqa: E402
from j5.db.store import DbError  # noqa: E402
from tests.test_j5_072_parcel_comps import ON, P1, _extra  # noqa: E402


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    import make_view

    def inspect(db, home, proj):
        pnus = [r["pnu"] for r in db.conn.execute("SELECT pnu FROM parcels ORDER BY pnu")]
        out = {"pnus": pnus, "tables": {}, "single": {}, "errors": {}}
        for years, kind in ((0, "all"), (5, "all"), (1, "land")):
            out["tables"][(years, kind)] = comps_table(db, list(reversed(pnus)) + [pnus[0]], years=years, kind=kind, on_date=ON)
            out["single"][(years, kind)] = {p: parcel_comps(db, p, years=years, kind=kind, on_date=ON) for p in pnus}
        for name, args in (("empty", []), ("bad", ["12"]), ("missing", ["9999900100100099999"]), ("many", [f"{i:019d}" for i in range(501)])):
            try:
                comps_table(db, args, on_date=ON)
            except DbError as e:
                out["errors"][name] = e.code
        return out

    return make_view.build(tmp_path_factory.mktemp("j5-074"), inspect=inspect, extra=_extra)["inspect"]


def test_rows_match_single_parcel(result):
    for (years, kind), t in result["tables"].items():
        assert t["parcels"] == len(result["pnus"]) and t["on_date"] == ON and t["years"] == years and t["kind"] == kind
        assert [r["pnu"] for r in t["rows"]] == list(reversed(result["pnus"])), "입력 순서, 같은 PNU 는 한 번"
        for row in t["rows"]:
            one = result["single"][(years, kind)][row["pnu"]]
            assert set(row) == set(TABLE_FIELDS)
            assert (row["count"], row["land_n"], row["land_median_krw_py"], row["floor_n"], row["ratio_n"], row["ratio_median"]) == \
                   (one["count"], one["land"]["n"], one["land"]["median"], one["floor"]["n"], one["ratio"]["n"], one["ratio"]["median"]), "캐시를 써도 값이 같다"
            assert row["reference_unit_krw_py"] == one["reference"].get("unit_krw_py") and row["reference_missing"] == one["reference"].get("missing")
            ch = (one["by_year"] or {}).get("change") or {}
            assert (row["trend_from"], row["trend_to"], row["trend_pct"]) == (ch.get("from"), ch.get("to"), ch.get("pct"))
            own = [c for c in one["comps"] if c["mine"]]
            assert row["own_count"] == len(own) and row["own_last_date"] == (own[0]["deal_date"] if own else None)
    p1 = next(r for r in result["tables"][(0, "all")]["rows"] if r["pnu"] == P1)
    assert p1["own_count"] == 2 and p1["own_last_date"] == "2026-05-27", "이 필지에 지번이 닿는 거래 (취소 확정 제외), 가장 최근"
    assert p1["reference_unit_krw_py"] is not None and p1["trend_pct"] is not None
    land = next(r for r in result["tables"][(1, "land")]["rows"] if r["pnu"] == P1)
    assert land["reference_missing"].startswith("공시지가와 견줄 수 있는 사례가 1건") and land["trend_missing"]


def test_csv_and_text(result):
    t = result["tables"][(0, "all")]
    rows = list(csv.DictReader(io.StringIO(table_csv(t))))
    assert tuple(rows[0].keys()) == TABLE_FIELDS and len(rows) == len(t["rows"])
    r1 = next(r for r in rows if r["pnu"] == P1)
    assert r1["own_count"] == "2" and r1["trend_missing"] == "" and float(r1["land_median_krw_py"]) > 0
    text = table_text(t)
    assert text.startswith(f"필지 {len(t['rows'])}개 주변 사례 요약 (전체 기간, 기준일 {ON})") and "감정평가가 아니다" in text
    assert "이 필지 거래 2건 (최근 2026-05-27" in text


def test_errors(result):
    assert result["errors"] == {"empty": "no_pnu", "bad": "bad_pnu", "missing": "parcel_missing", "many": "too_many"}


def test_cli_table(tmp_path, monkeypatch, capsys):
    from j5.db.parcels import load_bundle
    from j5.db.store import Db
    import make_view
    home = tmp_path / "home"
    home.mkdir()
    db = Db.create(home / "db" / "j5.sqlite3", study_id="t", data_mode="synthetic")
    load_bundle(db, json.loads(make_view.PARCELS_VW.read_text(encoding="utf-8")))
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    out = tmp_path / "comps.csv"
    p2 = "9999900100100020000"
    assert cli.main(["db", "parcels-comps", "--pnu", f"{P1},{p2}", "--on", ON, "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("필지 2개 주변 사례 요약") and f"CSV: {out} (2행)" in printed
    assert [r["pnu"] for r in csv.DictReader(out.open(encoding="utf-8"))] == [P1, p2]
    assert cli.main(["db", "parcels-comps", "--pnu", P1, "--on", ON, "--out", str(out)]) != 0, "덮어쓰지 않는다"
    assert "덮어쓰지 않음" in capsys.readouterr().err
    assert cli.main(["db", "parcels-comps", P1, "--pnu", p2, "--on", ON, "--json"]) == 0
    assert [r["pnu"] for r in json.loads(capsys.readouterr().out)["rows"]] == [P1, p2], "위치 인자와 --pnu 를 함께 주면 요약표"
    assert cli.main(["db", "parcels-comps", "--watchlist", "--on", ON]) != 0, "관찰목록이 비면 대상 없음"
    assert cli.main(["db", "parcels-comps", "--on", ON]) != 0, "대상이 없다"
    assert "PNU 를 주거나" in capsys.readouterr().err
    assert cli.main(["db", "parcels-comps", P1, "--on", ON]) == 0 and "주변 사례: 가상동" in capsys.readouterr().out, "하나만 주면 자세히 (J5-072 그대로)"
