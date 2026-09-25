"""J5-016C: 규제 검토·개발안·자금안 기록 종류 (R5). 데이터 사전 §2·§9·§10, 릴리스 계획 §8, db_schema 12 (표 재작성, ADR-14).

시험: 버전 11 정본(기록·근거·첨부·재확인 있음)을 열 때 표 재작성으로 새 종류가 허용되고 모두 보존되는지, 마이그레이션 9 문구가 고정됐는지, 입력 검증(스키마·참조 기록·수정 체인·근거 문서),
저장 시점 계산 스냅샷(여유면적·필요자기자본·최대 현금, 미확인이면 null), 현황 보기, 파생본 포함, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from j5 import cli
from j5.calc import CALCULATION_VERSION
from j5.db import schema as S
from j5.db.importer import import_package
from j5.db.judgment import apply_record_input
from j5.db.plans import apply_plan_input, load_plan_input, plan_overview, plan_overview_text
from j5.db.projection import build_projection
from j5.db.recheck import apply_recheck_input
from j5.db.store import Db
from j5.db.validate import ValidationError
from tests.test_j5_015a_judgment import A, DOC, SEED_PATH, target, write
from tests.test_j5_015c_recheck import recheck

FIX = Path(__file__).resolve().parent / "fixtures" / "calc"
E = 100_000_000


def calc(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


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
    yield d
    d.close()


def regulation(on="2026-09-01", **over) -> dict:
    doc = {"kind": "plan_record", "asset_id": A[0], "record_type": "regulation_review", "source_kind": "official_fact", "observed_at": on, "supersedes_id": None,
           "evidence": [{"document_id": DOC, "verification_status": "verified", "locator": "가상 고시문 p.1"}],
           "payload": {"issuing_agency": "가상시", "notice_number": "가상시 고시 제2026-1호", "document_stage": "decision_notice", "announced_on": "2026-01-10", "effective_on": "2026-01-10", "end_on": None,
                       "zoning": "일반상업", "applied_far_pct": 600, "regulation_version": "가상 일반상업 600% (2026-01 가상 고시)", "end_date_confirmed": True,
                       "constraints": [{"kind": "건축선 후퇴", "area_m2": 40, "basis": "가상", "excluded_from_denominator": "false", "reviewed_on": on}], "basis": "가상 토지이용계획 확인", "note": None}}
    doc.update(over)
    return doc


def regulation_payload(**over) -> dict:
    return {**regulation()["payload"], **over}


def development(on="2026-09-02", plan_kind="keep", far_input="far_gross", **over) -> dict:
    p = {"plan_kind": plan_kind, "label": "가상 개발안", "far_input": calc(far_input) if far_input else None, "assumptions": ["가상 전제"], "note": None}
    if far_input is None:
        p["far_reason"] = "대지·규제 확인 전"
    if plan_kind in ("remodel", "rebuild"):
        p["construction"] = {"months": 2, "vacancy_months": 2, "basis": "가상 공정"}
        p["architect_review"] = {"reviewed": True, "reviewed_on": on, "note": "가상 검토안"}
    if plan_kind == "joint_purchase":
        p["joint"] = {"extra_price": {"amount_krw": 12 * E, "basis": "가상"}, "included_in_price": True, "acquisition_order": ["본 필지", "인접 필지"], "period_months": 3, "partial_fallback": "현상 유지"}
    doc = {"kind": "plan_record", "asset_id": A[0], "record_type": "development_plan", "source_kind": "scenario_assumption", "observed_at": on, "supersedes_id": None, "evidence": [], "payload": p}
    doc.update(over)
    return doc


def financing(on="2026-09-03", equity="equity_gross", cash="cash_basic", **over) -> dict:
    p = {"development_plan_id": None, "scenario": "가상 자금안", "equity_input": calc(equity), "cash_input": calc(cash), "note": None}
    doc = {"kind": "plan_record", "asset_id": A[0], "record_type": "financing_plan", "source_kind": "scenario_assumption", "observed_at": on, "supersedes_id": None, "evidence": [], "payload": p}
    doc.update(over)
    return doc


def test_migration_12_rewrites_records_and_keeps_everything(tmp_path, home, monkeypatch, zip_of):
    """버전 11 정본(관측·첨부·근거·목표가·재확인 있음)을 열면 표 재작성으로 새 종류가 허용되고 모든 행·외래키·트리거·인덱스가 보존된다."""
    monkeypatch.setattr(S, "MIGRATIONS", S.MIGRATIONS[:11])
    monkeypatch.setattr(S, "DB_SCHEMA_VERSION", 11)
    path = home / "db" / "j5.sqlite3"
    d = Db.create(path, study_id="j5-synthetic-study", data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    with d.transaction():
        d.add_source_document({"document_id": DOC, "document_kind": "manual_entry", "title": "가상 근거 메모", "collected_at": "2026-09-01T10:00:00+09:00"})
    assert import_package(d, zip_of("valid"), home).outcome == "applied"
    tp = apply_record_input(d, target())
    apply_recheck_input(d, recheck(tp.record_id))
    TABLES = ("records", "record_evidence", "attachments", "import_events", "judgment_rechecks", "subjects")
    before = {t: d.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    recs_before = [dict(r) for r in d.conn.execute("SELECT * FROM records ORDER BY record_id")]
    assert before["records"] == 4 and before["judgment_rechecks"] == 1 and d.schema_version() == 11
    with pytest.raises((ValidationError, sqlite3.IntegrityError)):
        apply_plan_input(d, regulation())
    d.close()
    monkeypatch.undo()
    d2 = Db.open(path)
    try:
        st = d2.status()
        assert st["ok"] and st["db_schema_version"] == S.DB_SCHEMA_VERSION >= 12 and st["foreign_keys"] == 1 and st["foreign_key_check"] == []
        assert {t: d2.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES} == before
        assert [dict(r) for r in d2.conn.execute("SELECT * FROM records ORDER BY record_id")] == recs_before
        names = {r[0] for r in d2.conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'records'")}
        assert {"records_by_subject", "records_by_supersedes", "records_no_update", "records_no_delete"} <= names
        assert "records_new" not in {r[0] for r in d2.conn.execute("SELECT name FROM sqlite_master")}
        r = apply_plan_input(d2, regulation())
        assert d2.conn.execute("SELECT record_type FROM records WHERE record_id = ?", (r["record_id"],)).fetchone()[0] == "regulation_review"
        # 재확인이 가리키던 기록의 외래키가 살아 있다 (지우면 실패)
        with pytest.raises(sqlite3.IntegrityError):
            with d2.transaction():
                d2.conn.execute("DELETE FROM records WHERE record_id = ?", (tp.record_id,))
    finally:
        d2.close()
    # 마이그레이션 9 의 문구는 J5-015A 당시 목록에 고정돼 있고, 12 만 새 종류를 안다
    assert "regulation_review" not in S.MIGRATION_0009 and "'target_price'" in S.MIGRATION_0009 and "regulation_review" in S.MIGRATION_0012
    assert 12 in S.FK_OFF_MIGRATIONS and S.RECORD_TYPES_V9 == ("field_observation", "target_price", "investment_judgment")


def test_plan_add_computes_snapshots_and_chains(db):
    reg = apply_plan_input(db, regulation())
    assert reg["record_type"] == "regulation_review" and reg["unknown"] == [] and reg["calculation_version"] is None and reg["regulation_status"] == "효력 확인"
    dev = apply_plan_input(db, development(**{"payload": {**development()["payload"], "regulation_review_id": reg["record_id"]}}))
    assert dev["far"] == {"review_area_m2": 600, "headroom_m2": 360, "utilization": 0.4} and dev["calculation_version"] == CALCULATION_VERSION and dev["unknown"] == []
    stored = json.loads(db.conn.execute("SELECT payload_json FROM records WHERE record_id = ?", (dev["record_id"],)).fetchone()[0])
    assert stored["far_result"]["result"]["headroom_m2"] == 360 and stored["far_result"]["inputs"]["A_m2"] == 100 and stored["regulation_review_id"] == reg["record_id"]
    fin = apply_plan_input(db, financing(**{"payload": {**financing()["payload"], "development_plan_id": dev["record_id"]}}))
    assert fin["required_equity_krw"] == 17 * E and fin["max_required_equity_krw"] == 21 * E and fin["unknown"] == []
    # 미확인 입력이면 결과 null 로 저장한다 (거절하지 않음)
    d_unknown = development(far_input="far_excluded")
    d_unknown["payload"]["far_input"]["regulation"]["regulation_version"] = None
    d_unknown["payload"]["far_input"]["regulation"]["reason"] = "고시 확인 전"
    dev2 = apply_plan_input(db, d_unknown)
    assert dev2["far"] is None and any("regulation_version" in u for u in dev2["unknown"])
    fin2 = apply_plan_input(db, financing(equity="equity_unknown"))
    assert fin2["required_equity_krw"] is None and fin2["max_required_equity_krw"] == 21 * E and len(fin2["unknown"]) == 2
    dev3 = apply_plan_input(db, development(far_input=None, plan_kind="rebuild"))
    assert dev3["far"] is None and dev3["unknown"] == []
    # 수정 체인: 새 기록이 옛 기록을 대체하고, 대체된 기록은 다시 수정하거나 참조할 수 없다
    reg2 = apply_plan_input(db, regulation(on="2026-09-10", supersedes_id=reg["record_id"]))
    with pytest.raises(ValidationError) as e:
        apply_plan_input(db, regulation(on="2026-09-11", supersedes_id=reg["record_id"]))
    assert "이미 다른 기록으로 수정됐다" in str(e.value)
    with pytest.raises(ValidationError) as e:
        apply_plan_input(db, development(**{"payload": {**development()["payload"], "regulation_review_id": reg["record_id"]}}))
    assert "이미 수정된 기록" in str(e.value)
    # 보도자료·심의결과·가정값 규제를 전제한 개발안은 여유면적을 확정하지 않는다 (데이터 사전 §6)
    press = apply_plan_input(db, regulation(on="2026-09-12", **{"payload": regulation_payload(document_stage="press_release", effective_on=None, notice_number=None, reason="보도자료만 확인")}))
    assert press["regulation_status"].startswith("효력 미확인 (보도자료") and "effective_on" in press["unknown"]
    dev_press = apply_plan_input(db, development(**{"payload": {**development()["payload"], "regulation_review_id": press["record_id"]}}))
    assert dev_press["far"] is None and any("결정고시가 아니다" in u for u in dev_press["unknown"])
    assumed = apply_plan_input(db, regulation(on="2026-09-13", source_kind="scenario_assumption", evidence=[]))
    assert assumed["regulation_status"] == "가정 (시나리오 가정)"
    dev_assumed = apply_plan_input(db, development(**{"payload": {**development()["payload"], "regulation_review_id": assumed["record_id"]}}))
    assert dev_assumed["far"] is None and any("공식 자료가 아니라" in u for u in dev_assumed["unknown"])
    o = plan_overview(db, A[0])
    assert [r["record_id"] for r in o["regulation_reviews"]] == [reg2["record_id"], press["record_id"], assumed["record_id"]] and len(o["development_plans"]) == 5 and len(o["financing_plans"]) == 2 and o["superseded"] == 1
    text = plan_overview_text(o)
    assert "[규제 검토] " in text and "효력 확인 · 자료 공식 자료" in text and "가상시 가상시 고시 제2026-1호 · 결정고시 · 효력일 2026-01-10" in text
    assert "효력 미확인 (보도자료, 효력일 없음)" in text and "[규제 검토·가정]" in text and "공식 자료가 아닌 가정값이다" in text
    assert "검토 600㎡ · 여유 360㎡" in text and "필요자기자본 1,700,000,000원 · 최대 필요자기자본 2,100,000,000원" in text and "미확인: regulation_version" in text
    assert "[개발안·철거신축]" in text and "여유면적 미확정" in text and "고르지 않는다" in text
    with pytest.raises(ValidationError):
        plan_overview(db, "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99")


def test_plan_input_validation(db, tmp_path):
    reg = apply_plan_input(db, regulation())
    obs_tp = apply_record_input(db, target())
    for doc, word in (
        ({**regulation(), "kind": "record"}, "plan_records.schema"),
        ({**regulation(), "record_type": "target_price"}, "plan_records.schema"),
        ({**regulation(), "observed_at": "2026-02-30"}, "date"),
        (regulation(**{"payload": {**regulation()["payload"], "applied_far_pct": None}}), "plan_records.schema"),   # 미확인이면 reason 필수
        (regulation(**{"payload": regulation_payload(zoning=None)}), "plan_records.schema"),
        (regulation(**{"payload": regulation_payload(end_date_confirmed=None)}), "plan_records.schema"),
        (regulation(**{"payload": regulation_payload(document_stage=None)}), "plan_records.schema"),
        (regulation(**{"payload": regulation_payload(document_stage="notice")}), "plan_records.schema"),
        (regulation(evidence=[]), "plan_records.schema"),   # 공식 자료 규제 검토는 근거 문서 필수
        ({k: v for k, v in regulation().items() if k != "evidence"}, "plan_records.schema"),
        (development(**{"payload": {**development()["payload"], "far_result": {"result": None, "unknown": [], "errors": [], "inputs": {}}}}), "plan_records.schema"),  # 결과는 입력에 넣지 않는다
        (development(plan_kind="remodel", **{"payload": {k: v for k, v in development(plan_kind="remodel")["payload"].items() if k != "construction"}}), "plan_records.schema"),
        (financing(**{"payload": {**financing()["payload"], "equity_input": calc("cash_basic")}}), "plan_records.schema"),
        (development(far_input=None, **{"payload": {k: v for k, v in development(far_input=None)["payload"].items() if k != "far_reason"}}), "plan_records.schema"),
    ):
        with pytest.raises(ValidationError) as e:
            load_plan_input(write(tmp_path, doc, "p.json"))
        assert word in str(e.value), str(e.value)
    for doc, word in (
        (regulation(asset_id=A[1], supersedes_id=reg["record_id"]), "같은 물건·같은 종류"),
        (development(supersedes_id=reg["record_id"]), "같은 물건·같은 종류"),
        (development(supersedes_id="7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"), "정본에 없다"),
        (development(**{"payload": {**development()["payload"], "regulation_review_id": obs_tp.record_id}}), "규제 검토 기록이어야"),
        (financing(**{"payload": {**financing()["payload"], "development_plan_id": reg["record_id"]}}), "개발안 기록이어야"),
        (regulation(evidence=[{"document_id": "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"}]), "source_documents"),
        (regulation(asset_id="7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"), "정본에 없다"),
    ):
        with pytest.raises(ValidationError) as e:
            apply_plan_input(db, load_plan_input(write(tmp_path, doc, "p.json")))
        assert word in str(e.value), str(e.value)
    assert db.conn.execute("SELECT COUNT(*) FROM records WHERE record_type IN ('regulation_review', 'development_plan', 'financing_plan')").fetchone()[0] == 1, "거절된 입력은 남지 않는다"
    # 가정값 규제 검토는 근거 문서 없이도 넣을 수 있다 (가정으로 표시된다)
    assert apply_plan_input(db, load_plan_input(write(tmp_path, regulation(source_kind="personal_estimate", evidence=[]), "p.json")))["regulation_status"] == "가정 (개인 추정)"
    # 근거 문서 연결은 record_evidence 에 남는다
    r = apply_plan_input(db, regulation(on="2026-09-05", evidence=[{"document_id": DOC, "verification_status": "verified", "locator": "p.1"}]))
    assert db.conn.execute("SELECT verification_status FROM record_evidence WHERE record_id = ?", (r["record_id"],)).fetchone()[0] == "verified"


def test_projection_includes_plan_records(db, home):
    dev = apply_plan_input(db, development())
    fin = apply_plan_input(db, financing())
    pr = build_projection(db, home)
    assert pr.outcome == "published", pr.to_text()
    rows = {json.loads(l)["record_id"]: json.loads(l) for l in (home / pr.output_dir / "records.jsonl").read_text(encoding="utf-8").splitlines() if l}
    assert rows[dev["record_id"]]["record_type"] == "development_plan" and rows[dev["record_id"]]["payload"]["far_result"]["result"]["headroom_m2"] == 360
    assert rows[fin["record_id"]]["payload"]["equity_result"]["result_krw"] == 17 * E and rows[fin["record_id"]]["schema_version"] == "1.0.0"


def test_cli_plan_add_and_plans(db, tmp_path, capsys, monkeypatch):
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(tmp_path / "home"))
    rc = cli.main(["db", "plan-add", str(write(tmp_path, regulation(), "r.json"))])
    out = capsys.readouterr().out
    assert rc == 0 and "기록 추가: 규제 검토(regulation_review)" in out and "규제 상태: 효력 확인" in out
    rc = cli.main(["db", "plan-add", str(write(tmp_path, development(plan_kind="remodel"), "d.json")), "--json"])
    j = json.loads(capsys.readouterr().out)
    assert rc == 0 and j["far"]["headroom_m2"] == 360 and j["calculation_version"] == CALCULATION_VERSION
    rc = cli.main(["db", "plan-add", str(write(tmp_path, financing(equity="equity_unknown"), "f.json"))])
    out = capsys.readouterr().out
    assert rc == 0 and "필요자기자본 미확정 · 최대 필요자기자본 2,100,000,000원" in out and "미확인: 가격 기준" in out
    rc = cli.main(["db", "plans", "--asset", A[0]])
    out = capsys.readouterr().out
    assert rc == 0 and "규제 검토 1, 개발안 1, 자금안 1" in out and "[개발안·리모델링]" in out and "공사 2개월" in out and "건축사 검토" in out
    assert cli.main(["db", "plans", "--asset", A[0], "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["financing_plans"]) == 1
    assert cli.main(["db", "plan-add", str(write(tmp_path, {**regulation(), "kind": "x"}, "bad.json"))]) == 1
    assert "입력 거절" in capsys.readouterr().err
    assert cli.main(["db", "plan-add", str(tmp_path / "none.json")]) == cli.USAGE_ERROR
    assert cli.main(["db", "plans", "--asset", "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"]) == 1
