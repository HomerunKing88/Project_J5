"""J5-030 공시지가 추이 시험.

- PC: `attribute_history` 의 price_series(스냅샷에서 값이 바뀐 지점, 증감률, 같은 기준연월 표시)와 `parcels-history` 글.
- 폰과 같은 규칙: 파생본 parcels.geojson 의 attrs·attrs_history 를 web/app/parcels.js 의 priceTrend 로 되살린 결과가 PC 의 price_series 와 같다(Node 로 실행).
가상자료만 쓴다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from j5 import cli
from j5.db.parcels import attribute_history, history_text, load_bundle, price_series
from j5.db.projection import build_projection
from tests.test_j5_026_attr_history import P1, P11, db, home, later_bundle, vw_bundle  # noqa: F401 (fixture)

REPO = Path(__file__).resolve().parent.parent


def _load_series(db):
    load_bundle(db, vw_bundle())                                   # 2026-09-05: 12,340,000 (2026년 1월)
    load_bundle(db, later_bundle("2027-06-01", price=13_000_000))  # 2027년 1월
    load_bundle(db, later_bundle("2027-09-01", price=13_000_000, owner="법인"))  # 소유만 바뀜: 공시지가 점 없음
    load_bundle(db, later_bundle("2028-06-01", price=12_350_000))  # 2028년 1월 하락


def test_price_series_from_snapshots(db):
    _load_series(db)
    h = attribute_history(db, P1)
    assert [(p["as_of"], p["price_krw_m2"], p["base_year"], p["base_month"], p["delta_pct"], p["same_base"]) for p in h["price_series"]] == [
        ("2026-09-05", 12_340_000, 2026, 1, None, False), ("2027-06-01", 13_000_000, 2027, 1, 5.35, False), ("2028-06-01", 12_350_000, 2028, 1, -5.0, False)]
    txt = history_text(h)
    assert "공시지가 추이 (원/㎡, 값이 바뀐 지점):" in txt
    assert "  2027년 1월 기준 13,000,000 (+5.3%) · 확인 2027-06-01" in txt and "  2028년 1월 기준 12,350,000 (-5.0%) · 확인 2028-06-01" in txt
    # 가격이 바뀌지 않은 필지는 점 하나라 추이 글이 없다
    h11 = attribute_history(db, P11)
    assert len(h11["price_series"]) <= 1 and "공시지가 추이" not in history_text(h11)


def test_price_series_rules():
    snap = lambda as_of, price, y=2026, m=1, kind="land_feature": {"kind": kind, "as_of": as_of, "values": {"official_land_price_krw_m2": price, "price_base_year": y, "price_base_month": m}}
    s = price_series([snap("2026-09-25", 13_000_000), snap("2026-09-05", 12_340_000), snap("2026-10-01", 13_000_000), snap("2026-11-01", None),
                      snap("2026-12-01", 1, kind="land_ownership"), snap("2027-06-01", 0, 2027)])
    assert [(p["as_of"], p["price_krw_m2"], p["same_base"], p["delta_pct"]) for p in s] == [
        ("2026-09-05", 12_340_000, False, None), ("2026-09-25", 13_000_000, True, 5.35), ("2027-06-01", 0, False, -100.0)], "정렬·중복 제거·값 없음 건너뜀·다른 자료 무시·0 은 값"
    assert price_series([snap("2027-06-01", 5, 2027), snap("2028-06-01", 6, 2028)])[1]["delta_pct"] == 20.0
    assert price_series([snap("2026-01-01", 0), snap("2027-01-01", 5, 2027)])[1]["delta_pct"] is None, "앞 점이 0 이면 증감률 없음"


def test_cli_parcels_history_shows_trend(db, home, capsys, monkeypatch):
    _load_series(db)
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "parcels-history", P1]) == 0
    out = capsys.readouterr().out
    assert "공시지가 추이" in out and "2028년 1월 기준 12,350,000 (-5.0%)" in out
    assert cli.main(["db", "parcels-history", P1, "--json"]) == 0
    assert [p["price_krw_m2"] for p in json.loads(capsys.readouterr().out)["price_series"]] == [12_340_000, 13_000_000, 12_350_000]


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_phone_price_trend_matches_pc_series(db, home):
    """파생본의 attrs·attrs_history 로 폰이 되살린 추이가 정본 스냅샷의 추이와 같다."""
    _load_series(db)
    expected = {pnu: [(p["as_of"], p["price_krw_m2"], p["base_year"], p["base_month"], p["same_base"]) for p in attribute_history(db, pnu)["price_series"]] for pnu in (P1, P11)}
    r = build_projection(db, home, photos=False)
    assert r.outcome == "published", r.to_text()
    with zipfile.ZipFile(home / r.output_dir / r.zip_name) as z:
        geo = json.loads(z.read("parcels.geojson"))
    feats = {f["id"]: f["properties"] for f in geo["features"] if f["id"] in expected}
    js = ("import { priceTrend } from './web/app/parcels.js';\n"
          "const feats = JSON.parse(process.argv[1]);\n"
          "const out = {};\n"
          "for (const [id, p] of Object.entries(feats)) out[id] = priceTrend(p.attrs, p.attrs_history).map((q) => [q.as_of, q.price, q.year, q.month, q.sameBase]);\n"
          "console.log(JSON.stringify(out));\n")
    res = subprocess.run(["node", "--input-type=module", "-e", js, json.dumps(feats)], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    got = {k: [tuple(x) for x in v] for k, v in json.loads(res.stdout).items()}
    assert got == {k: v for k, v in expected.items()}
    assert len(got[P1]) == 3
