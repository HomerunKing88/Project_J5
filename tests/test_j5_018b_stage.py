"""J5-018B: 계약 직전·잔금 직전 재확인과 비교자료 내보내기 (R7). 데이터 사전 §11 ("권리·규제·임대차 확인은 계약 직전과 잔금 직전 다시 점검한다. 새로운 위험을 확인하면 준비 상태를
철회하는 이력을 남긴다"), 릴리스 계획 §9 ("계약 전·잔금 전 재확인 작업과 비교자료 내보내기"), ADR-07, db_schema 14.

시험: 버전 13 정본을 열 때 재확인 표 생성·보존, 입력 검증(필수 항목·중복·confirmed 근거·outcome 일관성·재확인일·검토 기록 없음·근거 문서), 흐름(승인 → 계약 직전 이상 없음 → 준비 유지
→ 잔금 직전 문제 확인 → 철회 이력·판정 차단 → 새 검토로 재승인), 불변, 비교자료 내보내기(CSV 행·JSON 묶음·README·덮어쓰지 않음·대상 지정), CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.cases import CSV_COLUMNS, case_bundle, export_cases
from j5.db.plans import apply_plan_input
from j5.db.readiness import RECHECK_PREFIX, apply_case_input, apply_stage_recheck, approve, current_case, evaluate, load_stage_recheck_input, readiness_text
from j5.db.store import Db
from j5.db.validate import ValidationError
from tests.test_j5_015a_judgment import A, DOC, SEED_PATH, write
from tests.test_j5_016c_plan_records import financing
from tests.test_j5_018a_readiness import CHECK_KEYS, TODAY, _plans, case, check

pytest.importorskip("jsonschema")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id="j5-synthetic-study", data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    with d.transaction():
        d.add_source_document({"document_id": DOC, "document_kind": "manual_entry", "title": "가상 근거 메모", "collected_at": "2026-09-01T10:00:00+09:00"})
    ticks = iter(range(1, 3600))

    def clock() -> str:
        n = next(ticks)
        return f"2026-09-26T00:{n // 60:02d}:{n % 60:02d}Z"
    d.now = clock
    yield d
    d.close()


def item(key, result="confirmed", evidence=(DOC,), note=None) -> dict:
    return {"key": key, "result": result, "evidence_ids": list(evidence), "note": note}


def recheck(stage="pre_contract", on=TODAY, outcome="cleared", items=None, issues=(), **over) -> dict:
    doc = {"kind": "stage_recheck", "asset_id": A[0], "stage": stage, "reviewed_on": on, "outcome": outcome,
           "items": items if items is not None else [item("rights_tenancy"), item("tax_legal")], "issues": list(issues), "note": None}
    doc.update(over)
    return doc


def _ready_case(db) -> dict:
    refs = _plans(db)
    apply_case_input(db, case(checks=[check(k) for k in CHECK_KEYS], refs=refs))
    return refs


def test_migration_14_adds_rechecks_table(home, monkeypatch):
    monkeypatch.setattr(S, "MIGRATIONS", S.MIGRATIONS[:13])
    monkeypatch.setattr(S, "DB_SCHEMA_VERSION", 13)
    path = home / "db" / "j5.sqlite3"
    d = Db.create(path, study_id="j5-synthetic-study", data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    with d.transaction():
        d.add_source_document({"document_id": DOC, "document_kind": "manual_entry", "title": "가상 근거 메모", "collected_at": "2026-09-01T10:00:00+09:00"})
    refs = _plans(d)
    r = apply_case_input(d, case(refs=refs))
    approve(d, A[0], TODAY)
    before = [dict(x) for x in d.conn.execute("SELECT * FROM records ORDER BY record_id")]
    assert d.schema_version() == 13 and not d._has_table("readiness_rechecks") and evaluate(d, A[0], today=TODAY)["stage_rechecks"] == []
    d.close()
    monkeypatch.undo()
    d2 = Db.open(path)
    try:
        st = d2.status()
        assert st["ok"] and st["db_schema_version"] == S.DB_SCHEMA_VERSION >= 14 and d2._has_table("readiness_rechecks")
        assert [dict(x) for x in d2.conn.execute("SELECT * FROM records ORDER BY record_id")] == before
        assert d2.conn.execute("SELECT COUNT(*) FROM readiness_decisions").fetchone()[0] == 1 and d2.conn.execute("SELECT tracking_status FROM assets WHERE asset_id = ?", (A[0],)).fetchone()[0] == "purchase_ready"
        rr = apply_stage_recheck(d2, recheck())
        assert rr["record_id"] == r["record_id"] and rr["withdrawn"] is None
    finally:
        d2.close()
    assert 14 not in S.FK_OFF_MIGRATIONS and "readiness_rechecks" in S.MIGRATION_0014


def test_stage_recheck_validation(db, tmp_path):
    with pytest.raises(ValidationError) as e:
        apply_stage_recheck(db, recheck())
    assert "검토 기록이 없다" in str(e.value)
    _ready_case(db)
    with pytest.raises(ValidationError):
        load_stage_recheck_input(write(tmp_path, recheck(items=[item("rights_tenancy")])))  # tax_legal 없음
    with pytest.raises(ValidationError):
        load_stage_recheck_input(write(tmp_path, recheck(items=[item("rights_tenancy", evidence=()), item("tax_legal")])))  # confirmed 근거 없음
    with pytest.raises(ValidationError) as e:
        load_stage_recheck_input(write(tmp_path, recheck(items=[item("rights_tenancy"), item("tax_legal"), item("tax_legal")])))
    assert "중복" in str(e.value)
    with pytest.raises(ValidationError) as e:
        load_stage_recheck_input(write(tmp_path, recheck(items=[item("rights_tenancy", result="changed", evidence=()), item("tax_legal")])))
    assert "issues_found 여야" in str(e.value)
    with pytest.raises(ValidationError) as e:
        load_stage_recheck_input(write(tmp_path, recheck(outcome="issues_found")))
    assert "하나 이상" in str(e.value)
    with pytest.raises(ValidationError):
        load_stage_recheck_input(write(tmp_path, recheck(on="2026-02-30")))
    ok = load_stage_recheck_input(write(tmp_path, recheck(outcome="issues_found", issues=["임대차 명세에 없는 임차인 확인"])))
    assert ok["outcome"] == "issues_found"
    with pytest.raises(ValidationError) as e:
        apply_stage_recheck(db, recheck(on="2026-09-01"))
    assert "검토일" in str(e.value)
    with pytest.raises(ValidationError) as e:
        apply_stage_recheck(db, recheck(items=[item("rights_tenancy", evidence=("2f5d7c9a-1b3e-4d6f-8a0c-2e4f6a8b0c99",)), item("tax_legal")]))
    assert "근거 문서" in str(e.value)


def test_stage_flow_withdraws_on_issues_and_blocks_readiness(db):
    refs = _ready_case(db)
    approve(db, A[0], TODAY)
    # 계약 직전: 이상 없음 → 준비 유지, 판정에 재확인 표시
    r1 = apply_stage_recheck(db, recheck())
    assert r1["outcome"] == "cleared" and r1["withdrawn"] is None and r1["tracking_status"] == "purchase_ready"
    e = evaluate(db, A[0], today=TODAY)
    assert e["ready"] and not e["release_required"] and [st["stage"] for st in e["stage_rechecks"]] == ["pre_contract"]
    assert "재확인 [계약 직전]" in readiness_text(e) and "이상 없음" in readiness_text(e)
    # purchase_ready 가 아닌 물건의 문제 확인은 철회할 것이 없다
    refs_b = None
    # 잔금 직전: 임대차 변경 확인 → 철회 이력(사유 접두어)·판정 차단
    r2 = apply_stage_recheck(db, recheck(stage="pre_settlement", outcome="issues_found", items=[item("rights_tenancy", result="changed", evidence=(DOC,), note="새 임차인"), item("tax_legal")],
                                         issues=["임대차 명세에 없는 임차인 확인"]))
    assert r2["withdrawn"] is not None and r2["withdrawn"]["reason"].startswith(RECHECK_PREFIX + "잔금 직전") and "권리·임대차 변경 확인" in r2["withdrawn"]["reason"]
    assert db.conn.execute("SELECT tracking_status FROM assets WHERE asset_id = ?", (A[0],)).fetchone()[0] == "detailed_review"
    e = evaluate(db, A[0], today=TODAY)
    assert not e["ready"] and any("잔금 직전 재확인에서 문제 확인" in b for b in e["blockers"]) and len(e["stage_rechecks"]) == 2
    rows = [tuple(r) for r in db.conn.execute("SELECT decision, previous_status, new_status FROM readiness_decisions ORDER BY recorded_at, rowid")]
    assert rows == [("approve", "unreviewed", "purchase_ready"), ("withdraw", "purchase_ready", "detailed_review")]
    snap = json.loads(db.conn.execute("SELECT evaluation_json FROM readiness_decisions ORDER BY recorded_at DESC, rowid DESC LIMIT 1").fetchone()[0])
    assert any(st["outcome"] == "issues_found" for st in snap["stage_rechecks"])
    with pytest.raises(ValidationError):
        approve(db, A[0], TODAY)
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.conn.execute("UPDATE readiness_rechecks SET outcome = 'cleared'")
    # 새 검토 기록(변경 반영)이면 옛 재확인은 옛 기록의 것이라 판정을 막지 않는다 → 재승인 → 새 재확인
    cur = current_case(db, A[0])
    apply_case_input(db, case(on=TODAY, supersedes_id=cur["record_id"], checks=[check(k) for k in CHECK_KEYS], refs=refs))
    e = evaluate(db, A[0], today=TODAY)
    assert e["ready"] and e["stage_rechecks"] == []
    approve(db, A[0], TODAY)
    r3 = apply_stage_recheck(db, recheck(stage="pre_settlement"))
    assert r3["withdrawn"] is None and evaluate(db, A[0], today=TODAY)["ready"]
    # purchase_ready 가 아닌 물건에서 문제 확인은 기록만 남긴다
    apply_plan_input(db, financing(on=TODAY))  # 변경 → 자동 철회 대상
    from j5.db.readiness import enforce_release
    assert enforce_release(db, A[0], today=TODAY) is not None
    r4 = apply_stage_recheck(db, recheck(stage="pre_settlement", outcome="issues_found", issues=["추가 문제"]))
    assert r4["withdrawn"] is None and r4["tracking_status"] == "detailed_review"


def test_case_export_csv_json_readme_no_overwrite(db, home, tmp_path):
    refs = _ready_case(db)
    approve(db, A[0], TODAY)
    apply_stage_recheck(db, recheck())
    b = case_bundle(db, A[0], today=TODAY)
    assert b["asset"]["tracking_status"] == "purchase_ready" and b["case"]["record_id"] and b["evaluation"]["ready"]
    assert b["target_price"]["record_id"] == refs["target_price_id"] and b["financing_plan"]["payload"]["equity_result"]["result_krw"] == 1_700_000_000
    assert b["regulation_review"]["payload"]["applied_far_pct"] == 600 and len(b["decisions"]) == 1 and b["plans"]["label"]
    r = export_cases(db, home, today=TODAY)
    dest = home / r["dest"]
    assert r["count"] == 1 and dest.parent == home / "exports" / "private" / "cases" and {p.name for p in dest.iterdir()} == {"cases.json", "cases.csv", "README.txt"}
    with open(dest / "cases.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and list(rows[0].keys()) == list(CSV_COLUMNS)
    row = rows[0]
    assert row["asset_id"] == A[0] and row["tracking_status"] == "purchase_ready" and row["strategy"] == "보유 후 리모델링" and row["gross_contract_krw"] == "4000000000"
    assert row["target_price_krw"] == "1500000000" and row["required_equity_krw"] == "1700000000" and row["max_required_equity_krw"] == "2100000000" and row["far_headroom_m2"] == "360.0"
    assert row["regulation_status"] == "효력 확인" and row["checks_ok"] == "8" and row["checks_total"] == "8" and row["ready"] == "True" and row["pre_contract_recheck"].endswith("cleared")
    assert row["pre_settlement_recheck"] == "" and row["last_decision"] == "approve" and row["decisions_count"] == "1" and row["dataset_version"] == str(db.status()["dataset_version"])
    j = json.loads((dest / "cases.json").read_text(encoding="utf-8"))
    assert j["format"] == "j5cases" and j["count"] == 1 and j["cases"][0]["asset"]["asset_id"] == A[0] and j["cases"][0]["evaluation"]["stage_rechecks"][0]["stage"] == "pre_contract"
    assert "저장소·공개 배포에 넣지 않는다" in (dest / "README.txt").read_text(encoding="utf-8")
    # 두 번째 실행은 새 폴더. 대상 지정(검토 없는 물건도 묶음은 만든다: 검토 없음 표시)
    r2 = export_cases(db, home, asset_ids=[A[0], A[1]], out_root=tmp_path / "ext", today=TODAY)
    assert r2["count"] == 2 and Path(r2["dest"]).is_absolute() and Path(r2["dest"]) != dest
    with open(Path(r2["dest"]) / "cases.csv", encoding="utf-8", newline="") as f:
        rows2 = list(csv.DictReader(f))
    assert rows2[1]["asset_id"] == A[1] and rows2[1]["case_record_id"] == "" and rows2[1]["ready"] == "False"
    with pytest.raises(ValidationError):
        export_cases(db, home, asset_ids=["7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"])


def test_cli_case_recheck_and_export(db, tmp_path, capsys, monkeypatch):
    refs = _ready_case(db)
    approve(db, A[0], TODAY)
    db.close()
    home = tmp_path / "home"
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "case-recheck", str(write(tmp_path, recheck(), "rc.json"))]) == 0
    assert "재확인 기록 [계약 직전]" in capsys.readouterr().out
    assert cli.main(["db", "case-export", "--as-of", TODAY]) == 0
    out = capsys.readouterr().out
    assert "비교자료 1건" in out and "cases.csv" in out and "준비 완료" in out
    assert cli.main(["db", "case-recheck", str(write(tmp_path, recheck(stage="pre_settlement", outcome="issues_found", issues=["권리 변동 확인"]), "rc2.json")), "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["withdrawn"]["reason"].startswith(RECHECK_PREFIX) and j["tracking_status"] == "detailed_review"
    assert cli.main(["db", "case-recheck", str(write(tmp_path, recheck(items=[item("rights_tenancy")]), "bad.json"))]) == 1
    assert "입력 거절" in capsys.readouterr().err
    assert cli.main(["db", "case-export", "--asset", A[0], "--out", str(tmp_path / "out"), "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["count"] == 1 and j["summary"][0]["ready"] is False and (Path(j["dest"]) / "cases.csv").is_file()
    monkeypatch.delenv("J5_DATA_HOME")
    assert cli.main(["db", "--db", str(home / "db" / "j5.sqlite3"), "case-export"]) == 3
