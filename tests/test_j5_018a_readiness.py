"""J5-018A: 매입 준비 검토·진입조건 (R7). 데이터 사전 §11, 릴리스 계획 §9 ("만료된 은행 검토, 미확인 보증금 명세, 바뀐 매입 범위, 미해결 명도 조건이 남으면 준비 완료가 되지 않는다.
중요한 입력이 변경되면 영향받은 검토를 다시 요구하고 과거 승인 이력을 보존한다"), §10 J5-018 완료 조건 "유효한 근거·본인 승인·변경 시 재검토", ADR-07, db_schema 13 (표 재작성 + readiness_decisions).

시험: 버전 12 정본을 열 때 표 재작성·보존, 입력 검증(8항목 각 1회·verified 검토일·NA 사유·조건부 미해결·참조 기록·근거 문서·현재 검토 하나), 판정(미완료 → 준비 → 만료 → 차단 →
미확인 보증금·자금안 미확정), 승인(조건 미충족 거절·이력)·검토 뒤 변경(새 자금안·구성 변경·참조 기록 수정)으로 재검토·해제 필요 → 철회(이력 보존·상태 복귀), 결정 불변, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.judgment import apply_record_input
from j5.db.plans import apply_plan_input
from j5.db.projection import build_projection
from j5.db.readiness import AUTO_PREFIX, CHECK_KEYS, MANDATORY_TRIGGERS, apply_case_input, approve, current_case, enforce_all, enforce_release, evaluate, load_case_input, readiness_text, withdraw
from j5.db.store import Db
from j5.db.validate import RECORD_TYPE_SUBJECTS, ValidationError
from tests.test_j5_015a_judgment import A, DOC, SEED_PATH, target, write
from tests.test_j5_016c_plan_records import development, financing, regulation

TODAY = "2026-09-26"


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
    # 정본 시각을 1초씩 전진시켜 "검토 뒤" 판정(recorded_at 비교)이 같은 초에 묶이지 않게 한다
    ticks = iter(range(1, 3600))

    def clock() -> str:
        n = next(ticks)
        return f"2026-09-26T00:{n // 60:02d}:{n % 60:02d}Z"
    d.now = clock
    yield d
    d.close()


def check(key, status="verified", checked_at="2026-09-20", valid_until=None, triggers=(), issues=(), na_reason=None, evidence=(DOC,)) -> dict:
    return {"key": key, "status": status, "evidence_ids": list(evidence), "reviewer_role": "self", "checked_at": checked_at, "valid_until": valid_until,
            "recheck_triggers": list(triggers), "open_issues": list(issues), "not_applicable_reason": na_reason, "note": None}


DEFAULT_TRIGGERS = {"scope_price": ("price", "composition"), "financing": ("loan", "deposit", "price"), "tax_legal": ("regulation", "strategy"), "rights_tenancy": ("tenancy", "deposit"),
                    "building_land": ("regulation", "composition", "due_diligence"), "business_funding": ("strategy", "loan"), "negotiation": ("price",), "final_decision": ("price", "strategy")}


def case(on="2026-09-22", refs=None, checks=None, price=40_0000_0000, deposit=1_0000_0000, **over) -> dict:
    r = {"target_price_id": None, "investment_judgment_id": None, "financing_plan_id": None, "development_plan_id": None, "regulation_review_id": None}
    r.update(refs or {})
    doc = {"kind": "acquisition_review", "asset_id": A[0], "record_type": "acquisition_review", "source_kind": "personal_estimate", "observed_at": on, "supersedes_id": None, "evidence": [],
           "payload": {"strategy": "보유 후 리모델링", "scope": {"ownership_kind": "full", "share_ratio": None, "parcel_pnus": ["1111010100100010000"], "buildings": ["가상 건물 1동"], "composition_as_of": on, "note": None},
                       "price": {"gross_contract_krw": price, "basis": "가상 매도 호가(서면)" if price is not None else None, "reason": None if price is not None else "호가 미확인",
                                 "deposit_total_krw": deposit, "deposit_basis": "가상 임대차 명세" if deposit is not None else "명세 미수령", "vat_status": "excluded", "asking_confirmation": "written_offer"},
                       "references": r, "listing_event": {"kind": "broker_offer", "noted_on": on, "note": None}, "checklist_version": "1.0.0",
                       "checks": checks if checks is not None else [check(k, triggers=DEFAULT_TRIGGERS[k]) for k in CHECK_KEYS], "note": None}}
    doc.update(over)
    return doc


def _plans(db) -> dict:
    """준비 완료가 가능한 참조 기록 한 벌: 효력 있는 규제, 개발안(여유면적 확정), 자금안(확정), 목표가."""
    reg = apply_plan_input(db, regulation())
    dev = apply_plan_input(db, development(**{"payload": {**development()["payload"], "regulation_review_id": reg["record_id"]}}))
    fin = apply_plan_input(db, financing(**{"payload": {**financing()["payload"], "development_plan_id": dev["record_id"]}}))
    tp = apply_record_input(db, target())
    return {"target_price_id": tp.record_id, "financing_plan_id": fin["record_id"], "development_plan_id": dev["record_id"], "regulation_review_id": reg["record_id"]}


def test_migration_13_rewrites_records_and_adds_decisions(home, monkeypatch):
    """버전 12 정본을 열면 표 재작성으로 acquisition_review 가 허용되고 기록·근거가 보존되며 readiness_decisions 가 생긴다. 마이그레이션 12 의 문구는 고정."""
    monkeypatch.setattr(S, "MIGRATIONS", S.MIGRATIONS[:12])
    monkeypatch.setattr(S, "DB_SCHEMA_VERSION", 12)
    path = home / "db" / "j5.sqlite3"
    d = Db.create(path, study_id="j5-synthetic-study", data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    with d.transaction():
        d.add_source_document({"document_id": DOC, "document_kind": "manual_entry", "title": "가상 근거 메모", "collected_at": "2026-09-01T10:00:00+09:00"})
    reg = apply_plan_input(d, regulation())
    apply_record_input(d, target())
    before = [dict(r) for r in d.conn.execute("SELECT * FROM records ORDER BY record_id")]
    assert d.schema_version() == 12 and len(before) == 2 and not d._has_table("readiness_decisions")
    with pytest.raises((ValidationError, sqlite3.IntegrityError)):
        apply_case_input(d, case())
    d.close()
    monkeypatch.undo()
    d2 = Db.open(path)
    try:
        st = d2.status()
        assert st["ok"] and st["db_schema_version"] == S.DB_SCHEMA_VERSION >= 13 and st["foreign_key_check"] == [] and d2._has_table("readiness_decisions")
        assert [dict(r) for r in d2.conn.execute("SELECT * FROM records ORDER BY record_id")] == before
        r = apply_case_input(d2, case(refs={"regulation_review_id": reg["record_id"]}))
        assert d2.conn.execute("SELECT record_type FROM records WHERE record_id = ?", (r["record_id"],)).fetchone()[0] == "acquisition_review"
    finally:
        d2.close()
    assert "acquisition_review" not in S.MIGRATION_0012 and "acquisition_review" in S.MIGRATION_0013 and 13 in S.FK_OFF_MIGRATIONS
    assert S.RECORD_TYPES_V12 == ("field_observation", "target_price", "investment_judgment", "regulation_review", "development_plan", "financing_plan")
    assert set(RECORD_TYPE_SUBJECTS) == set(S.RECORD_TYPES) and "acquisition_review" in S.RECORD_TYPES


def test_case_input_validation(db, tmp_path):
    refs = _plans(db)
    # 파일 로드: 스키마·달력·8항목
    bad = case()
    bad["payload"]["checks"] = bad["payload"]["checks"][:7]
    with pytest.raises(ValidationError):
        load_case_input(write(tmp_path, bad))
    dup = case()
    dup["payload"]["checks"][7] = check("scope_price")
    with pytest.raises(ValidationError):
        load_case_input(write(tmp_path, dup))  # 스키마(contains 8종)에서 걸린다
    with pytest.raises(ValidationError) as e:
        from j5.db.readiness import _validate_dates
        _validate_dates(dup)
    assert "8항목 각 1회" in str(e.value)
    v = case()
    v["payload"]["checks"][0] = check("scope_price", checked_at=None)  # verified 인데 검토일 없음
    with pytest.raises(ValidationError):
        load_case_input(write(tmp_path, v))
    na = case()
    na["payload"]["checks"][0] = check("scope_price", status="not_applicable", na_reason=None)
    with pytest.raises(ValidationError):
        load_case_input(write(tmp_path, na))
    noev = case()
    noev["payload"]["checks"][0] = check("scope_price", evidence=())  # verified 인데 근거 없음
    with pytest.raises(ValidationError):
        load_case_input(write(tmp_path, noev))
    assert all(MANDATORY_TRIGGERS[k] for k in CHECK_KEYS) and MANDATORY_TRIGGERS["final_decision"] == frozenset({"price", "loan", "deposit", "strategy", "composition", "regulation", "due_diligence", "tenancy"})
    cond = case()
    cond["payload"]["checks"][0] = check("scope_price", status="conditional", issues=())
    with pytest.raises(ValidationError):
        load_case_input(write(tmp_path, cond))
    exp = case()
    exp["payload"]["checks"][0] = check("scope_price", checked_at="2026-09-20", valid_until="2026-09-19")
    with pytest.raises(ValidationError) as e:
        load_case_input(write(tmp_path, exp))
    assert "유효기한이 검토일보다 앞선다" in str(e.value)
    pn = case(price=None)
    pn["payload"]["price"]["reason"] = None
    with pytest.raises(ValidationError):
        load_case_input(write(tmp_path, pn))
    assert load_case_input(write(tmp_path, case()))["kind"] == "acquisition_review"
    # 정본 대조: 근거 문서·참조 기록·수정 체인·현재 검토 하나
    with pytest.raises(ValidationError) as e:
        apply_case_input(db, case(checks=[check(k, evidence=("2f5d7c9a-1b3e-4d6f-8a0c-2e4f6a8b0c99",)) for k in CHECK_KEYS]))
    assert "근거 문서" in str(e.value)
    with pytest.raises(ValidationError) as e:
        apply_case_input(db, case(refs={"financing_plan_id": refs["target_price_id"]}))
    assert "financing_plan 기록이어야" in str(e.value)
    reg2 = apply_plan_input(db, regulation(on="2026-09-10", supersedes_id=refs["regulation_review_id"]))
    with pytest.raises(ValidationError) as e:
        apply_case_input(db, case(refs={"regulation_review_id": refs["regulation_review_id"]}))
    assert "이미 수정된 기록" in str(e.value)
    first = apply_case_input(db, case(refs={**refs, "regulation_review_id": reg2["record_id"]}))
    assert first["checks"]["financing"] == "verified" and current_case(db, A[0])["record_id"] == first["record_id"]
    with pytest.raises(ValidationError) as e:
        apply_case_input(db, case(on="2026-09-23"))
    assert "현재 검토는 하나" in str(e.value)
    second = apply_case_input(db, case(on="2026-09-23", supersedes_id=first["record_id"]))
    assert current_case(db, A[0])["record_id"] == second["record_id"]
    with pytest.raises(ValidationError):
        apply_case_input(db, case(on="2026-09-24", supersedes_id=first["record_id"]))
    with pytest.raises(ValidationError):
        apply_case_input(db, case(asset_id=A[1], supersedes_id=second["record_id"]))


def test_readiness_flow_and_blockers(db):
    refs = _plans(db)
    # 미완료 항목·미확인 보증금·자금안 없음 → 준비 아님
    checks = [check(k, triggers=DEFAULT_TRIGGERS[k]) for k in CHECK_KEYS]
    checks[1] = check("financing", status="in_progress", checked_at=None, triggers=DEFAULT_TRIGGERS["financing"])
    r1 = apply_case_input(db, case(checks=checks, deposit=None, refs={"target_price_id": refs["target_price_id"]}))
    e = evaluate(db, A[0], today=TODAY)
    assert not e["ready"] and e["tracking_status"] == "unreviewed" and not e["release_required"]
    verdicts = {c["key"]: c["verdict"] for c in e["checks"]}
    assert verdicts["financing"] == "incomplete" and verdicts["scope_price"] == "ok"
    assert any("보증금 명세 미확인" in u for u in e["unknown"]) and any("자금안 기록 없음" in u for u in e["unknown"])
    t = readiness_text(e)
    assert "판정: 준비 아님" in t and "[미완료] 금융" in t and "중요 미확인: 보증금 명세 미확인" in t
    with pytest.raises(ValidationError) as ex:
        approve(db, A[0], TODAY)
    assert "준비 조건을 충족하지 않아" in str(ex.value)
    # 만료된 은행 검토 → 준비 아님. 기준일을 앞으로 두면 유효
    checks = [check(k, triggers=DEFAULT_TRIGGERS[k]) for k in CHECK_KEYS]
    checks[1] = check("financing", valid_until="2026-09-25", triggers=DEFAULT_TRIGGERS["financing"])
    r2 = apply_case_input(db, case(on="2026-09-23", supersedes_id=r1["record_id"], checks=checks, refs=refs))
    e = evaluate(db, A[0], today=TODAY)
    assert not e["ready"] and {c["key"]: c["verdict"] for c in e["checks"]}["financing"] == "expired" and e["unknown"] == []
    assert evaluate(db, A[0], today="2026-09-25")["ready"] is True
    # 차단·조건부(미해결 명도 조건) → 준비 아님
    checks = [check(k, triggers=DEFAULT_TRIGGERS[k]) for k in CHECK_KEYS]
    checks[3] = check("rights_tenancy", status="conditional", issues=["명도 조건 미해결"], triggers=DEFAULT_TRIGGERS["rights_tenancy"])
    r3 = apply_case_input(db, case(on="2026-09-24", supersedes_id=r2["record_id"], checks=checks, refs=refs))
    e = evaluate(db, A[0], today=TODAY)
    assert not e["ready"] and any("권리·임대차: 조건부" in b and "미해결 1건" in b for b in e["blockers"])
    # 미확정 자금안을 전제하면 중요 미확인
    fin_unknown = apply_plan_input(db, financing(on="2026-09-24", equity="equity_unknown"))
    checks = [check(k, status="not_applicable", checked_at=None, na_reason="가상 시험: 해당 없음", evidence=()) if k == "negotiation" else check(k, triggers=DEFAULT_TRIGGERS[k]) for k in CHECK_KEYS]
    r4 = apply_case_input(db, case(on="2026-09-25", supersedes_id=r3["record_id"], checks=checks, refs={**refs, "financing_plan_id": fin_unknown["record_id"]}))
    e = evaluate(db, A[0], today=TODAY)
    assert not e["ready"] and any("필요자기자본이 미확정" in u for u in e["unknown"]) and {c["key"]: c["verdict"] for c in e["checks"]}["negotiation"] == "not_applicable"
    # 모든 항목 유효·확정 참조 → 준비 완료 → 본인 승인 → purchase_ready + 결정 이력
    r5 = apply_case_input(db, case(on="2026-09-25", supersedes_id=r4["record_id"], checks=checks, refs=refs))
    e = evaluate(db, A[0], today=TODAY)
    assert e["ready"] and e["blockers"] == [] and e["signals"] == [] and "판정: 준비 완료 조건 충족" in readiness_text(e)
    v_before = db.status()["dataset_version"]
    ap = approve(db, A[0], TODAY, note="가상 승인")
    assert ap["previous_status"] == "unreviewed" and ap["new_status"] == "purchase_ready" and ap["dataset_version"] == v_before + 1
    assert db.conn.execute("SELECT tracking_status FROM assets WHERE asset_id = ?", (A[0],)).fetchone()[0] == "purchase_ready"
    e = evaluate(db, A[0], today=TODAY)
    assert e["tracking_status"] == "purchase_ready" and e["ready"] and not e["release_required"] and e["last_decision"]["decision"] == "approve" and e["decisions"] == 1
    with pytest.raises(ValidationError) as ex:
        approve(db, A[0], TODAY)
    assert "이미 purchase_ready" in str(ex.value)
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.conn.execute("DELETE FROM readiness_decisions")
    # 승인 뒤 변경: 새 자금안(대출) → 금융·사업·자금·최종 의사결정 항목 재검토(기본 조건), 해제 필요. 입력 조건을 비워도 기본 조건이 잡는다
    apply_plan_input(db, financing(on="2026-09-26"))
    e = evaluate(db, A[0], today=TODAY)
    vd = {c["key"]: c["verdict"] for c in e["checks"]}
    assert vd["financing"] == "recheck" and vd["business_funding"] == "recheck" and vd["final_decision"] == "recheck" and vd["scope_price"] == "ok" and vd["tax_legal"] == "ok"
    assert not e["ready"] and e["release_required"] and any("금융: 변경 감지" in b and "대출 변경" in b for b in e["blockers"])
    assert "주의: purchase_ready 인데" in readiness_text(e) and "검토 뒤 변경 1건: 대출(record)" in readiness_text(e)
    # 참조한 목표 매수가를 수정하면 가격 변경으로 잡힌다. 해당 없음 항목도 기본 조건에 걸리면 재검토다
    tp2 = target(price=1_400_000_000, decided="2026-09-26", supersedes_id=refs["target_price_id"])
    tp2["payload"]["revision_reason"] = "가상: 인근 거래 반영"
    apply_record_input(db, tp2)
    e = evaluate(db, A[0], today=TODAY)
    assert {c["key"]: c["verdict"] for c in e["checks"]}["negotiation"] == "recheck" and {c["key"]: c["verdict"] for c in e["checks"]}["scope_price"] == "recheck"
    assert len(e["ref_changes"]) == 1 and e["ref_changes"][0]["reference"] == "target_price_id"
    # 철회: 사유 필수, 이력 보존, 관심 단계 복귀
    with pytest.raises(ValidationError):
        withdraw(db, A[0], TODAY, reason="  ")
    wd = withdraw(db, A[0], TODAY, reason="새 자금안·목표가 변경으로 재검토")
    assert wd["new_status"] == "detailed_review" and db.conn.execute("SELECT tracking_status FROM assets WHERE asset_id = ?", (A[0],)).fetchone()[0] == "detailed_review"
    rows = [tuple(r) for r in db.conn.execute("SELECT decision, previous_status, new_status FROM readiness_decisions WHERE asset_id = ? ORDER BY recorded_at, rowid", (A[0],))]
    assert rows == [("approve", "unreviewed", "purchase_ready"), ("withdraw", "purchase_ready", "detailed_review")]
    snap = json.loads(db.conn.execute("SELECT evaluation_json FROM readiness_decisions ORDER BY recorded_at DESC, rowid DESC LIMIT 1").fetchone()[0])
    assert snap["release_required"] is True and any(c["verdict"] == "recheck" for c in snap["checks"])
    with pytest.raises(ValidationError):
        withdraw(db, A[0], TODAY, reason="다시")
    e = evaluate(db, A[0], today=TODAY)
    assert e["tracking_status"] == "detailed_review" and not e["release_required"] and e["decisions"] == 2
    # 새 검토 기록(변경 반영)으로 다시 준비 완료 → 재승인 가능
    new_fin = current_fin = db.conn.execute("SELECT record_id FROM records WHERE record_type = 'financing_plan' AND observed_at = '2026-09-26'").fetchone()[0]
    new_tp = db.conn.execute("SELECT record_id FROM records WHERE record_type = 'target_price' AND supersedes_id = ?", (refs["target_price_id"],)).fetchone()[0]
    r6 = apply_case_input(db, case(on="2026-09-26", supersedes_id=r5["record_id"], checks=checks, refs={**refs, "financing_plan_id": new_fin, "target_price_id": new_tp}))
    e = evaluate(db, A[0], today=TODAY)
    assert e["ready"] and e["signals"] == [] and e["case"]["record_id"] == r6["record_id"]
    assert approve(db, A[0], TODAY)["previous_status"] == "detailed_review"
    assert db.conn.execute("SELECT COUNT(*) FROM readiness_decisions").fetchone()[0] == 3 and current_fin == new_fin


def test_auto_withdrawal_on_readiness_and_projection(db, home):
    """purchase_ready 가 유효하지 않으면 readiness·파생본 생성이 이력을 남기며 자동 철회한다. 게시된 파생본은 오래된 purchase_ready 를 담지 않는다."""
    refs = _plans(db)
    checks = [check(k) for k in CHECK_KEYS]
    apply_case_input(db, case(checks=checks, refs=refs))
    approve(db, A[0], TODAY)
    assert enforce_release(db, A[0], today=TODAY) is None and enforce_all(db, today=TODAY) == []
    apply_plan_input(db, regulation(on="2026-09-26"))  # 규제 변경
    d = enforce_release(db, A[0], today=TODAY)
    assert d is not None and d["decision"] == "withdraw" and d["reason"].startswith(AUTO_PREFIX) and "규제 변경" in d["reason"]
    assert db.conn.execute("SELECT tracking_status FROM assets WHERE asset_id = ?", (A[0],)).fetchone()[0] == "detailed_review"
    assert enforce_release(db, A[0], today=TODAY) is None
    rows = [tuple(r) for r in db.conn.execute("SELECT decision, reason FROM readiness_decisions ORDER BY recorded_at, rowid")]
    assert rows[0][0] == "approve" and rows[1][0] == "withdraw" and rows[1][1].startswith(AUTO_PREFIX)
    # 파생본 생성 경로: 다시 승인한 뒤 변경을 넣고 project 를 돌리면 게시본의 관심 단계가 detailed_review 다
    reg2 = db.conn.execute("SELECT record_id FROM records WHERE record_type = 'regulation_review' AND observed_at = '2026-09-26'").fetchone()[0]
    cur = current_case(db, A[0])
    apply_case_input(db, case(on="2026-09-26", supersedes_id=cur["record_id"], checks=checks, refs={**refs, "regulation_review_id": reg2}))
    approve(db, A[0], TODAY)
    apply_plan_input(db, financing(on="2026-09-26"))
    r = build_projection(db, home)
    assert r.outcome == "published" and any(f["code"] == "readiness_withdrawn" for f in r.findings)
    seed = json.loads((home / r.output_dir / "assets.seed.json").read_text(encoding="utf-8"))
    geo = json.loads((home / r.output_dir / "assets.geojson").read_text(encoding="utf-8"))
    assert next(f["properties"]["tracking_status"] for f in geo["features"] if f["properties"]["asset_id"] == A[0]) == "detailed_review"
    assert db.conn.execute("SELECT COUNT(*) FROM readiness_decisions WHERE reason LIKE ?", (AUTO_PREFIX + "%",)).fetchone()[0] == 2
    assert r.source_dataset_version == db.status()["dataset_version"], "자동 철회로 오른 버전을 스냅샷했다"


def test_composition_change_and_observation_trigger_recheck(db):
    from j5.db.parcels import apply_links, load_bundle
    from tests.test_j5_015a_judgment import BUNDLE, P1
    refs = _plans(db)
    checks = [check(k, triggers=DEFAULT_TRIGGERS[k]) for k in CHECK_KEYS]
    apply_case_input(db, case(checks=checks, refs=refs))
    assert evaluate(db, A[0], today=TODAY)["ready"]
    load_bundle(db, json.loads(BUNDLE.read_text(encoding="utf-8")))
    apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[0], "pnu": P1, "effective_from": "2026-09-26", "effective_to": None, "basis": "location_point", "note": "가상 연결"}]})
    e = evaluate(db, A[0], today=TODAY)
    vd = {c["key"]: c["verdict"] for c in e["checks"]}
    assert vd["scope_price"] == "recheck" and vd["building_land"] == "recheck" and vd["financing"] == "ok" and any(s["trigger"] == "composition" for s in e["signals"])
    assert not e["ready"] and any("구성 토지 변경" in b for b in e["blockers"])
    # 같은 초에 들어온 구성 변경도 검토 뒤로 본다: 검토 기록과 같은 시각의 updated_at 을 흉내
    with db.transaction():
        db.conn.execute("UPDATE asset_components SET updated_at = ? WHERE asset_id = ?", (current_case(db, A[0])["recorded_at"], A[0]))
    assert any(s["trigger"] == "composition" for s in evaluate(db, A[0], today=TODAY)["signals"])


def test_cli_case_readiness_approve_withdraw(db, tmp_path, capsys, monkeypatch):
    refs = _plans(db)
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(tmp_path / "home"))
    p = write(tmp_path, case(refs=refs))
    assert cli.main(["db", "case-add", str(p)]) == 0
    out = capsys.readouterr().out
    assert "매입 준비 검토 기록 추가" in out and "관심 단계는 그대로다" in out
    assert cli.main(["db", "case-add", str(p)]) == 1
    assert "현재 검토는 하나" in capsys.readouterr().err
    assert cli.main(["db", "readiness", "--asset", A[0], "--as-of", TODAY]) == 0
    assert "판정: 준비 완료 조건 충족" in capsys.readouterr().out
    assert cli.main(["db", "readiness-withdraw", "--asset", A[0], "--on", TODAY, "--reason", "x"]) == 1
    assert "철회할 것이 없다" in capsys.readouterr().err
    assert cli.main(["db", "readiness-approve", "--asset", A[0], "--on", TODAY, "--note", "가상"]) == 0
    assert "unreviewed → purchase_ready" in capsys.readouterr().out
    assert cli.main(["db", "readiness", "--asset", A[0], "--as-of", TODAY, "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["tracking_status"] == "purchase_ready" and j["ready"] and j["last_decision"]["decision"] == "approve"
    # 승인 뒤 새 규제 검토 → readiness 가 이력을 남기며 자동 철회 (종료 코드 1). 그 뒤 수동 철회는 할 것이 없다
    assert cli.main(["db", "plan-add", str(write(tmp_path, regulation(on="2026-09-26"), "reg.json"))]) == 0
    capsys.readouterr()
    assert cli.main(["db", "readiness", "--asset", A[0], "--as-of", TODAY]) == 1
    out = capsys.readouterr().out
    assert "자동 철회: 물건" in out and AUTO_PREFIX in out and "규제 변경" in out and "관심 단계 detailed_review" in out
    assert cli.main(["db", "readiness-withdraw", "--asset", A[0], "--on", TODAY, "--reason", "규제 검토 갱신"]) == 1
    assert "철회할 것이 없다" in capsys.readouterr().err
    assert cli.main(["db", "readiness", "--asset", "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"]) == 1
    assert cli.main(["db", "readiness", "--asset", A[1]]) == 1
    assert "검토 기록 없음" in capsys.readouterr().out
