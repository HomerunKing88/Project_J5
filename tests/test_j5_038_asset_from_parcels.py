"""J5-038: 필지 목록으로 물건 만들기 (db asset-from-parcels).

시험: 필지 안의 점(오목 필지 포함, 폰 interiorPoint 와 같은 규칙), 물건·연결·관심 단계를 한 트랜잭션으로, 이미 물건이 있는 필지 건너뜀,
없는 PNU·형식 오류·상한은 전체 거절(정본 무변화), 미리 보기는 쓰지 않음, 중간 실패(사유 필요한 단계)면 아무것도 남지 않음,
parcels-find CSV 에서 읽기, 만든 뒤 parcels-find 결과(물건 없음 → 관찰목록), 파생본 시드, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from j5 import cli
from j5.db.parcel_assets import create_from_parcels, interior_point, pnus_from_csv
from j5.db.parcel_find import find_csv, find_parcels
from j5.db.parcels import active_links, geometry_contains, load_bundle
from j5.db.projection import build_projection
from j5.db.store import Db, DbError
from j5.db.validate import ValidationError
from tests.test_j5_026_attr_history import db, home, vw_bundle  # noqa: F401 (fixture)

P42, PS12 = "9999900100100040002", "9999900100200010002"   # 가상 번들에서 물건이 없는 두 필지 (4-2, 산1-2)
P1 = "9999900100100010000"


def counts(db):
    st = db.status()["counts"]
    return st["assets"], st["asset_components"], st["tracking_changes"], db.meta("dataset_version")


def test_interior_point_inside_even_for_concave_parcel():
    # ㄷ자 필지: 면적 중심이 빈 곳(밖)에 떨어진다 → 격자에서 안의 점을 찾는다
    ring = [[0, 0], [3, 0], [3, 1], [1, 1], [1, 2], [3, 2], [3, 3], [0, 3], [0, 0]]
    g = {"type": "Polygon", "coordinates": [ring]}
    pt = interior_point(g, [0, 0, 3, 3])
    assert pt is not None and geometry_contains(g, pt[0], pt[1])
    for f in vw_bundle()["features"]:
        p = interior_point(f["geometry"], f["properties"]["bbox"])
        assert p is not None and geometry_contains(f["geometry"], p[0], p[1]), f["id"]


def test_create_link_track_and_skip_existing(db):
    load_bundle(db, vw_bundle())
    before = counts(db)
    r = create_from_parcels(db, [P42, P1, PS12], track="watch", effective_from="2026-09-28")
    assert [c["pnu"] for c in r["created"]] == [P42, PS12]
    assert [s["pnu"] for s in r["skipped"]] == [P1] and "이미 물건이 있다" in r["skipped"][0]["reason"], "위치점이 든 물건이 있는 필지는 건너뛴다"
    a, links, tc, v = counts(db)
    assert a == before[0] + 2 and links == before[1] + 2 and tc == before[2] + 2 and int(v) > int(before[3])
    for c in r["created"]:
        row = db.conn.execute("SELECT label, lon, lat, tracking_status, resolution_status, data_mode, notes FROM assets WHERE asset_id = ?", (c["asset_id"],)).fetchone()
        assert row["label"] == c["parcel"] and row["tracking_status"] == "watch" and row["resolution_status"] == "confirmed" and row["data_mode"] == db.data_mode
        assert "asset-from-parcels" in row["notes"] and [row["lon"], row["lat"]] == c["location_point"]
    assert {(l["asset_id"], l["pnu"]) for l in active_links(db, "2026-09-28")} >= {(c["asset_id"], c["pnu"]) for c in r["created"]}
    # 다시 실행하면 모두 건너뛴다 (정본 연결 + 위치점)
    again = create_from_parcels(db, [P42, PS12], effective_from="2026-09-28")
    assert again["created"] == [] and len(again["skipped"]) == 2 and counts(db)[:3] == (a, links, tc)
    # 조건 찾기와 이어진다: 물건 없는 필지 → 없음, 관찰목록 물건 있는 필지에 들어온다
    assert find_parcels(db, {"assets": "none"}, on_date="2026-09-28")["total"] == 0
    assert {row["pnu"] for row in find_parcels(db, {"assets": "watch"}, on_date="2026-09-28")["rows"]} == {P42, PS12}


def test_rejects_whole_request_and_rolls_back(db):
    load_bundle(db, vw_bundle())
    before = counts(db)
    for pnus, code in (([P42, "9999900100199990000"], "parcel_missing"), ([P42, "123"], "bad_pnu"), ([], "no_pnu"), ([f"99999001001{i:04d}0000" for i in range(51)], "too_many")):
        with pytest.raises(DbError) as e:
            create_from_parcels(db, pnus)
        assert e.value.code == code
    assert counts(db) == before
    # 사유가 필요한 단계를 사유 없이 주면 물건·연결까지 모두 되돌린다
    with pytest.raises(ValidationError):
        create_from_parcels(db, [P42], track="hold")
    assert counts(db) == before, "한 트랜잭션: 중간 실패면 아무것도 남지 않는다"
    with pytest.raises(DbError) as e:
        create_from_parcels(db, [P42], effective_from="2026-02-30", dry_run=True)
    assert e.value.code == "bad_date", "미리 보기에서도 날짜를 검사한다"
    # 미리 보기는 쓰지 않는다
    dry = create_from_parcels(db, [P42, P1], track="watch", dry_run=True)
    assert [c["pnu"] for c in dry["created"]] == [P42] and dry["created"][0]["asset_id"] is None and counts(db) == before


def test_csv_from_parcels_find_and_cli(db, home, tmp_path, capsys):
    load_bundle(db, vw_bundle())
    csv_path = tmp_path / "found.csv"
    csv_path.write_text(find_csv(find_parcels(db, {"assets": "none"})), encoding="utf-8")
    assert pnus_from_csv(csv_path) == [PS12, P42], "공부면적 큰 순 그대로"
    bad = tmp_path / "bad.csv"
    bad.write_text("x,y\n1,2\n", encoding="utf-8")
    with pytest.raises(DbError):
        pnus_from_csv(bad)
    path = str(db.path)
    db.close()
    assert cli.main(["db", "--db", path, "asset-from-parcels", "--csv", str(csv_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "만들 물건 (미리 보기, 정본에 쓰지 않음) 2개" in out
    assert cli.main(["db", "--db", path, "asset-from-parcels", "--csv", str(csv_path), "--pnu", P1, "--track", "watch", "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert len(r["created"]) == 2 and [s["pnu"] for s in r["skipped"]] == [P1] and r["track"] == "watch"
    assert cli.main(["db", "--db", path, "asset-from-parcels", "--pnu", "9999900100199990000"]) == 1
    assert "정본에 없는 필지" in capsys.readouterr().err
    assert cli.main(["db", "--db", path, "asset-from-parcels", "--pnu", P42, "--track", "purchase_ready"]) == cli.USAGE_ERROR, "argparse 선택지에 없다"
    # 파생본 시드에 관찰 단계로 실린다
    d = Db.open(Path(path))
    try:
        pr = build_projection(d, home, photos=False)
        seed = json.loads((home / pr.output_dir / "assets.seed.json").read_text(encoding="utf-8"))
    finally:
        d.close()
    assert sum(1 for a in seed if a.get("tracking_status") == "watch") == 2
