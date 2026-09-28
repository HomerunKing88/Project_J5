"""J5-035: 페이지 넘김이 맞지 않는 응답을 받지 않는다 (PR #81 리뷰에서 나온 실거래 수집 후속).

API 가 pageNo 를 무시하고 앞 페이지를 되풀이하면, 겹친 행을 더하다 totalCount 에 닿아 뒷 행을 받지 않고도 complete 가 되고,
정본 반영에서는 같은 응답 안의 같은 행이 순번으로 나뉘어 별개 거래가 된다(데이터 사전 §7.2 순번 규칙). 시험:
- 수집(rt-sample/rt-fetch): 응답 pageNo 가 요청과 다르면 그 달은 failed, 항목을 더하지 않음. pageNo 가 없는 응답은 이전과 같음. 재요약은 맞지 않는 원본을 넣지 않음.
- 정본 반영(rt-load): 이 검사 전 도구로 받은 실행(맞지 않는 원본이 ok 로 기록됨)은 raw_page_mismatch 로 거절, 정본 무변화.
- 건축물대장 요약도 같은 규칙(맞지 않는 원본 파일 제외).
가상 응답만 쓴다 (네트워크 없음).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from j5.collect import br as BR
from j5.collect import rt
from j5.collect.rt import collect_months, load_month_items, page_mismatch, page_of_file, report_from_raw
from j5.db.store import Db, DbError
from j5.db.transactions import load_run
from tests.test_j5_014_collect import KEY, item, xml_page

ENDPOINT = "http://127.0.0.1:9/fake"   # 실제 제공자 아님 (호출하지 않는다: opener 로 응답을 준다)


def stuck_opener(items: list[dict], *, with_page_no: bool = True, page_no_text: str | None = None):
    """pageNo 를 무시하고 늘 1페이지를 주는 응답. with_page_no=False 면 응답에 pageNo 가 없다. page_no_text 를 주면 pageNo 값을 그 글로 바꾼다."""
    def opener(url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        rows = int(q["numOfRows"])
        data = xml_page(items[:rows], len(items), 1, rows)
        if not with_page_no:
            data = data.replace(b"<pageNo>1</pageNo>", b"")
        elif page_no_text is not None:
            data = data.replace(b"<pageNo>1</pageNo>", f"<pageNo>{page_no_text}</pageNo>".encode("utf-8"))
        return 200, data
    return opener


def good_opener(items: list[dict]):
    def opener(url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        page, rows = int(q["pageNo"]), int(q["numOfRows"])
        return 200, xml_page(items[(page - 1) * rows:page * rows], len(items), page, rows)
    return opener


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_helpers():
    assert page_mismatch({"page_no": 1}, 2) and not page_mismatch({"page_no": 2}, 2) and not page_mismatch({"page_no": None}, 2)
    assert page_mismatch({"page_no": None, "page_no_raw": "abc"}, 2) and page_mismatch({"page_no": None, "page_no_raw": ""}, 1), "있는데 숫자가 아니면 이상 응답"
    assert rt.parse_response(xml_page([], 0, 1, 10).replace(b"<pageNo>1</pageNo>", b"<pageNo>abc</pageNo>"))["page_no_raw"] == "abc"
    assert rt.parse_response(xml_page([], 0, 1, 10).replace(b"<pageNo>1</pageNo>", b""))["page_no_raw"] is None
    assert page_of_file("p003-20260928-010000-abcdef.xml") == 3 and page_of_file("x.xml") is None and page_of_file("pabc-1.xml") is None


def test_collect_marks_repeated_page_failed_and_report_skips_it(home):
    items = [item(i) for i in range(4)]
    run = collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202608"], endpoint=ENDPOINT, num_rows=2, opener=stuck_opener(items), sleep=lambda s: None)
    m = run.months[0]
    assert m.outcome == "failed" and m.items == 2, "되풀이된 1페이지의 행을 더하지 않는다 (이전에는 4건 complete)"
    assert m.pages[1].outcome == "bad_response" and "pageNo 1" in m.pages[1].error
    items_back, files = load_month_items(home, "11110", "202608", run.run_id)
    assert len(items_back) == 2 and files == [f"p001-{run.run_id}.xml"], "재요약은 페이지가 맞지 않는 원본(p002)을 넣지 않는다"
    assert report_from_raw(home, "11110", ["202608"], run.run_id)["months"][0]["items"] == 2
    # 응답에 pageNo 가 없으면 확인할 수 없으므로 이전과 같다 (거절하지 않는다)
    run2 = collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202607"], endpoint=ENDPOINT, num_rows=2,
                          opener=stuck_opener(items, with_page_no=False), sleep=lambda s: None)
    assert run2.months[0].outcome == "complete" and run2.months[0].items == 4
    # pageNo 가 있는데 숫자가 아니면(되풀이된 1페이지를 알아볼 수 없음) 이상 응답으로 실패 (리뷰 반영 PR #82). 첫 페이지부터 실패한다
    run4 = collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202605"], endpoint=ENDPOINT, num_rows=2,
                          opener=stuck_opener(items, page_no_text="abc"), sleep=lambda s: None)
    assert run4.months[0].outcome == "failed" and run4.months[0].items == 0 and "'abc'" in run4.months[0].pages[0].error
    # 정상 페이지 넘김은 그대로 complete
    run3 = collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202606"], endpoint=ENDPOINT, num_rows=2, opener=good_opener(items), sleep=lambda s: None)
    assert run3.months[0].outcome == "complete" and run3.months[0].items == 4 and len(run3.months[0].pages) == 2


def test_rt_load_rejects_run_recorded_by_old_tool(home, monkeypatch):
    """검사 전 도구로 받은 실행을 흉내 낸다: 맞지 않는 원본이 ok 로 기록되고 달은 complete. 반영하면 같은 행 2건이 순번 1 의 별개 거래가 됐다."""
    items = [dict(item(i), sggCd="11110", landUse="일반상업", shareDealingType="") for i in range(4)]
    with monkeypatch.context() as mp:
        mp.setattr(rt, "page_mismatch", lambda parsed, page_no: False)
        old = collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202608"], endpoint=ENDPOINT, num_rows=2, opener=stuck_opener(items), sleep=lambda s: None)
    assert old.months[0].outcome == "complete" and old.months[0].items == 4, "옛 도구는 complete 로 적었다"
    with monkeypatch.context() as mp:
        mp.setattr(rt, "page_mismatch", lambda parsed, page_no: False)
        bad = collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202607"], endpoint=ENDPOINT, num_rows=2,
                             opener=stuck_opener(items, page_no_text="abc"), sleep=lambda s: None)
    assert bad.months[0].outcome == "complete"
    db = Db.create(home / "db" / "j5.sqlite3", study_id="j5-synthetic-study", data_mode="synthetic")
    try:
        before = db.status()["counts"]
        with pytest.raises(DbError) as e:
            load_run(db, home, "11110", old.run_id)
        assert e.value.code == "raw_page_mismatch" and "rt-fetch --refresh" in e.value.message
        after = db.status()["counts"]
        assert after["transactions"] == before["transactions"] == 0 and after["collection_runs"] == 0 and after["transaction_observations"] == 0, "정본 무변화"
        with pytest.raises(DbError) as e:
            load_run(db, home, "11110", bad.run_id)
        assert e.value.code == "raw_page_mismatch" and "'abc'" in e.value.message, "숫자가 아닌 pageNo 도 거절 (리뷰 반영 PR #82)"
        # 새 도구로 다시 받은 정상 실행은 반영된다
        good = collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202608"], endpoint=ENDPOINT, num_rows=2, opener=good_opener(items), sleep=lambda s: None)
        r = load_run(db, home, "11110", good.run_id)
        assert r.observations == 4 and db.status()["counts"]["transactions"] == 4
    finally:
        db.close()


def test_br_summary_skips_mismatched_page_files(home):
    """건축물대장 요약도 같은 규칙: 맞지 않는 원본 파일은 요약에서 뺀다 (수집 결과는 PR #81 에서 이미 failed)."""
    items = [{"mgmBldrgstPk": f"k{i}", "mainPurpsCdNm": "업무시설"} for i in range(4)]
    run = BR.collect_buildings(home, key=KEY, key_source="t", pnus=["9999900100100010000"], ops=["title"], endpoint_base=ENDPOINT, num_rows=2,
                               opener=stuck_opener(items), sleep=lambda s: None)
    assert run.targets[0].outcome == "failed"
    got, files = BR.load_target_items(home, "9999900100100010000", "title", run.run_id)
    assert len(got) == 2 and files == [f"p001-{run.run_id}.xml"]
    assert BR.report_from_raw(home, run.run_id)["operations"][0]["items"] == 2
