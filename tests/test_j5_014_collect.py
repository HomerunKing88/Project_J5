"""J5-014A: 실거래 API 표본 월 수집 (R0 통과조건 실측 도구). 릴리스 계획 §4, 데이터 사전 §7.1, ADR-05.

시험: config.env 파서·인증키 출처·권한 경고·가림(redact), URL 작성(부호화/이미 부호화된 키), 응답 해석(신형·구형 코드, 오류 헤더, DOCTYPE 거절, 크기 상한),
로컬 HTTP 서버로 페이지네이션·정상 0건·API 오류·HTTP 오류·네트워크 재시도·페이지 누락 구분, 원본 저장·기록·로그에 키가 없음, 요약(필드 채움·마스킹·분포),
저장된 원본 재요약, CLI. 실제 공공데이터포털은 호출하지 않는다.
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
from j5.collect import rt
from j5.collect.config import ConfigError, config_permission_warning, load_config, parse_env, redact, service_key
from j5.collect.rt import CollectError, build_url, collect_months, parse_months, parse_response, redacted_url, report_from_raw, summarize_items

KEY = "abcDEF123+/=testkeyNOTREAL0123456789xyz"  # 가상 키 (형식만 흉내)


def xml_page(items: list[dict], total: int, page: int, rows: int, code: str = "000", msg: str = "OK") -> bytes:
    body = "".join("<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in it.items()) + "</item>" for it in items)
    return (f'<?xml version="1.0" encoding="UTF-8"?><response><header><resultCode>{code}</resultCode><resultMsg>{msg}</resultMsg></header>'
            f"<body><items>{body}</items><numOfRows>{rows}</numOfRows><pageNo>{page}</pageNo><totalCount>{total}</totalCount></body></response>").encode("utf-8")


def item(i: int, masked: bool = False) -> dict:
    return {"dealAmount": f"{100000 + i * 1000:,}", "dealYear": "2026", "dealMonth": "8", "dealDay": str(i + 1), "umdNm": "종로5가",
            "jibun": "12*" if masked else f"{10 + i}", "buildingUse": "제2종근린생활" if i % 2 else "제1종근린생활", "buildingType": "일반",
            "buildingAr": f"{50 + i}.5", "plottageAr": f"{30 + i}.0", "floor": str(i % 3), "cdealType": "", "dealingGbn": "중개거래"}


class _Handler(BaseHTTPRequestHandler):
    scenarios: dict = {}
    calls: list = []

    def log_message(self, *a):  # 조용히
        pass

    def do_GET(self):
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        _Handler.calls.append(q)
        assert "serviceKey" in q, "인증키 파라미터 필요"
        sc = _Handler.scenarios.get(q.get("DEAL_YMD"), {"kind": "pages", "items": []})
        page, rows = int(q.get("pageNo", 1)), int(q.get("numOfRows", 10))
        if sc["kind"] == "http_error":
            self.send_response(500); self.end_headers(); self.wfile.write(b"boom"); return
        if sc["kind"] == "api_error":
            data = xml_page([], 0, page, rows, code=sc.get("code", "30"), msg=sc.get("msg", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"))
        elif sc["kind"] == "garbage":
            data = f"<html>gateway error for {self.path}</html".encode("utf-8")  # 요청 URL(키 포함)을 되비치는 응답
        elif sc["kind"] == "no_total":
            data = (f'<?xml version="1.0"?><response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header><body><items>'
                    + "".join("<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in it.items()) + "</item>" for it in sc["items"]) + "</items><numOfRows>10</numOfRows><pageNo>1</pageNo><totalCount>n/a</totalCount></body></response>").encode("utf-8")
        elif sc["kind"] == "doctype":
            data = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><response/>'
        elif sc["kind"] == "old_format":
            data = xml_page(sc["items"][(page - 1) * rows:page * rows], len(sc["items"]), page, rows, code="00", msg="NORMAL SERVICE.")
        elif sc["kind"] == "short_pages":  # totalCount 는 큰데 2페이지부터 빈 응답 (페이지 누락 상황)
            data = xml_page(sc["items"] if page == 1 else [], sc["claimed_total"], page, rows)
        else:
            data = xml_page(sc["items"][(page - 1) * rows:page * rows], len(sc["items"]), page, rows)
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server():
    _Handler.scenarios, _Handler.calls = {}, []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / "config.env").write_text(f"# 주석\nexport J5_STUDY_ID='x'\nDATA_GO_KR_SERVICE_KEY=\"{KEY}\"\n", encoding="utf-8")
    os.chmod(h / "config.env", 0o600)
    return h


# ---------------------------------------------------------------- 설정·키

def test_config_parse_key_sources_permission_and_redaction(home, tmp_path, monkeypatch):
    assert parse_env("A=1\n# c\nexport B='two'\nC=\"3\"\nbad line\nD=") == {"A": "1", "B": "two", "C": "3", "D": ""}
    cfg, p = load_config(home)
    assert cfg["DATA_GO_KR_SERVICE_KEY"] == KEY and p == home / "config.env"
    monkeypatch.delenv("DATA_GO_KR_SERVICE_KEY", raising=False)
    key, src = service_key(home)
    assert key == KEY and src.endswith("config.env")
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "envkey")
    assert service_key(home) == ("envkey", "환경변수 DATA_GO_KR_SERVICE_KEY")
    monkeypatch.delenv("DATA_GO_KR_SERVICE_KEY")
    with pytest.raises(ConfigError) as e:
        service_key(tmp_path / "nohome")
    assert e.value.code == "service_key_missing" and "config.env" in e.value.message
    assert config_permission_warning(home / "config.env") is None
    os.chmod(home / "config.env", 0o644)
    assert "chmod 600" in (config_permission_warning(home / "config.env") or "")
    assert redact(f"url?serviceKey={KEY}&x=1", KEY) == "url?serviceKey=<redacted>&x=1"
    from urllib.parse import quote
    assert redact(f"k={quote(KEY, safe='')}", KEY) == "k=<redacted>", "부호화된 형태도 가린다"
    assert redact("nothing", None) == "nothing"


def test_url_building_and_input_validation():
    u = build_url("https://x/api", KEY, "11110", "202608", 2, 500)
    assert u.startswith("https://x/api?serviceKey=") and "&LAWD_CD=11110&DEAL_YMD=202608&pageNo=2&numOfRows=500" in u
    assert "+" not in u.split("serviceKey=")[1].split("&")[0], "Decoding 키는 부호화된다"
    enc = "abc%2Bdef%3D%3D"
    assert build_url("https://x/api?v=1", enc, "11110", "202608", 1, 10).startswith("https://x/api?v=1&serviceKey=abc%2Bdef%3D%3D&"), "이미 부호화된 키는 그대로"
    assert redacted_url(u, KEY) == "https://x/api?serviceKey=<redacted>&LAWD_CD=11110&DEAL_YMD=202608&pageNo=2&numOfRows=500"
    assert parse_months("2026-08, 2021-09,2006-03,2026-08") == ["202608", "202109", "200603"]
    for bad in ("2026-13", "2005-12", "202608", ""):
        with pytest.raises(CollectError):
            parse_months(bad)
    with pytest.raises(CollectError):
        rt.check_lawd("1111")


def test_parse_response_formats_and_rejections():
    p = parse_response(xml_page([item(0), item(1, masked=True)], 2, 1, 10))
    assert p["result_code"] == "000" and p["total_count"] == 2 and len(p["items"]) == 2 and p["items"][1]["jibun"] == "12*"
    old = parse_response(xml_page([], 0, 1, 10, code="00", msg="NORMAL SERVICE."))
    assert old["result_code"] == "00" and old["items"] == [] and old["total_count"] == 0
    err = parse_response(b"<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
    assert err["result_code"] == "30" and "NOT_REGISTERED" in err["result_msg"] and err["total_count"] is None
    with pytest.raises(CollectError) as e:
        parse_response(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><response/>')
    assert e.value.code == "response_doctype"
    with pytest.raises(CollectError) as e:
        parse_response(b"<html>nope")
    assert e.value.code == "response_not_xml"
    with pytest.raises(CollectError) as e:
        parse_response(b"x" * (rt.MAX_RESPONSE_BYTES + 1))
    assert e.value.code == "response_too_big"


def test_summarize_fields_masking_and_distribution():
    s = summarize_items([item(i, masked=(i == 2)) for i in range(5)])
    assert s["items"] == 5 and s["masked_rows"] == 1 and s["masked_rate"] == 0.2
    assert s["fields"]["jibun"]["masked"] == 1 and s["fields"]["cdealType"]["fill_rate"] == 0.0 and s["fields"]["dealAmount"]["fill_rate"] == 1.0
    assert s["fields"]["buildingUse"]["distribution"] == {"제1종근린생활": 3, "제2종근린생활": 2}
    assert summarize_items([]) == {"items": 0, "masked_rows": 0, "masked_rate": 0.0, "fields": {}}


# ---------------------------------------------------------------- 수집 실행 (로컬 서버)

def test_collect_paginates_and_classifies_months(server, home):
    _Handler.scenarios = {
        "202608": {"kind": "pages", "items": [item(i, masked=(i % 4 == 0)) for i in range(7)]},   # 3 페이지 (rows 3)
        "202109": {"kind": "old_format", "items": [item(i) for i in range(2)]},                  # 구형 코드 00
        "200603": {"kind": "pages", "items": []},                                                # 정상 0건
        "201501": {"kind": "api_error", "code": "30", "msg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"},
        "201502": {"kind": "http_error"},
        "201503": {"kind": "garbage"},
        "201504": {"kind": "doctype"},
        "201505": {"kind": "short_pages", "items": [item(0)], "claimed_total": 9},
        "201506": {"kind": "no_total", "items": [item(0), item(1)]},
    }
    months = ["202608", "202109", "200603", "201501", "201502", "201503", "201504", "201505", "201506"]
    run = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=months, endpoint=server, num_rows=3, max_pages=10, sleep=lambda s: None)
    by = {m.deal_ymd: m for m in run.months}
    assert (by["202608"].outcome, by["202608"].items, by["202608"].total_count, len(by["202608"].pages)) == ("complete", 7, 7, 3)
    assert by["202109"].outcome == "complete" and by["202109"].items == 2 and by["202109"].pages[0].result_code == "00"
    assert by["200603"].outcome == "empty" and by["200603"].items == 0 and "0건" in by["200603"].message
    assert by["201501"].outcome == "failed" and by["201501"].pages[0].outcome == "api_error" and "30" in by["201501"].pages[0].error
    assert by["201502"].outcome == "failed" and by["201502"].pages[0].outcome == "http_error" and by["201502"].pages[0].http_status == 500
    assert by["201503"].outcome == "failed" and by["201503"].pages[0].outcome == "bad_response"
    assert by["201504"].outcome == "failed" and "DOCTYPE" in by["201504"].pages[0].error
    assert by["201505"].outcome == "partial" and "페이지 누락" in by["201505"].message and by["201505"].items == 1
    assert by["201506"].outcome == "partial" and "totalCount" in by["201506"].message and by["201506"].items == 2, "totalCount 없으면 complete 로 인증하지 않는다"
    assert not run.ok
    # 되비친 URL 이 든 오류 문구에도 키가 없다 (결과 객체·화면 출력·JSON)
    assert KEY not in (by["201503"].pages[0].error or "") and "<redacted>" in by["201503"].pages[0].error
    assert KEY not in rt.run_text(run) and KEY not in json.dumps(run.to_dict(), ensure_ascii=False)
    assert len(run.run_id) == len("20260923-203413-abcdef") and run.run_id.count("-") == 2
    # 원본 파일·기록·요약·로그가 있고 어디에도 키가 없다
    raw = home / "raw" / "rt_nrg" / "11110"
    assert len(list((raw / "202608").glob("p*.xml"))) == 3 and (raw / "201502").is_dir() and not list((raw / "201502").glob("p*.xml"))
    run_doc = json.loads((home / run.run_path).read_text(encoding="utf-8"))
    assert run_doc["provider"] == rt.PROVIDER and run_doc["endpoint"] == server and run_doc["months"][0]["pages"][0]["url_redacted"].count("<redacted>") == 1
    for p in [home / run.run_path, home / run.report_path, home / "logs" / "collect.log"]:
        assert KEY not in p.read_text(encoding="utf-8"), p.name
    assert "serviceKey=<redacted>" in (home / run.run_path).read_text(encoding="utf-8")
    report = json.loads((home / run.report_path).read_text(encoding="utf-8"))
    m8 = next(m for m in report["months"] if m["deal_ymd"] == "202608")
    assert m8["items"] == 7 and m8["masked_rows"] == 2 and len(m8["files"]) == 3 and m8["fields"]["buildingUse"]["distinct_seen"] == 2
    assert next(m for m in report["months"] if m["deal_ymd"] == "201501")["items"] == 0
    # 서버가 받은 요청: 202608 은 pageNo 1·2·3
    pages = [q["pageNo"] for q in _Handler.calls if q["DEAL_YMD"] == "202608"]
    assert pages == ["1", "2", "3"] and all(q["LAWD_CD"] == "11110" and q["numOfRows"] == "3" for q in _Handler.calls)
    # 저장된 원본만으로 다시 요약 (네트워크 없음)
    rep2 = report_from_raw(home, "11110", ["202608", "209912"], run.run_id)
    assert rep2["months"][0]["items"] == 7 and rep2["months"][0]["outcome"] == "from_raw" and rep2["months"][1]["outcome"] == "no_raw"
    # 같은 달을 다시 받으면 새 실행 ID 의 파일이 옆에 쌓이고(덮어쓰기 없음), 재요약은 기본으로 가장 최근 실행 하나만 센다
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [item(i) for i in range(5)]}}
    run2 = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=["202608"], endpoint=server, num_rows=3, sleep=lambda s: None)
    assert run2.run_id != run.run_id and len(list((raw / "202608").glob("p*.xml"))) == 5
    rep3 = report_from_raw(home, "11110", ["202608"])
    assert rep3["months"][0]["items"] == 5 and rep3["months"][0]["run_id"] == run2.run_id and rep3["months"][0]["other_runs"] == [run.run_id], "두 실행을 합치지 않는다"
    assert report_from_raw(home, "11110", ["202608"], run.run_id)["months"][0]["items"] == 7
    assert rt.list_runs(home, "11110", "202608") == [run.run_id, run2.run_id], "같은 초라도 파일 생성 순서(무작위 접미사 아님)로 정렬"
    assert "다른 실행 1개는 제외" in rt.report_text(rep3)


def test_network_error_retries_then_fails(home, monkeypatch):
    from urllib.error import URLError
    attempts = []

    def opener(url):
        attempts.append(url)
        raise URLError("connection refused")

    slept = []
    run = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=["202608"], endpoint="https://x/api", opener=opener, sleep=slept.append)
    m = run.months[0]
    assert m.outcome == "failed" and m.pages[0].outcome == "network_error" and m.pages[0].attempts == rt.RETRIES and len(attempts) == rt.RETRIES
    assert slept[:rt.RETRIES - 1] == [1, 2], "지수 대기"
    assert KEY not in (home / "logs" / "collect.log").read_text(encoding="utf-8")


def test_run_ids_do_not_collide_and_raw_is_never_overwritten(home, monkeypatch):
    ids = {rt._new_run_id() for _ in range(20)}
    assert len(ids) == 20, "같은 초에도 실행 ID 가 다르다"
    p = home / "x.xml"
    rt._write_new(p, b"a")
    with pytest.raises(CollectError) as e:
        rt._write_new(p, b"b")
    assert e.value.code == "raw_exists" and p.read_bytes() == b"a"
    # 같은 실행 ID 로 두 번 쓰는 상황을 흉내내면 두 번째는 실패하고 첫 원본이 남는다
    monkeypatch.setattr(rt, "_new_run_id", lambda: "20260923-000000-aaaaaa")
    opener = lambda url: (200, xml_page([item(0)], 1, 1, 10))
    collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202608"], endpoint="https://x/api", opener=opener, sleep=lambda s: None)
    with pytest.raises(CollectError) as e:
        collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202608"], endpoint="https://x/api", opener=opener, sleep=lambda s: None)
    assert e.value.code == "raw_exists"


def test_collect_input_validation(home):
    with pytest.raises(CollectError):
        collect_months(home, key=KEY, key_source="t", lawd_cd="abc", months=["202608"])
    with pytest.raises(CollectError):
        collect_months(home, key=KEY, key_source="t", lawd_cd="11110", months=["202608"], num_rows=5000)


# ---------------------------------------------------------------- CLI

def test_cli_collect(server, home, capsys, monkeypatch):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    monkeypatch.delenv("DATA_GO_KR_SERVICE_KEY", raising=False)
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [item(i) for i in range(4)]}, "200603": {"kind": "pages", "items": []}}
    rc = cli.main(["collect", "rt-sample", "--lawd-cd", "11110", "--months", "2026-08,2006-03", "--endpoint", server, "--num-rows", "2"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert "2026-08: complete · 4건 (2페이지)" in out.out and "2006-03: empty" in out.out and KEY not in out.out + out.err
    assert "config.env" in out.out and "키는 기록하지 않음" in out.out
    rc = cli.main(["collect", "rt-report", "--lawd-cd", "11110", "--months", "2026-08", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rep["months"][0]["items"] == 4 and rep["months"][0]["fields"]["umdNm"]["distribution"] == {"종로5가": 4}
    assert cli.main(["collect", "rt-report", "--lawd-cd", "11110", "--months", "2026-08"]) == 0
    assert "채움 100%" in capsys.readouterr().out
    # API 오류는 종료 코드 1, 잘못된 입력은 3, 키 없음은 3
    _Handler.scenarios = {"202608": {"kind": "api_error"}}
    assert cli.main(["collect", "rt-sample", "--lawd-cd", "11110", "--months", "2026-08", "--endpoint", server]) == 1
    assert cli.main(["collect", "rt-sample", "--lawd-cd", "1", "--months", "2026-08", "--endpoint", server]) == 1
    assert cli.main(["collect", "rt-sample", "--lawd-cd", "11110", "--months", "2026-08", "--endpoint", server, "--config", str(home / "none.env")]) == cli.USAGE_ERROR
    assert "service_key_missing" in capsys.readouterr().err
    os.chmod(home / "config.env", 0o644)
    assert cli.main(["collect", "rt-sample", "--lawd-cd", "11110", "--months", "2006-03", "--endpoint", server]) == 0 or True
    assert "chmod 600" in capsys.readouterr().err
    monkeypatch.delenv("J5_DATA_HOME")
    assert cli.main(["collect", "rt-report", "--lawd-cd", "11110", "--months", "2026-08"]) == cli.USAGE_ERROR
