"""J5-039 (ADR-23): 정본 필지 상한과 폰 파생본 범위를 나눈다.

시험: 두 법정동을 정본에 넣고 법정동별 수, 폰 상한을 넘으면 파생본이 실패하고 이전본 유지, 범위를 정하면 그 법정동 + 물건이 연결된 필지만 실리고
warnings 에 적힘(번들 스키마 통과), 범위 변경은 dataset_version 을 올리지 않음, 잘못된·없는 코드 거절, 전체로 되돌리기, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import copy
import json

import pytest

from j5 import cli
from j5.db import projection as PJ
from j5.db.parcels import apply_links, load_bundle, parcels_bundle_from_db, phone_scope, scope_overview, set_phone_scope
from j5.db.projection import build_projection, projection_status
from j5.db.validate import ValidationError
from j5.schemas_loader import schema_errors
from tests.test_j5_026_attr_history import db, home, vw_bundle  # noqa: F401 (fixture)

EMD1, EMD2 = "9999900100", "9999900200"
A2 = "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a52"   # 위치점 없는 가상 물건 3


def second_emd_bundle() -> dict:
    """가상 번들을 옆 법정동으로 옮긴 사본: PNU·법정동 코드·이름을 바꾸고 경도를 0.01 옮긴다."""
    b = copy.deepcopy(vw_bundle())
    b["source"]["name"] = "가상 VWorld 옆 동"
    for a in b["attrs_sources"]:   # 옆 동은 다른 자료 파일에서 왔다 (출처가 범위에 따라 갈리는지 본다)
        a["dbf_sha256"] = "e" * 64
        a["name"] = a["name"] + " (옆 동)"

    def shift(c):
        return [shift(x) for x in c] if isinstance(c[0], list) else [c[0] + 0.01, c[1]]
    for f in b["features"]:
        pnu = EMD2 + f["id"][10:]
        f["id"] = pnu
        p = f["properties"]
        p["pnu"], p["emd_code"], p["emd_name"] = pnu, EMD2, "가상이동"
        f["geometry"]["coordinates"] = shift(f["geometry"]["coordinates"])
        p["bbox"] = [p["bbox"][0] + 0.01, p["bbox"][1], p["bbox"][2] + 0.01, p["bbox"][3]]
    b["bbox"] = [b["bbox"][0] + 0.01, b["bbox"][1], b["bbox"][2] + 0.01, b["bbox"][3]]
    b["clip"]["bbox"] = b["bbox"]
    return b


@pytest.fixture
def two_emd(db):
    load_bundle(db, vw_bundle())
    load_bundle(db, second_emd_bundle())
    return db


def test_overview_and_bundle_scope(two_emd):
    db = two_emd
    o = scope_overview(db)
    assert o["scope"] is None and o["total_parcels"] == 12 and o["phone_parcels"] == 12
    assert [(e["emd_code"], e["parcels"], e["in_scope"]) for e in o["by_emd"]] == [(EMD1, 6, True), (EMD2, 6, True)]
    v = db.meta("dataset_version")
    other = EMD2 + "100010000"
    apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A2, "pnu": other, "effective_from": "2026-01-01", "effective_to": None, "basis": "manual"}]})
    v = db.meta("dataset_version")
    o = set_phone_scope(db, [EMD1])
    assert o["scope"] == [EMD1] and o["phone_parcels"] == 7 and o["changed"], "범위 안 6필지 + 물건이 연결된 옆 동 필지 1"
    assert int(db.meta("dataset_version")) == int(v) + 1, "범위가 바뀌면 dataset_version 을 올린다: 이전 범위의 파생본·백업이 최신으로 보이지 않게 (리뷰 반영 PR #86)"
    assert set_phone_scope(db, [EMD1])["changed"] is False and int(db.meta("dataset_version")) == int(v) + 1, "같은 범위는 그대로"
    b = parcels_bundle_from_db(db, generated_at="2026-09-28T00:00:00Z")
    assert b["count"] == 7 and {f["id"] for f in b["features"] if f["properties"]["emd_code"] == EMD2} == {other}
    # 출처는 실은 필지의 것만: 옆 동에서는 연결된 1필지만 실었으므로 옆 동 자료도 적힌다. 옆 동만 범위로 하면 이 동의 자료는 빠진다 (리뷰 반영 PR #86)
    assert {a["dbf_sha256"] for a in b["attrs_sources"]} >= {"e" * 64}
    set_phone_scope(db, [EMD2])
    b2 = parcels_bundle_from_db(db, generated_at="2026-09-28T00:00:00Z")
    assert {a["dbf_sha256"] for a in b2["attrs_sources"]} == {"e" * 64}, "범위 밖 지역의 자료 출처를 적지 않는다"
    assert any("폰 범위" in w and "12개 중" in w for w in b["warnings"]) and schema_errors("parcels_bundle.schema.json", b) == []
    assert set_phone_scope(db, None)["scope"] is None and phone_scope(db) is None
    assert parcels_bundle_from_db(db, generated_at="2026-09-28T00:00:00Z")["count"] == 12


def test_bad_codes(two_emd):
    for codes in (["123"], ["9999900300"]):
        with pytest.raises(ValidationError):
            set_phone_scope(two_emd, codes)
    assert phone_scope(two_emd) is None


def test_projection_fails_over_phone_limit_until_scoped(two_emd, home, monkeypatch):
    db = two_emd
    monkeypatch.setattr(PJ, "PARCELS_PHONE_LIMIT", 8)
    r = build_projection(db, home, photos=False)
    assert r.outcome == "failed" and any(m["code"] == "parcels_phone_limit" and "phone-scope" in m["message"] for m in r.findings)
    assert r.source_dataset_version == int(db.meta("dataset_version")) > 0, "실패 기록에도 실제 정본 버전 (리뷰 반영 PR #86)"
    set_phone_scope(db, [EMD2])
    r = build_projection(db, home, photos=False)
    assert r.outcome == "published", r.findings
    assert projection_status(db, home)["stale"] is False
    set_phone_scope(db, [EMD1])
    assert projection_status(db, home)["stale"] is True, "범위를 바꾸면 이전 범위의 파생본은 구본이다"
    pb = json.loads((home / r.output_dir / "parcels.geojson").read_text(encoding="utf-8"))
    assert pb["count"] == 6 and {f["properties"]["emd_code"] for f in pb["features"]} == {EMD2} and r.counts["parcels"] == 6


def test_cli(two_emd, capsys):
    path = str(two_emd.path)
    two_emd.close()
    assert cli.main(["db", "--db", path, "phone-scope"]) == 0
    out = capsys.readouterr().out
    assert "전체 (범위를 정하지 않음)" in out and f"{EMD2} 가상이동 · 6필지" in out
    assert cli.main(["db", "--db", path, "phone-scope", "--emd", EMD1]) == 0
    assert "폰에 실릴 필지 6개" in capsys.readouterr().out
    assert cli.main(["db", "--db", path, "phone-scope", "--emd", "999"]) == 1, "입력 거절 (다른 db 명령의 ValidationError 와 같은 종료 코드)"
    assert cli.main(["db", "--db", path, "phone-scope", "--emd", EMD1, "--all"]) == cli.USAGE_ERROR
    assert cli.main(["db", "--db", path, "phone-scope", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["scope"] is None
