"""J5-040: 번들 한 개의 상한(8,000필지)을 넘는 VWorld 묶음을 나눠 넣는다 (ADR-23 후속).

시험: split_bundle(PNU 순 조각, 조각마다 필지 수·범위·경고, 스키마 통과, 상한 이하면 그대로), 묶음 넣기가 큰 묶음을 partNNofMM 번들로 나눠 모두 반영,
다시 실행하면 조각을 다시 쓰고 변화 없음, 조각이 일부만 남았으면 거절(정본 무변화), 나누지 않는 변환의 상한은 그대로, 결과 글.
조각 크기는 시험에서 작게 바꾼다(가상 6필지). 가상자료만 쓴다.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from j5.db import ingest as ING
from j5.parcels.convert import MAX_FEATURES, SPLIT_MAX_FEATURES, Clip, ConvertError, ConvertOptions, convert, split_bundle
from j5.schemas_loader import schema_errors
from tests.test_j5_031_ingest import GV, counts, db, home, vw_zip  # noqa: F401 (fixture)

FIX = Path(__file__).resolve().parent / "fixtures" / "parcels"


def test_split_bundle_parts():
    b = json.loads((FIX / "synthetic_vworld.j5parcels.json").read_text(encoding="utf-8"))
    assert split_bundle(b, 8000) == [b], "상한 이하면 그대로"
    shuffled = copy.deepcopy(b)
    shuffled["features"].reverse()
    parts = split_bundle(shuffled, 4)
    assert [p["count"] for p in parts] == [4, 2] and [len(p["features"]) for p in parts] == [4, 2]
    ids = [f["id"] for p in parts for f in p["features"]]
    assert ids == sorted(f["id"] for f in b["features"]), "PNU 순으로 나눈다"
    for i, p in enumerate(parts):
        assert schema_errors("parcels_bundle.schema.json", p) == []
        bb = [f["properties"]["bbox"] for f in p["features"]]
        assert p["bbox"] == [min(x[0] for x in bb), min(x[1] for x in bb), max(x[2] for x in bb), max(x[3] for x in bb)]
        assert any(f"{i + 1}/2" in w for w in p["warnings"]) and p["source"] == b["source"] and p["attrs_sources"] == b["attrs_sources"]


def test_convert_limit_without_split_unchanged(vw_zip, tmp_path):
    clip = Clip.from_bbox("0,0,1,1")
    with pytest.raises(ConvertError) as e:
        convert(vw_zip, ConvertOptions(clip=clip, geometry_version=GV, source_name="x", max_features=MAX_FEATURES + 1))
    assert e.value.code == "bad_max_features", "나누지 않는 변환(parcels convert)의 상한은 그대로다"
    with pytest.raises(ConvertError):
        convert(vw_zip, ConvertOptions(clip=clip, geometry_version=GV, source_name="x", split=True, max_features=SPLIT_MAX_FEATURES + 1))


def test_ingest_splits_big_source_and_reuses_parts(db, home, vw_zip, monkeypatch):
    monkeypatch.setattr(ING, "PART_SIZE", 4)
    r = ING.ingest_vworld(db.path, home, [vw_zip], geometry_version=GV)
    assert r.outcome == "applied", r.to_text()
    it = r.items[0]
    assert len(it.bundles) == 2 and it.bundles[0].endswith(".part01of02.j5parcels.json") and it.bundles[1].endswith(".part02of02.j5parcels.json")
    assert it.count == 6 and it.attrs == 6 and it.load["inserted"] == 6 and it.load["parts"] == 2 and counts(db) == (6, 6, 18)
    assert all((home / b).is_file() for b in it.bundles) and it.bundle == it.bundles[0]
    assert "번들 2개로 나눔" in r.to_text()
    # 다시 실행: 조각을 다시 쓰고 변화 없음
    r2 = ING.ingest_vworld(db.path, home, [vw_zip], geometry_version=GV, backup=False)
    assert r2.outcome == "unchanged" and r2.items[0].reused and r2.items[0].bundles == it.bundles and r2.items[0].load["outcome"] == "unchanged"
    # 조각이 일부만 남았으면 거절한다 (중간에 끊긴 변환). 정본은 그대로
    (home / it.bundles[1]).unlink()
    v = db.status()["dataset_version"]
    r3 = ING.ingest_vworld(db.path, home, [vw_zip], geometry_version=GV, backup=False)
    assert r3.outcome == "failed" and r3.stage == "convert" and "parts_incomplete" in (r3.items[0].error or "")
    assert db.status()["dataset_version"] == v and counts(db) == (6, 6, 18)


def test_small_source_keeps_single_bundle_name(db, home, vw_zip):
    r = ING.ingest_vworld(db.path, home, [vw_zip], geometry_version=GV, backup=False)
    it = r.items[0]
    assert it.bundles == [it.bundle] and ".part" not in it.bundle, "나누지 않는 묶음의 파일 이름은 이전과 같다 (이전 번들을 다시 쓴다)"


def test_later_part_failure_keeps_earlier_part_results(db, home, vw_zip, monkeypatch):
    """리뷰 반영 PR #87 (P1): 나눈 입력의 뒤 조각이 실패해도 앞 조각(각자 완결된 트랜잭션)의 반영을 결과에 남긴다."""
    monkeypatch.setattr(ING, "PART_SIZE", 4)
    real = ING.load_bundle
    calls = []

    def flaky(db_, doc):
        calls.append(doc["count"])
        if len(calls) == 2:
            raise RuntimeError("가상 반영 실패")
        return real(db_, doc)
    monkeypatch.setattr(ING, "load_bundle", flaky)
    r = ING.ingest_vworld(db.path, home, [vw_zip], geometry_version=GV, backup=False)
    it = r.items[0]
    assert r.outcome == "failed" and r.stage == "load" and "가상 반영 실패" in it.error
    assert it.load["partial"] and it.load["parts"] == 1 and it.load["parts_total"] == 2 and it.load["inserted"] == 4
    assert counts(db)[0] == 4, "앞 조각은 정본에 들어갔다"
    assert "나눈 번들 2개 중 앞 1개가 반영됐다 (신규 4" in r.message and "나눈 번들 2개 중 1개는 반영됨 (신규 4" in r.to_text()
    monkeypatch.setattr(ING, "load_bundle", real)
    again = ING.ingest_vworld(db.path, home, [vw_zip], geometry_version=GV, backup=False)
    assert again.outcome == "applied" and again.items[0].load["inserted"] == 2 and again.items[0].load["unchanged"] == 4 and counts(db) == (6, 6, 18)
