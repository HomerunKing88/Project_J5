"""J5-053: 폰 연도별 요약의 소유 변동일이 PC `db parcels-years` 와 같다.

시험: 파생본 parcels.geojson 의 attrs·attrs_history 로 폰(web/app/parcels.js ownershipChanges)이 모은 소유 변동일·자료 여부가
정본 스냅샷으로 PC(parcel_years)가 모은 값과 같다(Node 로 실행). 원본에 소유 행이 없어 값이 모두 null 인 스냅샷은 양쪽 모두 자료 없음(모름)이다.
가상자료만 쓴다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from j5.db.parcel_years import _ownership_dates, parcel_years, years_text
from j5.db.parcels import attribute_history, load_bundle
from j5.db.projection import build_projection
from tests.test_j5_026_attr_history import P1, db, home, later_bundle, vw_bundle  # noqa: F401 (fixture)

REPO = Path(__file__).resolve().parent.parent
P3 = "9999900100100030000"        # 원본 소유 행이 비어 값이 모두 null
P_SAN = "9999900100200010002"     # 번들에 소유 필드가 없음


def _load(db):
    load_bundle(db, vw_bundle())                                     # 2026-09-05: 필지 1 소유 변동일 2017-01-01
    load_bundle(db, later_bundle("2027-09-01", owner="법인"))         # 필지 1 소유 변동 2027-09-01


def test_pc_ownership_unknown_when_values_all_null(db):
    _load(db)
    r = parcel_years(db, [P1, P3, P_SAN], year_from=2026, year_to=2027)
    by = {(x["pnu"], x["year"]): x for x in r["rows"]}
    assert by[(P1, 2027)]["ownership_snapshot"] is True and by[(P1, 2027)]["ownership_changed_on"] == ["2027-09-01"]
    assert by[(P3, 2027)]["ownership_snapshot"] is False, "값이 모두 null 인 소유 스냅샷은 자료 없음"
    assert by[(P_SAN, 2027)]["ownership_snapshot"] is False
    txt = years_text(r)
    assert "소유 변동 2027-09-01" in txt and txt.count("소유 자료 없음") == 4, "자료 없는 두 필지 × 두 해"


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_phone_ownership_matches_pc(db, home):
    """파생본으로 폰이 모은 소유 변동일·자료 여부가 정본 스냅샷으로 PC 가 모은 것과 같다."""
    _load(db)
    pnus = [f["id"] for f in vw_bundle()["features"]]
    snaps = {p: attribute_history(db, p)["snapshots"] for p in pnus}
    rows = parcel_years(db, pnus, year_from=2027, year_to=2027)["rows"]
    expected = {x["pnu"]: {"known": x["ownership_snapshot"], "dates": _ownership_dates(snaps[x["pnu"]])} for x in rows}
    r = build_projection(db, home, photos=False)
    assert r.outcome == "published", r.to_text()
    with zipfile.ZipFile(home / r.output_dir / r.zip_name) as z:
        geo = json.loads(z.read("parcels.geojson"))
    feats = {f["id"]: f["properties"] for f in geo["features"] if f["id"] in expected}
    assert set(feats) == set(expected)
    js = ("import { ownershipChanges } from './web/app/parcels.js';\n"
          "const feats = JSON.parse(process.argv[1]);\n"
          "const out = {};\n"
          "for (const [id, p] of Object.entries(feats)) out[id] = ownershipChanges(p.attrs, p.attrs_history);\n"
          "console.log(JSON.stringify(out));\n")
    res = subprocess.run(["node", "--input-type=module", "-e", js, json.dumps(feats)], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got == expected
    assert got[P1]["dates"] == ["2017-01-01", "2027-09-01"] and got[P3] == {"known": False, "dates": []}
