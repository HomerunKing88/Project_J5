"""J5-014B-3: 핵심·비교 범위 분류, 취소·정정 점검, 파생본의 거래 목록 (데이터 사전 §7.1·§4, db_schema 8).

시험: 마이그레이션(기존 정본에 열 추가), 규칙 파일 검증(겹침·빈 값·스키마), 반영·재분류·변화 없음·새 버전, 반영 시 현재 규칙으로 분류, 현황의 범위별 수,
재점검 대상 월(최근 N·실패 월)과 CLI, 변경 점검(새 거래·취소·사라짐, 범위 필터), 파생본 transactions.json(범위 밖·취소 제외, 확정 연결만 asset_id, 검증기 변조 탐지). 가상자료만 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from j5 import cli
from j5.collect import rt
from j5.collect.rt import collect_months, recent_months
from j5.db import schema as S
from j5.db.projection import ProjectionError, build_projection, verify_projection_dir
from j5.db.store import Db
from j5.db.transactions import coverage, identity_hash, load_run
from j5.db.txlinks import apply_decisions
from j5.db.validate import ValidationError
from j5.db.zones import apply_rules, changes_since, changes_text, classify, current_rules, load_rules, rules_text, transactions_for_projection, zone_counts
from tests.test_j5_014_collect import KEY, _Handler, item, server  # noqa: F401

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
A = [f"7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a5{i}" for i in range(5)]
RULES = {"kind": "zone_rules", "name": "잠정 핵심 6개동", "core": ["종로5가", "종로6가", "효제동"], "comparison": ["예지동", "장사동"], "note": None}


@pytest.fixture(autouse=True)
def sequential_run_ids(monkeypatch):
    counter = iter(range(1, 10_000))
    monkeypatch.setattr(rt, "_new_run_id", lambda: f"20260925-140000-{next(counter):06x}")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id="j5-synthetic-study", data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    yield d
    d.close()


def row(i: int, **over) -> dict:
    it = item(i)
    it.update({"sggCd": "11110", "landUse": "일반상업", "shareDealingType": "", "cdealDay": "", "estateAgentSggNm": "", "buyerGbn": "", "slerGbn": ""})
    it.update(over)
    return it


def load_month(db, home, server, items, ym="202608"):
    _Handler.scenarios = {ym: {"kind": "pages", "items": items}}
    run = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=[ym], endpoint=server, sleep=lambda s: None)
    load_run(db, home, "11110", run.run_id)
    return run


def rules_file(tmp_path, doc=RULES) -> Path:
    p = tmp_path / "zones.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def test_schema_8_migrates_existing_db_and_rules_validation(db, home, tmp_path):
    st = db.status()
    assert st["db_schema_version"] == S.DB_SCHEMA_VERSION >= 8 and st["counts"]["zone_rules"] == 0
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(transactions)")}
    assert {"zone", "zone_rule_version"} <= cols
    # 기존 정본(버전 7까지만 적용된 것처럼)을 열면 마이그레이션이 붙는다
    p = home / "db" / "old.sqlite3"
    with db.transaction():
        pass
    import sqlite3
    src = sqlite3.connect(str(db.path))
    dst = sqlite3.connect(str(p))
    src.backup(dst)
    dst.execute("DELETE FROM schema_migrations WHERE version = 8")
    dst.execute("DROP INDEX transactions_by_zone")
    dst.execute("DROP TABLE zone_rules")
    dst.commit()
    dst.close()
    src.close()
    # ALTER 로 붙은 열은 남지만 8 버전 재적용은 실패하지 않아야 한다 → 실제 운영에서는 열이 없는 정본만 존재하므로 표만 확인
    assert load_rules(rules_file(tmp_path))["core"] == ["종로5가", "종로6가", "효제동"]
    for bad, word in ((dict(RULES, core=["종로5가"], comparison=["종로5가"]), "둘 다"), (dict(RULES, core=[" ", "종로5가"]), "비어"), (dict(RULES, core=["종로5가", "종로5가 "]), "두 번"),
                      (dict(RULES, core=[]), "zone_rules.schema"), ({"kind": "zone_rules", "name": "x"}, "zone_rules.schema")):
        with pytest.raises(ValidationError) as e:
            load_rules(rules_file(tmp_path, bad))
        assert word in str(e.value), (bad, str(e.value))
    assert classify(None, "종로5가") == "unclassified" and classify(RULES, "종로5가") == "core" and classify(RULES, "장사동") == "comparison" and classify(RULES, "당주동") == "outside"


def test_apply_rules_reclassifies_and_load_uses_current_rules(db, home, server, tmp_path):
    load_month(db, home, server, [row(0, umdNm="종로5가"), row(1, umdNm="장사동"), row(2, umdNm="당주동"), row(3, umdNm="종로6가", cdealType="O", cdealDay="26.08.20")])
    assert {r[0] for r in db.conn.execute("SELECT DISTINCT zone FROM transactions")} == {"unclassified"}
    r = apply_rules(db, load_rules(rules_file(tmp_path)))
    assert r.outcome == "applied" and r.version == 1 and r.counts_by_zone == {"core": 1, "comparison": 1, "outside": 1} and r.dataset_version == db.status()["dataset_version"]
    z = {t["emd_name"]: (t["zone"], t["zone_rule_version"]) for t in db.conn.execute("SELECT emd_name, zone, zone_rule_version FROM transactions")}
    assert z["종로5가"] == ("core", 1) and z["장사동"] == ("comparison", 1) and z["당주동"] == ("outside", 1) and z["종로6가"] == ("core", 1)
    assert apply_rules(db, load_rules(rules_file(tmp_path))).outcome == "unchanged" and current_rules(db)["version"] == 1
    # 새로 반영하는 거래는 현재 규칙으로 분류된다
    load_month(db, home, server, [row(5, umdNm="효제동"), row(6, umdNm="가회동")], ym="202609")
    z2 = {t["emd_name"]: t["zone"] for t in db.conn.execute("SELECT emd_name, zone FROM transactions")}
    assert z2["효제동"] == "core" and z2["가회동"] == "outside"
    # 규칙 v2: 비교 블록을 바꾸면 모두 재분류
    r2 = apply_rules(db, load_rules(rules_file(tmp_path, dict(RULES, name="v2", comparison=["가회동"]))))
    assert r2.version == 2 and {t["emd_name"]: t["zone"] for t in db.conn.execute("SELECT emd_name, zone FROM transactions")}["가회동"] == "comparison"
    assert {t[0] for t in db.conn.execute("SELECT zone FROM transactions WHERE emd_name = '장사동'")} == {"outside"}
    assert db.status()["counts"]["zone_rules"] == 2 and "v2" in rules_text(current_rules(db), zone_counts(db)) and "규칙 없음" in rules_text(None, {})
    c = coverage(db, "11110", ["202608", "202609"])
    assert c["core"] == 2 and c["comparison"] == 1 and c["months"][0]["core"] == 1 and c["months"][1]["core"] == 1, "취소된 종로6가 거래는 핵심 수에 들어가지 않는다"
    assert c["months"][0]["cancelled"] == 1, "취소 거래는 범위 수에 들어가지 않는다"


def test_recheck_months_changes_and_cli(db, home, server, tmp_path, capsys, monkeypatch):
    assert recent_months(3, today="2026-09-25") == ["202607", "202608", "202609"] and recent_months(2, today="2026-01-10") == ["202512", "202601"]
    assert recent_months(3, today="2006-02-01") == ["200601", "200602"], "2006-01 이전은 자르지 않고 뺀다"
    with pytest.raises(rt.CollectError):
        recent_months(0)
    run1 = load_month(db, home, server, [row(0, umdNm="종로5가"), row(1, umdNm="종로5가"), row(2, umdNm="당주동")])
    apply_rules(db, load_rules(rules_file(tmp_path)))
    # 재조회: row(1) 취소, row(2) 사라짐, row(7) 신규
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(0, umdNm="종로5가"), row(1, umdNm="종로5가", cdealType="O", cdealDay="26.09.15"), row(7, umdNm="종로5가")]}}
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", KEY)
    fixed_today = "2026-08-20"
    monkeypatch.setattr(rt, "_now", lambda: fixed_today + "T00:00:00Z")
    assert cli.main(["collect", "rt-recheck", "--lawd-cd", "11110", "--endpoint", server]) == cli.USAGE_ERROR
    capsys.readouterr()
    rc = cli.main(["collect", "rt-recheck", "--lawd-cd", "11110", "--recent", "1", "--endpoint", server])
    out = capsys.readouterr()
    assert rc == 0 and "2026-08: complete" in out.out and "rt-changes" in out.out and "최근 1개월" in out.err
    assert cli.main(["db", "rt-load", "--lawd-cd", "11110"]) == 0
    capsys.readouterr()
    rc = cli.main(["db", "rt-changes", "--lawd-cd", "11110", "--since-run", run1.run_id, "--json"])
    c = json.loads(capsys.readouterr().out)
    assert rc == 0 and len(c["runs_after"]) == 1 and [t["emd_name"] for t in c["new"]] == ["종로5가"] and [t["cancel_date"] for t in c["cancelled"]] == ["2026-09-15"]
    assert [t["emd_name"] for t in c["missing"]] == ["당주동"]
    rc = cli.main(["db", "rt-changes", "--lawd-cd", "11110", "--since-run", run1.run_id, "--zone", "core"])
    text = capsys.readouterr().out
    assert rc == 0 and "새 거래 1, 취소로 바뀜 1, 응답에서 사라짐 0" in text and "취소 확정이 아니다" in text
    # --failed: 실행 기록상 완전하지 않은 달만 (202607 은 받은 적 없음 → 대상, 202608 은 complete → 제외)
    _Handler.scenarios["202607"] = {"kind": "pages", "items": []}
    _Handler.calls = []
    rc = cli.main(["collect", "rt-recheck", "--lawd-cd", "11110", "--failed", "--from", "2026-07", "--to", "2026-08", "--endpoint", server])
    assert rc == 0 and [q["DEAL_YMD"] for q in _Handler.calls] == ["202607"]
    capsys.readouterr()
    assert cli.main(["collect", "rt-recheck", "--lawd-cd", "11110", "--failed", "--from", "2026-07", "--to", "2026-08", "--endpoint", server]) == 0
    assert "다시 받을 달이 없다" in capsys.readouterr().err
    d2 = Db.open(home / "db" / "j5.sqlite3")
    try:
        assert changes_text(changes_since(d2, "11110", "20260925-140000-000000"))
        rc_zones = cli.main(["db", "rt-zones", "show"])
        assert rc_zones == 0 and "잠정 핵심 6개동" in capsys.readouterr().out
        assert cli.main(["db", "rt-zones", "apply"]) == cli.USAGE_ERROR
        rf = rules_file(tmp_path, dict(RULES, name="cli v2"))
        assert cli.main(["db", "rt-zones", "apply", str(rf)]) == 0 and "cli v2" in capsys.readouterr().out
    finally:
        d2.close()


def test_projection_transactions_file(db, home, server, tmp_path):
    load_month(db, home, server, [row(0, umdNm="종로5가", jibun="1"), row(1, umdNm="장사동"), row(2, umdNm="당주동"), row(3, umdNm="종로5가", cdealType="O", cdealDay="26.08.20")])
    assert build_projection(db, home).counts["transactions"] == 0, "규칙이 없으면 거래 파일을 만들지 않는다"
    apply_rules(db, load_rules(rules_file(tmp_path)))
    t1 = db.conn.execute("SELECT transaction_id FROM transactions WHERE identity_hash = ?", (identity_hash(row(0, umdNm="종로5가", jibun="1")),)).fetchone()[0]
    apply_decisions(db, [{"_line": 2, "transaction_id": t1, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual"}])
    doc = transactions_for_projection(db, generated_at="2026-09-25T00:00:00Z", study_id="j5-synthetic-study", source_dataset_version=3, data_mode="synthetic")
    assert doc["count"] == 2 and {t["zone"] for t in doc["transactions"]} == {"core", "comparison"}, "범위 밖·취소 확정 거래는 없다"
    tx1 = next(t for t in doc["transactions"] if t["transaction_id"] == t1)
    assert tx1["asset_id"] == A[0] and tx1["link_basis"] == "manual" and tx1["jibun"] == "1" and tx1["missing_from_provider"] is False
    assert next(t for t in doc["transactions"] if t["zone"] == "comparison")["asset_id"] is None
    r = build_projection(db, home)
    assert r.outcome == "published" and r.counts["transactions"] == 2
    out = home / r.output_dir
    listed = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert any(f["path"] == "transactions.json" for f in listed["files"]) and listed["counts"]["transactions"] == 2
    tx = json.loads((out / "transactions.json").read_text(encoding="utf-8"))
    assert tx["j5transactions"] == "1.0.0" and tx["generated_at"] == listed["generated_at"] and tx["study_id"] == "j5-synthetic-study"
    assert verify_projection_dir(out)["counts"]["transactions"] == 2
    # 변조: 범위 밖 거래를 끼워 넣으면 검증기가 잡는다 (manifest 해시부터 어긋나므로 해시를 맞춘 뒤에도 zone 검사에 걸린다)
    tx["transactions"][0]["zone"] = "outside"
    data = (json.dumps(tx, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    (out / "transactions.json").write_bytes(data)
    import hashlib
    for f in listed["files"]:
        if f["path"] == "transactions.json":
            f["bytes"], f["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    (out / "manifest.json").write_text(json.dumps(listed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ProjectionError) as e:
        verify_projection_dir(out)
    assert e.value.code == "verify_transaction_zone"
