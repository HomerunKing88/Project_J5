"""J5-034: 건축물대장 API(건축HUB) 표본 수집 도구. 릴리스 계획 §6 R2, J5-013B-3 첫 조각.

시험: PNU → 요청 변수(시군구·법정동·대지구분·본번·부번), 오퍼레이션 키, URL(부호화/이미 부호화된 키, 기본 주소 검사),
로컬 HTTP 서버로 필지 × 오퍼레이션마다 페이지네이션·정상 0건·API 오류·HTTP 오류(거절 사유 보관)·페이지 누락 구분, 원본 저장 경로·기록·로그에 키가 없음,
오퍼레이션별 요약(긴 글의 값 분포는 싣지 않음), 저장된 원본 재요약, 대상 상한, CLI(--pnu, 정본 연결 --linked, 종료 코드). 실제 공공데이터포털은 호출하지 않는다.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from j5 import cli
from j5.collect import br as BR
from j5.collect.rt import CollectError
from j5.db.parcels import apply_links, load_bundle
from j5.db.store import Db

KEY = "abcDEF123+/=testkeyNOTREAL0123456789xyz"  # 가상 키 (형식만 흉내)
P1, P2, PM = "9999900100100010000", "9999900100100020000", "9999900100200010002"   # 가상 PNU (PM 은 산)
FIX = Path(__file__).resolve().parent / "fixtures"


def xml_page(items: list[dict], total, page: int, rows: int, code: str = "00", msg: str = "NORMAL SERVICE.") -> bytes:
    body = "".join("<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in it.items()) + "</item>" for it in items)
    return (f'<?xml version="1.0" encoding="UTF-8"?><response><header><resultCode>{code}</resultCode><resultMsg>{msg}</resultMsg></header>'
            f"<body><items>{body}</items><numOfRows>{rows}</numOfRows><pageNo>{page}</pageNo><totalCount>{total}</totalCount></body></response>").encode("utf-8")


def title_item(i: int) -> dict:
    """가상 표제부 항목 (필드 이름은 흉내일 뿐 코드가 가정하지 않는다)."""
    return {"mgmBldrgstPk": f"99999-{i:05d}", "platPlc": f"가상시 가상구 가상동 {i}번지 아주 긴 주소 글", "bldNm": "", "mainPurpsCdNm": "제2종근린생활시설" if i % 2 else "업무시설",
            "totArea": f"{300 + i}.5", "vlRatEstmTotArea": f"{280 + i}.0", "archArea": "150.2", "grndFlrCnt": "4", "useAprDay": "19890101"}


class _Handler(BaseHTTPRequestHandler):
    scenarios: dict = {}
    calls: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        op = parts.path.rsplit("/", 1)[-1]
        _Handler.calls.append((op, q))
        assert "serviceKey" in q and q.get("_type") == "xml"
        pnu = q["sigunguCd"] + q["bjdongCd"] + {"0": "1", "1": "2"}[q["platGbCd"]] + q["bun"] + q["ji"]
        sc = _Handler.scenarios.get((pnu, op), {"kind": "pages", "items": []})
        page, rows = int(q.get("pageNo", 1)), int(q.get("numOfRows", 10))
        if sc["kind"] == "forbidden":
            body = (f"<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg><returnAuthMsg>SERVICE_ACCESS_DENIED_ERROR for {self.path}</returnAuthMsg>"
                    "<returnReasonCode>20</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>").encode("utf-8")
            self.send_response(403); self.send_header("Content-Type", "application/xml"); self.end_headers(); self.wfile.write(body); return
        if sc["kind"] == "api_error":
            data = xml_page([], 0, page, rows, code="99", msg="APPLICATION ERROR")
        elif sc["kind"] == "short_pages":
            data = xml_page(sc["items"] if page == 1 else [], sc["claimed_total"], page, rows)
        else:
            data = xml_page(sc["items"][(page - 1) * rows:page * rows], len(sc["items"]), page, rows)
        self.send_response(200); self.send_header("Content-Type", "application/xml"); self.end_headers(); self.wfile.write(data)


@pytest.fixture
def server():
    _Handler.scenarios, _Handler.calls = {}, []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/1613000/BldRgstHubService"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / "config.env").write_text(f"DATA_GO_KR_SERVICE_KEY=\"{KEY}\"\n", encoding="utf-8")
    os.chmod(h / "config.env", 0o600)
    return h


def test_pnu_params_ops_and_url():
    assert BR.pnu_params(P1) == {"sigunguCd": "99999", "bjdongCd": "00100", "platGbCd": "0", "bun": "0001", "ji": "0000"}
    assert BR.pnu_params(PM)["platGbCd"] == "1" and BR.pnu_params(PM)["ji"] == "0002", "산(11번째 자리 2)은 대지구분 1"
    for bad in ("123", "99999001003000100000", "9999900100300010000", "99999001001x0010000"):
        with pytest.raises(CollectError):
            BR.pnu_params(bad)
    assert BR.parse_pnus([f"{P1}, {P2}", P1]) == [P1, P2], "쉼표·반복, 중복 제거"
    assert BR.parse_ops(None) == list(BR.DEFAULT_OPS) and BR.parse_ops("title,title,floor") == ["title", "floor"]
    with pytest.raises(CollectError):
        BR.parse_ops("owner")
    assert "owner" not in " ".join(BR.OPERATIONS) and not any("Ownr" in op for op, _ in BR.OPERATIONS.values()), "소유자 오퍼레이션은 없다"
    url = BR.build_url(BR.DEFAULT_ENDPOINT_BASE, "title", KEY, P1, 2, 50)
    assert url.startswith("https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo?serviceKey=abcDEF123%2B%2F%3D")
    q = parse_qs(urlsplit(url).query)
    assert q["sigunguCd"] == ["99999"] and q["bun"] == ["0001"] and q["pageNo"] == ["2"] and q["numOfRows"] == ["50"] and q["_type"] == ["xml"]
    assert "serviceKey=abc%2B" in BR.build_url(BR.DEFAULT_ENDPOINT_BASE + "/", "title", "abc%2B", P1, 1, 10), "이미 부호화된 키는 그대로, 끝의 / 는 하나로"
    with pytest.raises(CollectError):
        BR.build_url(BR.DEFAULT_ENDPOINT_BASE + "?x=1", "title", KEY, P1, 1, 10)


def test_collect_outcomes_raw_paths_and_no_key(server, home):
    _Handler.scenarios = {
        (P1, "getBrTitleInfo"): {"kind": "pages", "items": [title_item(i) for i in range(5)]},
        (P1, "getBrRecapTitleInfo"): {"kind": "pages", "items": []},
        (P2, "getBrTitleInfo"): {"kind": "short_pages", "items": [title_item(9)], "claimed_total": 7},
        (P2, "getBrRecapTitleInfo"): {"kind": "api_error"},
        (PM, "getBrTitleInfo"): {"kind": "forbidden"},
    }
    run = BR.collect_buildings(home, key=KEY, key_source="config", pnus=[P1, P2, PM], ops=["title", "recap"], endpoint_base=server, num_rows=2, sleep=lambda s: None)
    got = {(t.pnu, t.op): t for t in run.targets}
    assert got[(P1, "title")].outcome == "complete" and got[(P1, "title")].items == 5 and len(got[(P1, "title")].pages) == 3
    assert got[(P1, "recap")].outcome == "empty" and "API 실패가 아님" in got[(P1, "recap")].message
    assert got[(P2, "title")].outcome == "partial" and "페이지 누락" in got[(P2, "title")].message
    assert got[(P2, "recap")].outcome == "failed" and "api_error" in got[(P2, "recap")].message
    fb = got[(PM, "title")]
    assert fb.outcome == "failed" and fb.pages[0].http_status == 403 and "SERVICE_ACCESS_DENIED_ERROR" in fb.pages[0].error
    assert got[(PM, "recap")].outcome == "empty", "하나가 실패해도 다음 대상을 계속한다"
    assert not run.ok
    # 원본 경로: raw/br_hub/<PNU>/<키>/pNNN-<실행>.xml, 오류 본문은 가려서 .txt
    assert (home / "raw" / "br_hub" / P1 / "title" / f"p003-{run.run_id}.xml").is_file()
    err = home / fb.pages[0].error_body_path
    assert err.name == f"p001-{run.run_id}-http403.txt" and KEY not in err.read_text(encoding="utf-8") and "<redacted>" in err.read_text(encoding="utf-8")
    everything = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (home / "raw").rglob("*") if p.is_file() and p.suffix in (".json", ".txt"))
    everything += (home / "logs" / "collect.log").read_text(encoding="utf-8")
    for form in (KEY, "abcDEF123%2B%2F%3D", "abcDEF123%2b%2f%3d"):
        assert form not in everything, "기록·요약·로그·오류 본문에 키가 없다"
    rec = json.loads((home / run.run_path).read_text(encoding="utf-8"))
    assert rec["provider"] == BR.PROVIDER and {t["operation"] for t in rec["targets"]} == {"getBrTitleInfo", "getBrRecapTitleInfo"}
    assert all(t["pages"][0]["url_redacted"].count("serviceKey=<redacted>") == 1 for t in rec["targets"])
    # 요약: 오퍼레이션별 결과 수·필드 채움. 긴 글(주소)의 값은 싣지 않고 짧은 분류 값은 싣는다
    rep = json.loads((home / run.report_path).read_text(encoding="utf-8"))
    title = next(o for o in rep["operations"] if o["op"] == "title")
    assert title["items"] == 6 and title["outcomes"] == {"complete": 1, "partial": 1, "failed": 1}
    assert "distribution" not in title["fields"]["platPlc"] and title["fields"]["platPlc"]["fill_rate"] == 1.0
    assert title["fields"]["mainPurpsCdNm"]["distribution"] == {"업무시설": 3, "제2종근린생활시설": 3}
    assert title["fields"]["bldNm"]["fill_rate"] == 0.0
    assert "가상동" not in json.dumps(rep, ensure_ascii=False), "원본 행의 주소 글은 요약에 없다"
    # 재요약 (네트워크 없음): 같은 결과, 최근 실행
    again = BR.report_from_raw(home)
    assert again["run_id"] == run.run_id and [o["items"] for o in again["operations"]] == [o["items"] for o in rep["operations"]]
    assert "표제부(title, getBrTitleInfo): 항목 6건" in BR.report_text(again) and "complete" in BR.run_text(run)


def test_limits_and_paging_checks(home):
    with pytest.raises(CollectError) as e:
        BR.collect_buildings(home, key=KEY, key_source="x", pnus=[], ops=["title"])
    assert e.value.code == "no_pnu"
    many = [f"99999001001{i:04d}0000" for i in range(BR.MAX_PNUS + 1)]
    with pytest.raises(CollectError) as e:
        BR.collect_buildings(home, key=KEY, key_source="x", pnus=many, ops=["title"])
    assert e.value.code == "too_many_pnus"
    with pytest.raises(CollectError) as e:
        BR.collect_buildings(home, key=KEY, key_source="x", pnus=[P1], ops=["title"], num_rows=1000)
    assert e.value.code == "bad_paging"
    with pytest.raises(CollectError) as e:
        BR.report_from_raw(home)
    assert e.value.code == "no_run"
    assert not (home / "raw").exists(), "검사에서 걸리면 요청도 파일도 없다"


def _linked_db(home: Path) -> Path:
    path = home / "db" / "j5.sqlite3"
    d = Db.create(path, study_id="j5-synthetic-study", data_mode="synthetic")
    d.load_seed(json.loads((FIX / "assets.seed.synthetic.json").read_text(encoding="utf-8")))
    load_bundle(d, json.loads((FIX / "parcels" / "synthetic.j5parcels.json").read_text(encoding="utf-8")))
    apply_links(d, {"kind": "asset_components", "links": [{"asset_id": "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50", "pnu": P1, "effective_from": "2026-01-01", "effective_to": None, "basis": "manual"},
                                                         {"asset_id": "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a53", "pnu": P2, "effective_from": "2026-01-01", "effective_to": "2026-02-01", "basis": "manual"}]})
    d.close()
    return path


def test_cli_br_sample_and_report(server, home, capsys, monkeypatch):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    monkeypatch.delenv("DATA_GO_KR_SERVICE_KEY", raising=False)
    _Handler.scenarios = {(P1, "getBrTitleInfo"): {"kind": "pages", "items": [title_item(1)]}}
    rc = cli.main(["collect", "br-sample", "--pnu", P1, "--ops", "title,floor", "--endpoint-base", server])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert f"{P1} 표제부(title): complete · 1건 (1페이지)" in out.out and "층별개요(floor): empty" in out.out
    assert KEY not in out.out + out.err and "트래픽" in out.err and "br-report --run-id" in out.out
    assert cli.main(["collect", "br-report", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert [o["op"] for o in rep["operations"]] == ["title", "floor"] and rep["pnus"] == 1
    # 정본의 현재 연결만 대상 (끝난 연결 P2 는 빠진다). 정본은 읽기 전용으로 열어 바꾸지 않는다
    dbp = _linked_db(home)
    before = dbp.read_bytes()
    _Handler.calls = []
    rc = cli.main(["collect", "br-sample", "--linked", "--ops", "title", "--endpoint-base", server])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert "연결 1필지" in out.err and {c[1]["bun"] for c in _Handler.calls} == {"0001"}
    assert dbp.read_bytes() == before
    # 종료 코드: 잘못된 PNU·키 없음·대상 없음은 3, API 실패는 1
    assert cli.main(["collect", "br-sample", "--pnu", "123", "--endpoint-base", server]) == cli.USAGE_ERROR
    assert cli.main(["collect", "br-sample", "--endpoint-base", server]) == cli.USAGE_ERROR
    assert cli.main(["collect", "br-sample", "--pnu", P1, "--ops", "nope", "--endpoint-base", server]) == cli.USAGE_ERROR
    assert cli.main(["collect", "br-sample", "--pnu", P1, "--endpoint-base", server, "--config", str(home / "none.env")]) == cli.USAGE_ERROR
    _Handler.scenarios = {(P1, "getBrTitleInfo"): {"kind": "forbidden"}}
    assert cli.main(["collect", "br-sample", "--pnu", P1, "--ops", "title", "--endpoint-base", server]) == 1
    out = capsys.readouterr()
    assert "SERVICE_ACCESS_DENIED_ERROR" in out.out and KEY not in out.out + out.err
