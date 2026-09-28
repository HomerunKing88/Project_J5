"""J5-041: 파생본 필지 번들의 스키마 검증은 쓴 파일을 다시 읽는 게시 전 확인(verify_projection_dir)에서 한 번만 한다.

시험: 스키마에 맞지 않는 필지 번들이 들어오면 게시하지 않고(verify_parcels, 스키마 오류 내용 포함) 이전 파생본을 유지한다. 정상 번들은 게시된다.
가상자료만 쓴다.
"""

from __future__ import annotations

from j5.db import projection as PJ
from j5.db.parcels import load_bundle
from j5.db.projection import build_projection, projection_status
from tests.test_j5_026_attr_history import db, home, vw_bundle  # noqa: F401 (fixture)


def test_bad_parcels_bundle_is_not_published(db, home, monkeypatch):
    load_bundle(db, vw_bundle())
    ok = build_projection(db, home, photos=False)
    assert ok.outcome == "published"
    real = PJ.parcels_bundle_from_db

    def broken(db_, **kw):
        b = real(db_, **kw)
        b["features"][0]["id"] = "not-a-pnu"   # 스키마 위반 (PNU 형식)
        return b
    monkeypatch.setattr(PJ, "parcels_bundle_from_db", broken)
    r = build_projection(db, home, photos=False)
    assert r.outcome == "failed"
    f = next(x for x in r.findings if x["code"] == "verify_parcels")
    assert "번들 스키마에 맞지 않는다" in f["message"] and "not-a-pnu" in f["message"], "스키마 오류 내용이 안내에 들어간다"
    st = projection_status(db, home)
    assert st["stale"] is False, "이전 파생본을 그대로 최신으로 둔다 (정본은 바뀌지 않았다)"
