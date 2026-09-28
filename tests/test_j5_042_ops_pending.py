"""J5-042: 운영 점검이 최근 기능의 할 일을 함께 본다.

시험: 받아 두고 반영하지 않은 실거래 수집 실행은 조치 항목(rt_unloaded)이고 반영하면 사라진다, 폰에 실릴 필지가 폰 상한을 넘으면
조치 항목(phone_scope_over_limit)과 폰 범위 줄, 건축물대장 표본 수집은 안내(주의)로만 두어 통과를 막지 않는다. 가상자료만 쓴다(네트워크 없음).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from j5.collect import br as BR
from j5.collect.rt import collect_months
from j5.db import parcels as PM
from j5.db.backup import create_backup
from j5.db.ops import ops_check, ops_text
from j5.db.parcels import load_bundle
from j5.db.projection import build_projection
from j5.db.transactions import load_run
from tests.test_j5_014_collect import KEY, item, xml_page
from tests.test_j5_017a_ops import db, home  # noqa: F401 (fixture)
from tests.test_j5_026_attr_history import vw_bundle

ENDPOINT = "http://127.0.0.1:9/fake"


def opener(items):
    def f(url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        page, rows = int(q["pageNo"]), int(q["numOfRows"])
        return 200, xml_page(items[(page - 1) * rows:page * rows], len(items), page, rows)
    return f


def make_ok(db, home):
    assert create_backup(db, home).outcome == "completed" and build_projection(db, home).outcome == "published"
    r = ops_check(home)
    assert r["ok"], r["actions"]
    return r


def codes(r):
    return [a["code"] for a in r["actions"]]


def test_unloaded_rt_run_is_an_action_until_loaded(db, home):
    make_ok(db, home)
    rows = [dict(item(i), sggCd="11110", landUse="일반상업", shareDealingType="") for i in range(2)]
    run = collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202608"], endpoint=ENDPOINT, opener=opener(rows), sleep=lambda s: None)
    r = ops_check(home)
    assert codes(r) == ["rt_unloaded"] and "시군구 11110 1건" in r["actions"][0]["text"] and "rt-load" in r["actions"][0]["text"]
    assert r["collect_pending"]["rt"] == {"11110": 1} and "반영 대기 수집: 시군구 11110 1건" in ops_text(r)
    load_run(db, home, "11110", run.run_id)
    r2 = ops_check(home)
    assert "rt_unloaded" not in codes(r2), "반영하면 사라진다 (정본이 바뀌어 백업·파생본은 다시 필요)"
    make_ok(db, home)


def test_phone_scope_over_limit_is_an_action(db, home, monkeypatch):
    load_bundle(db, vw_bundle())
    r = make_ok(db, home)
    assert r["phone_scope"] == {"scope": None, "total_parcels": 6, "phone_parcels": 6, "phone_limit": PM.MAX_FEATURES}
    assert "폰 범위: 전체 · 폰에 실릴 필지 6개" in ops_text(r)
    monkeypatch.setattr(PM, "MAX_FEATURES", 3)
    r2 = ops_check(home)
    assert codes(r2) == ["phone_scope_over_limit"] and "phone-scope" in r2["actions"][0]["text"]


def test_br_samples_are_a_notice_not_an_action(db, home):
    make_ok(db, home)
    BR.collect_buildings(home, key=KEY, key_source="t", pnus=["9999900100100010000"], ops=["title"], endpoint_base=ENDPOINT,
                         opener=lambda url: (200, xml_page([], 0, 1, 10)), sleep=lambda s: None)
    r = ops_check(home)
    assert r["ok"] and r["collect_pending"]["br_hub_runs"] == 1
    assert any("건축물대장 표본 수집 1건" in w for w in r["warnings"])


def test_store_changing_actions_come_before_backup_and_projection(db, home, monkeypatch):
    """리뷰 반영 PR #89: 조치는 순서대로 한다. 정본을 바꾸는 조치(폰 범위·수집 반영)를 백업·파생본 조치보다 먼저 낸다."""
    make_ok(db, home)
    load_bundle(db, vw_bundle())                  # 정본이 바뀌어 백업·파생본이 구본
    monkeypatch.setattr(PM, "MAX_FEATURES", 3)    # 폰 상한도 넘는다
    rows = [dict(item(i), sggCd="11110", landUse="일반상업", shareDealingType="") for i in range(2)]
    collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202608"], endpoint=ENDPOINT, opener=opener(rows), sleep=lambda s: None)
    r = ops_check(home)
    assert codes(r) == ["phone_scope_over_limit", "rt_unloaded", "backup_stale", "projection_stale"], codes(r)
