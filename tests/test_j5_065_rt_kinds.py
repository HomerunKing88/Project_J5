"""J5-065: 실거래 수집 종류 확장 (상업·업무용 nrg · 단독·다가구 sh · 토지 land).

시험: 종류 등록부(제공자·기본 주소·원본 폴더), 종류마다 따로 쌓이는 원본·실행 기록·완료 월, 종류별 정규화(면적·주택유형·지목·지분·없는 값의 사유),
상업·업무용 식별 해시는 이전과 같고 다른 종류는 제공자를 넣는다, 다른 종류의 완전한 응답으로 사라짐을 적지 않는다, 폴더와 제공자가 다른 실행 기록 거절,
종류별 범위 현황·반영 대기·최근 수집, 파생본의 deal_kind·land_category·coverage.kind_months 와 게시 전 확인, CLI --kind.
단독·다가구·토지의 응답 필드 이름은 가정이다(가상 응답만 쓴다). 실제 공공데이터포털 응답 확인이 아니다.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from j5 import cli
from j5.collect import rt
from j5.collect.rt import KINDS, CollectError, collect_months, kind_of_provider, list_run_ids, months_done_on_disk, report_from_raw, rt_kind
from j5.db.ops import ops_check, ops_text, recent_collection
from j5.db.projection import ProjectionError, build_projection, verify_projection_dir
from j5.db.store import DbError
from j5.db.transactions import IDENTITY_FIELDS, coverage, coverage_rows, identity_hash, load_run, normalize, unloaded_run_ids
from j5.db.zones import apply_rules, load_rules
from tests.test_j5_014_collect import KEY, xml_page
from tests.test_j5_014b3_zones import db, home, rules_file, sequential_run_ids  # noqa: F401 (fixture)
from tests.test_j5_047_year_summary_data import tamper

ENDPOINT = "http://127.0.0.1:9/fake"
LAWD = "11110"


def opener(items, claimed=None):
    def f(url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        page, rows = int(q["pageNo"]), int(q["numOfRows"])
        return 200, xml_page(items[(page - 1) * rows:page * rows], len(items) if claimed is None else claimed, page, rows)
    return f


def fetch(home, kind, items, ym="202608", claimed=None):
    return collect_months(home, key=KEY, key_source="t", lawd_cd=LAWD, months=[ym], endpoint=ENDPOINT, opener=opener(items, claimed), sleep=lambda s: None, kind=kind)


def sh_row(i=0, **over):
    r = {"sggCd": LAWD, "umdNm": "종로5가", "jibun": f"{20 + i}", "houseType": "다가구" if i % 2 else "단독", "totalFloorAr": f"{180 + i}.5", "plottageAr": f"{120 + i}.0",
         "buildYear": "1995", "dealYear": "2026", "dealMonth": "8", "dealDay": str(i + 1), "dealAmount": f"{95000 + i * 100:,}", "cdealType": "", "cdealDay": "",
         "dealingGbn": "중개거래", "estateAgentSggNm": "서울 종로구"}
    r.update(over)
    return r


def land_row(i=0, **over):
    r = {"sggCd": LAWD, "umdNm": "종로5가", "jibun": f"{30 + i}", "jimok": "대", "landUse": "일반상업지역", "dealArea": f"{88 + i}.5", "dealYear": "2026", "dealMonth": "8",
         "dealDay": str(i + 3), "dealAmount": f"{120000 + i * 100:,}", "shareDealingType": "", "cdealType": "", "cdealDay": "", "dealingGbn": "직거래"}
    r.update(over)
    return r


def nrg_row(i=0, **over):
    r = {"sggCd": LAWD, "umdNm": "종로5가", "jibun": f"{10 + i}", "buildingUse": "제2종근린생활", "buildingType": "일반", "buildingAr": f"{50 + i}.5", "plottageAr": f"{30 + i}.0",
         "buildYear": "2001", "floor": "1", "landUse": "일반상업", "shareDealingType": "", "dealYear": "2026", "dealMonth": "8", "dealDay": str(i + 5),
         "dealAmount": f"{100000 + i * 1000:,}", "cdealType": "", "cdealDay": "", "dealingGbn": "중개거래"}
    r.update(over)
    return r


def test_kind_registry_and_defaults():
    assert list(KINDS) == ["nrg", "sh", "land"] and rt_kind(None).name == "nrg" and rt_kind("land") is KINDS["land"]
    assert rt.PROVIDER == "data.go.kr:RTMSDataSvcNrgTrade" and rt.RAW_DIR.as_posix() == "raw/rt_nrg", "기존 상수는 상업·업무용 그대로"
    assert rt.DEFAULT_ENDPOINT == "https://apis.data.go.kr/1613000/RTMSDataSvcNrgTrade/getRTMSDataSvcNrgTrade"
    assert KINDS["sh"].endpoint == "https://apis.data.go.kr/1613000/RTMSDataSvcSHTrade/getRTMSDataSvcSHTrade" and KINDS["sh"].raw_dir.as_posix() == "raw/rt_sh"
    assert KINDS["land"].provider == "data.go.kr:RTMSDataSvcLandTrade" and KINDS["land"].raw_dir.as_posix() == "raw/rt_land"
    assert kind_of_provider("data.go.kr:RTMSDataSvcSHTrade").name == "sh" and kind_of_provider("x") is None
    with pytest.raises(CollectError) as e:
        rt_kind("apt")
    assert e.value.code == "bad_kind"


def test_kinds_keep_separate_raw_folders_and_done_months(home):
    s = fetch(home, "sh", [sh_row(0), sh_row(1)])
    n = fetch(home, None, [nrg_row(0)], ym="202607")
    assert s.provider == KINDS["sh"].provider and s.run_path.startswith("raw/rt_sh/11110/") and n.run_path.startswith("raw/rt_nrg/11110/")
    assert list_run_ids(home, LAWD, "sh") == [s.run_id] and list_run_ids(home, LAWD) == [n.run_id]
    assert months_done_on_disk(home, LAWD, "sh") == {"202608": "complete"} and months_done_on_disk(home, LAWD) == {"202607": "complete"}, "다른 종류의 완료 월을 건너뛰지 않는다"
    rep = report_from_raw(home, LAWD, ["202608"], kind="sh")
    assert rep["kind"] == "sh" and rep["provider"] == KINDS["sh"].provider and rep["months"][0]["items"] == 2 and "houseType" in rep["months"][0]["fields"]
    assert "단독·다가구 매매" in rt.run_text(s) and "단독·다가구 매매" in rt.report_text(rep)
    assert json.loads((home / s.report_path).read_text(encoding="utf-8"))["kind"] == "sh"


def test_normalize_by_kind_and_reasons():
    sh = normalize(sh_row(1), "sh")
    assert (sh["building_area_m2"], sh["plottage_area_m2"], sh["build_year"], sh["building_use_raw"], sh["building_kind"]) == (181.5, 121.0, 1995, "다가구", "general")
    assert sh["amount_krw"] == 951_000_000 and sh["deal_date"] == "2026-08-02" and sh["scope"] == "unclear" and json.loads(sh["missing_reasons_json"]) == {}
    odd = normalize(sh_row(0, houseType="", totalFloorAr=""), "sh")
    assert odd["building_kind"] == "unknown" and odd["building_area_m2"] is None and json.loads(odd["missing_reasons_json"]) == {"totalFloorAr": "not_provided"}
    land = normalize(land_row(0), "land")
    assert (land["plottage_area_m2"], land["building_area_m2"], land["build_year"], land["land_use_raw"], land["building_use_raw"]) == (88.5, None, None, "일반상업지역", None)
    assert land["scope"] == "land_only" and land["building_kind"] == "unknown"
    assert json.loads(land["missing_reasons_json"]) == {"buildingAr": "not_applicable", "buildYear": "not_applicable"}, "토지 거래에 없는 값은 null 과 사유"
    share = normalize(land_row(0, shareDealingType="지분", dealArea="-1"), "land")
    assert share["scope"] == "partial_share" and share["share_deal"] == 1 and share["plottage_area_m2"] is None and json.loads(share["missing_reasons_json"])["dealArea"] == "unparsable"
    # 상업·업무용은 이전과 같다 (기본값)
    assert normalize(nrg_row(0)) == normalize(nrg_row(0), "nrg") and normalize(nrg_row(0))["building_area_m2"] == 50.5


def test_identity_hash_nrg_unchanged_other_kinds_include_provider():
    c = {**nrg_row(0), **land_row(0), **sh_row(0)}
    legacy = hashlib.sha256(json.dumps({k: c.get(k, "") for k in IDENTITY_FIELDS}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert identity_hash(c) == identity_hash(c, "nrg") == legacy, "이미 정본에 있는 상업·업무용 거래의 해시가 바뀌지 않는다"
    assert len({identity_hash(c, "nrg"), identity_hash(c, "sh"), identity_hash(c, "land")}) == 3
    assert identity_hash(land_row(0), "land") == identity_hash(land_row(0, cdealType="O", dealingGbn="중개거래"), "land"), "가변 필드는 식별에 들지 않는다"
    assert identity_hash(land_row(0), "land") != identity_hash(land_row(0, jimok="전"), "land")


def test_load_by_kind_and_isolation(db, home):
    s = fetch(home, "sh", [sh_row(0), sh_row(1)])
    r = load_run(db, home, LAWD, s.run_id, "sh")
    assert r.outcome == "applied" and r.kind == "sh" and r.transactions_new == 2 and "단독·다가구 매매" in r.to_text() and r.to_dict()["kind"] == "sh"
    with pytest.raises(DbError) as e:
        load_run(db, home, LAWD, s.run_id)
    assert e.value.code == "run_missing", "상업·업무용 폴더에는 그 실행이 없다"
    # 토지: 오래된 실행 A1(행 2)·상업·업무용 빈 달 B(완전)·토지 부분 실행 A3(행 1). A3, B, A1 순으로 반영한다
    a1 = fetch(home, "land", [land_row(0), land_row(1)])
    b = fetch(home, None, [])
    a3 = fetch(home, "land", [land_row(0)], claimed=5)
    assert (a1.months[0].outcome, b.months[0].outcome, a3.months[0].outcome) == ("complete", "empty", "partial")
    load_run(db, home, LAWD, a3.run_id, "land")
    rb = load_run(db, home, LAWD, b.run_id)
    assert rb.transactions_missing == 0, "상업·업무용의 빈 달이 토지·단독 거래를 사라짐으로 적지 않는다"
    ra1 = load_run(db, home, LAWD, a1.run_id, "land")
    assert ra1.transactions_new == 1 and ra1.transactions_missing == 0, "그 뒤 완전한 토지 실행이 없으면 사라짐이 아니다 (다른 종류의 완전한 실행은 보지 않는다)"
    rows = {t["provider"]: t for t in db.conn.execute("SELECT * FROM transactions WHERE missing_since_run_id IS NOT NULL")}
    assert rows == {}
    by = {r_[0]: r_[1] for r_ in db.conn.execute("SELECT provider, COUNT(*) FROM transactions GROUP BY provider")}
    assert by == {KINDS["sh"].provider: 2, KINDS["land"].provider: 2}
    land = db.conn.execute("SELECT * FROM transactions WHERE provider = ? ORDER BY jibun_raw", (KINDS["land"].provider,)).fetchone()
    assert (land["plottage_area_m2"], land["building_area_m2"], land["scope"], land["land_use_raw"]) == (88.5, None, "land_only", "일반상업지역")
    assert unloaded_run_ids(db, home, LAWD, "land") == [] and unloaded_run_ids(db, home, LAWD) == [] and unloaded_run_ids(db, home, LAWD, "sh") == []
    c = coverage(db, LAWD, ["202608"], "land")
    assert c["kind"] == "land" and c["months"][0]["collection"] == "complete" and c["months"][0]["runs"] == 2 and c["transactions"] == 2
    cn = coverage(db, LAWD, ["202608"])
    assert cn["kind"] == "nrg" and cn["months"][0]["collection"] == "empty" and cn["transactions"] == 0, "기본 범위 현황은 상업·업무용만"
    assert coverage_rows(db) == [{"lawd_cd": LAWD, "year": 2026, "months_complete": 1, "months_any": 1, "kind_months": {"nrg": 1, "sh": 1, "land": 1}, "kind_months_any": {"nrg": 1, "sh": 1, "land": 1}}]
    # 리뷰 반영(PR #112): 상업·업무용만 더 받은 해는 종류마다 개월이 다르게 남는다 (모든 종류를 다 받은 해로 보이지 않는다)
    load_run(db, home, LAWD, fetch(home, None, [], ym="202607").run_id)
    assert coverage_rows(db) == [{"lawd_cd": LAWD, "year": 2026, "months_complete": 2, "months_any": 2, "kind_months": {"nrg": 2, "sh": 1, "land": 1}, "kind_months_any": {"nrg": 2, "sh": 1, "land": 1}}]


def test_run_record_in_wrong_kind_folder_is_rejected(db, home):
    s = fetch(home, "sh", [sh_row(0)])
    src = home / s.run_path
    dest = home / "raw" / "rt_land" / LAWD / src.name
    dest.parent.mkdir(parents=True)
    dest.write_bytes(src.read_bytes())
    before = db.status()["counts"]
    with pytest.raises(DbError) as e:
        load_run(db, home, LAWD, s.run_id, "land")
    assert e.value.code == "run_invalid" and "provider" in e.value.message and db.status()["counts"] == before


def test_ops_pending_and_recent_by_kind(db, home):
    from datetime import datetime, timezone
    land = fetch(home, "land", [land_row(0)])
    r = ops_check(home)
    assert r["collect_pending"]["rt_land"] == {LAWD: 1} and "rt" not in r["collect_pending"], "원본 폴더가 없는 종류는 싣지 않는다"
    act = next(a for a in r["actions"] if a["code"] == "rt_unloaded")
    assert "토지 매매 시군구 11110 1건" in act["text"] and "j5 db rt-load --kind land --lawd-cd 11110" in act["text"]
    assert "반영 대기 수집: 토지 매매 시군구 11110 1건" in ops_text(r)
    load_run(db, home, LAWD, land.run_id, "land")
    load_run(db, home, LAWD, fetch(home, None, [], ym="202606").run_id)
    rc = recent_collection(db, datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert [(c["kind"], c["latest_complete_ym"], c["months_behind"]) for c in rc] == [("nrg", "202606", 3), ("land", "202608", 1)], "종류마다 따로 본다"
    r2 = ops_check(home)
    assert not any(a["code"] == "rt_unloaded" for a in r2["actions"])
    lines = ops_text(r2)
    assert "최근 거래 수집: 토지 매매 · 시군구 11110" in lines and "최근 거래 수집: 시군구 11110" in lines


def test_projection_exports_kind_and_verifier(db, home, tmp_path):
    apply_rules(db, load_rules(rules_file(tmp_path)))
    for kind, rows in (("land", [land_row(0), land_row(1, jimok="")]), ("sh", [sh_row(0)]), (None, [nrg_row(0)])):
        load_run(db, home, LAWD, fetch(home, kind, rows).run_id, kind)
    r = build_projection(db, home)
    assert r.outcome == "published", r.findings
    out = home / r.output_dir
    tx = json.loads((out / "transactions.json").read_text(encoding="utf-8"))
    kinds = sorted(((t["deal_kind"], t["land_category"], t["building_use"]) for t in tx["transactions"]), key=lambda x: tuple(v or "" for v in x))
    assert kinds == [("land", None, None), ("land", "대", None), ("nrg", None, "제2종근린생활"), ("sh", None, "단독")]
    assert tx["coverage"] == [{"lawd_cd": LAWD, "year": 2026, "months_complete": 1, "months_any": 1, "kind_months": {"nrg": 1, "sh": 1, "land": 1}, "kind_months_any": {"nrg": 1, "sh": 1, "land": 1}}]
    land = next(t for t in tx["transactions"] if t["land_category"] == "대")
    assert land["building_area_m2"] is None and land["missing_reasons"] == {"buildingAr": "not_applicable", "buildYear": "not_applicable"} and land["scope"] == "land_only"
    verify_projection_dir(out)
    saved = {n: (out / n).read_bytes() for n in ("transactions.json", "manifest.json")}
    for change, code in ((lambda t: t["transactions"][0].update(deal_kind="apt"), "verify_transaction_kind"),
                         (lambda t: t["coverage"][0].update(kind_months={"apt": 1}), "verify_transactions_coverage"),
                         (lambda t: t["coverage"][0].update(kind_months={"nrg": 2}), "verify_transactions_coverage"),
                         (lambda t: t["coverage"][0].update(kind_months={"nrg": 0}), "verify_transactions_coverage"),
                         (lambda t: t["coverage"][0].update(kind_months=["nrg"]), "verify_transactions_coverage"),
                         # J5-073: 종류별 시도 개월은 그 종류의 완전 개월 이상, 시도 개월 이하
                         (lambda t: t["coverage"][0].update(kind_months_any={"apt": 1}), "verify_transactions_coverage"),
                         (lambda t: t["coverage"][0].update(kind_months_any={"nrg": 2, "sh": 1, "land": 1}), "verify_transactions_coverage"),
                         (lambda t: t["coverage"][0].update(kind_months_any={"nrg": 1, "sh": 1}), "verify_transactions_coverage"),
                         (lambda t: t["coverage"][0].update(kind_months_any={"nrg": 0, "sh": 1, "land": 1}), "verify_transactions_coverage"),
                         (lambda t: t["coverage"][0].update(kind_months_any=[]), "verify_transactions_coverage"),
                         # J5-075 (리뷰 반영 PR #120): 칸이 있는데 null 이면 폰처럼 거절한다 (칸이 없는 이전 형식은 받는다)
                         (lambda t: t["coverage"][0].update(kind_months=None), "verify_transactions_coverage"),
                         (lambda t: t["coverage"][0].update(kind_months_any=None), "verify_transactions_coverage"),
                         (lambda t: t["transactions"][0].update(land_category=3), "verify_transaction_kind"),
                         (lambda t: t["transactions"][0].update(deal_kind=["nrg"]), "verify_transaction_kind")):
        tamper(out, change)
        with pytest.raises(ProjectionError) as e:
            verify_projection_dir(out)
        assert e.value.code == code
        for n, data in saved.items():
            (out / n).write_bytes(data)
    tamper(out, lambda t: [x.pop("deal_kind") for x in t["transactions"]] and [(c.pop("kind_months"), c.pop("kind_months_any")) for c in t["coverage"]])
    verify_projection_dir(out)  # 이전 형식(종류 없음)도 받는다


def test_cli_kind_option(db, home, monkeypatch, capsys):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", KEY)
    calls = []

    def fake(url):
        calls.append(url)
        return opener([land_row(0), land_row(1)])(url)
    monkeypatch.setattr(rt, "_fetch_once", fake)
    rc = cli.main(["collect", "rt-fetch", "--kind", "land", "--lawd-cd", LAWD, "--from", "2026-08", "--to", "2026-08"])
    out = capsys.readouterr()
    assert rc == 0 and "토지 매매" in out.err and "j5 db rt-load --kind land --lawd-cd 11110" in out.out and KEY not in out.out + out.err
    assert calls and all("/RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade?" in u for u in calls), "엔드포인트를 주지 않으면 종류의 기본 주소"
    assert cli.main(["collect", "rt-fetch", "--kind", "land", "--lawd-cd", LAWD, "--from", "2026-08", "--to", "2026-08"]) == 0
    assert "모두 이미" in capsys.readouterr().err
    assert cli.main(["collect", "rt-report", "--kind", "land", "--lawd-cd", LAWD, "--months", "2026-08", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["months"][0]["items"] == 2
    db.close()
    assert cli.main(["db", "rt-load", "--lawd-cd", LAWD]) == 0 and "raw/rt_nrg/11110" in capsys.readouterr().err, "기본 종류에는 반영할 실행이 없다"
    assert cli.main(["db", "rt-load", "--kind", "land", "--lawd-cd", LAWD]) == 1
    assert "data_mode_mismatch" in capsys.readouterr().err, "실제 주소로 받은 토지 응답도 가상 정본에 넣지 않는다"
    assert cli.main(["collect", "rt-fetch", "--kind", "land", "--lawd-cd", LAWD, "--from", "2026-09", "--to", "2026-09", "--endpoint", ENDPOINT]) == 0
    capsys.readouterr()
    assert cli.main(["db", "rt-load", "--kind", "land", "--lawd-cd", LAWD, "--run-id", list_run_ids(home, LAWD, "land")[-1]]) == 0
    assert "토지 매매: 반영됨" in capsys.readouterr().out
    assert cli.main(["db", "rt-coverage", "--kind", "land", "--lawd-cd", LAWD, "--from", "2026-08", "--to", "2026-09"]) == 0
    text = capsys.readouterr().out
    assert "거래 수집 현황 토지 매매" in text and "2026-09: complete" in text and "rt-fetch --kind land" in text
    assert cli.main(["db", "rt-load", "--kind", "apt", "--lawd-cd", LAWD]) != 0
    assert "invalid choice: 'apt'" in capsys.readouterr().err
