"""J5-015C: 반대 증거·변경 조건 재확인 (데이터 사전 §8, 릴리스 계획 §8·§9, db_schema 11).

시험: judgment_rechecks 표·불변 트리거, 판단 뒤 들어온 정보(관측·연결 결정·취소 표시·비교 근거 변동·범위 규칙·구성 변경·반박 근거)로 "재확인 필요" 판정,
재확인 기록 뒤 상태(재확인됨 → 새 정보 → 다시 재확인 필요 → 수정 필요), 수정된 기록은 재확인 대상에서 빠짐, 입력 검증, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from j5 import cli
from j5.collect import rt
from j5.collect.rt import collect_months
from j5.db import schema as S
from j5.db.judgment import apply_record_input
from j5.db.parcels import apply_links, load_bundle
from j5.db.recheck import apply_recheck_input, load_recheck_input, recheck_status, recheck_text, signals_since
from j5.db.store import Db
from j5.db.transactions import identity_hash, load_run
from j5.db.txlinks import apply_decisions
from j5.db.validate import ValidationError
from j5.db.zones import apply_rules
from tests.test_j5_014_collect import KEY, _Handler, item, server  # noqa: F401
from tests.test_j5_015a_judgment import A, BUNDLE, DOC, P1, SEED_PATH, judgment, target, write

RULES = {"kind": "zone_rules", "name": "잠정 핵심", "core": ["가상동"], "comparison": [], "note": None}


@pytest.fixture(autouse=True)
def sequential_run_ids(monkeypatch):
    counter = iter(range(1, 10_000))
    monkeypatch.setattr(rt, "_new_run_id", lambda: f"20260925-160000-{next(counter):06x}")


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


def recheck(record_id: str, on="2026-09-26", outcome="reconfirmed", **over) -> dict:
    doc = {"kind": "recheck", "asset_id": A[0], "record_id": record_id, "reviewed_on": on, "outcome": outcome,
           "conditions_checked": [{"condition": "용도지역 변경 없음", "holds": True, "note": None}], "counter_evidence": [], "note": None}
    doc.update(over)
    return doc


def observe(db, rid: str, on: str, status="no_change", corrects=None) -> None:
    with db.transaction():
        db.insert_record({"record_id": rid, "subject_id": A[0], "subject_type": "asset", "record_type": "field_observation", "source_kind": "field_observation",
                          "schema_version": "1.0.0", "payload": {"change_status": status, "note": None}, "observed_at": on, "observed_at_precision": "date", "supersedes_id": corrects})


def test_schema_11_table_is_immutable(db):
    assert db.status()["db_schema_version"] == S.DB_SCHEMA_VERSION >= 11
    r1 = apply_record_input(db, target())
    res = apply_recheck_input(db, recheck(r1.record_id))
    assert res["outcome"] == "reconfirmed" and res["signals_seen"] == 0 and res["dataset_version"] >= 2
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.conn.execute("UPDATE judgment_rechecks SET outcome = 'revision_needed' WHERE recheck_id = ?", (res["recheck_id"],))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.conn.execute("DELETE FROM judgment_rechecks WHERE recheck_id = ?", (res["recheck_id"],))
    assert db.conn.execute("SELECT outcome FROM judgment_rechecks").fetchone()[0] == "reconfirmed"


def test_signals_and_status_flow(db, home, server, tmp_path):
    """9/5 목표가·투자판단 기록 → 새 정보 없음. 9/25 관측·연결·취소·범위 규칙·구성 변경 → 재확인 필요. 9/26 재확인 → 재확인됨. 9/27 새 관측 → 다시 재확인 필요. 수정 필요 판정 → 새 기록으로 수정."""
    clock = {"now": "2026-09-05T00:00:00Z"}
    db.now = lambda: clock["now"]
    observe(db, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "2026-09-03")
    load_bundle(db, json.loads(BUNDLE.read_text(encoding="utf-8")))
    apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[0], "pnu": P1, "effective_from": "2026-09-01", "effective_to": None, "basis": "manual", "note": None}]})
    aug0 = {**item(0), "umdNm": "가상동", "jibun": "1", "dealYear": "2026", "dealMonth": "8", "dealDay": "10"}
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [aug0]}}
    run1 = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=["202608"], endpoint=server, sleep=lambda s: None)
    load_run(db, home, "11110", run1.run_id)
    t1 = db.conn.execute("SELECT transaction_id FROM transactions WHERE identity_hash = ?", (identity_hash(aug0),)).fetchone()[0]
    apply_decisions(db, [{"_line": 2, "transaction_id": t1, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual", "reviewed_on": "2026-09-04"}])
    clock["now"] = "2026-09-05T01:00:00Z"
    tp = target()
    tp["payload"]["comparison_basis"] = [{"kind": "transaction", "ref_id": t1, "note": "8/10 인근 거래"}]
    r_tp = apply_record_input(db, tp)
    r_j = apply_record_input(db, judgment(status="formal", decision="approved"))
    st = recheck_status(db, A[0])
    assert [(x["record_type"], x["status"]) for x in st["records"]] == [("investment_judgment", "no_signal"), ("target_price", "no_signal")]
    tp_item = next(x for x in st["records"] if x["record_type"] == "target_price")
    assert [c["name"] for c in tp_item["conditions"]] == ["유효 조건", "비용·규제 전제", "비용·규제 전제"], "조건 목록은 payload 의 유효 조건·전제"
    assert next(x for x in st["records"] if x["record_type"] == "investment_judgment")["conditions"][2]["text"] == "counter_evidence 작성"
    # 9/25: 관측(변화, 9/20 정정 포함), 두 번째 거래 연결 확정, 첫 거래 취소 표시, 범위 규칙 v1, 필지 연결 변경
    clock["now"] = "2026-09-25T00:00:00Z"
    observe(db, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2", "2026-09-20", status="change_observed")
    observe(db, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3", "2026-09-03", corrects="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
    aug1 = {**item(1), "umdNm": "가상동", "jibun": "1", "dealYear": "2026", "dealMonth": "8", "dealDay": "12"}
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [{**aug0, "cdealType": "O", "cdealDay": "26.09.20"}, aug1]}}
    run2 = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=["202608"], endpoint=server, sleep=lambda s: None)
    load_run(db, home, "11110", run2.run_id)
    t2 = db.conn.execute("SELECT transaction_id FROM transactions WHERE identity_hash = ?", (identity_hash(aug1),)).fetchone()[0]
    apply_decisions(db, [{"_line": 2, "transaction_id": t2, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual", "reviewed_on": "2026-09-25"}])
    apply_rules(db, RULES)
    apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[0], "pnu": P1, "effective_from": "2026-09-01", "effective_to": "2026-09-30", "basis": "manual", "note": "구성 축소"}]})
    st = recheck_status(db, A[0])
    tp_item = next(x for x in st["records"] if x["record_type"] == "target_price")
    sig = tp_item["signals"]
    assert tp_item["status"] == "recheck_needed" and sig["since"] == "2026-09-05T01:00:00Z" and sig["latest_at"] == "2026-09-25T00:00:00Z"
    assert sig["counts"] == {"observations": 2, "observations_changed": 1, "link_decisions": 1, "cancellations": 1, "comparison_basis_problems": 1, "zone_rule_versions": 1,
                             "component_changes": 1, "disputed_evidence": 0}, sig["counts"]
    assert sig["observations"][1]["corrects"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1" and sig["link_decisions"][0]["transaction_id"] == t2
    assert sig["cancellations"][0]["transaction_id"] == t1 and "취소" in sig["comparison_basis"][0]["problem"] and sig["components"][0]["kind"] == "changed"
    j_item = next(x for x in st["records"] if x["record_type"] == "investment_judgment")
    assert j_item["status"] == "recheck_needed" and j_item["signals"]["counts"]["comparison_basis_problems"] == 0, "비교 근거 변동은 목표 매수가에만"
    text = recheck_text(st)
    assert "[재확인 필요] target_price" in text and "취소 표시" in text and "구성 changed" in text and "범위 규칙 v1" in text
    # 9/26 재확인(반대 증거 1건, 조건 하나 깨짐 아님) → 재확인됨. 신호 요약이 남는다
    clock["now"] = "2026-09-26T00:00:00Z"
    res = apply_recheck_input(db, recheck(r_tp.record_id, counter_evidence=[{"note": "8/10 거래 취소로 비교 근거 약화", "document_id": DOC, "observed_on": "2026-09-25"}]))
    assert res["signals_seen"] == 8
    st = recheck_status(db, A[0])
    tp_item = next(x for x in st["records"] if x["record_type"] == "target_price")
    assert tp_item["status"] == "reconfirmed" and tp_item["last_recheck"]["signals"]["total"] == 8 and tp_item["last_recheck"]["counter_evidence"][0]["document_id"] == DOC
    assert next(x for x in st["records"] if x["record_type"] == "investment_judgment")["status"] == "recheck_needed", "투자판단은 아직 재확인하지 않았다"
    assert "반대 증거: 8/10 거래 취소로 비교 근거 약화" in recheck_text(st)
    # 9/27 새 관측 → 다시 재확인 필요 (마지막 재확인 뒤의 신호만)
    clock["now"] = "2026-09-27T00:00:00Z"
    observe(db, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4", "2026-09-27")
    tp_item = next(x for x in recheck_status(db, A[0])["records"] if x["record_type"] == "target_price")
    assert tp_item["status"] == "recheck_needed" and "마지막 재확인(2026-09-26) 뒤" in tp_item["reason"]
    # 수정 필요 판정 → 상태 수정 필요. 새 기록으로 수정하면 옛 기록은 목록에서 빠지고 새 기록은 새 정보 없음
    apply_recheck_input(db, recheck(r_tp.record_id, on="2026-09-27", outcome="revision_needed", conditions_checked=[{"condition": "용도지역 변경 없음", "holds": False, "note": "비교 근거 취소"}]))
    tp_item = next(x for x in recheck_status(db, A[0])["records"] if x["record_type"] == "target_price")
    assert tp_item["status"] == "revision_needed" and tp_item["rechecks"] == 2
    clock["now"] = "2026-09-28T00:00:00Z"
    tp2 = target(price=1_400_000_000, decided="2026-09-28", supersedes_id=r_tp.record_id)
    tp2["payload"]["revision_reason"] = "비교 근거 거래 취소"
    r_tp2 = apply_record_input(db, tp2)
    st = recheck_status(db, A[0])
    ids = [x["record_id"] for x in st["records"]]
    assert r_tp.record_id not in ids and r_tp2.record_id in ids
    assert next(x for x in st["records"] if x["record_id"] == r_tp2.record_id)["status"] == "no_signal"
    with pytest.raises(ValidationError) as e:
        apply_recheck_input(db, recheck(r_tp.record_id, on="2026-09-28"))
    assert "이미" in str(e.value) and "수정됐다" in str(e.value)
    # 재확인 기록은 판단을 바꾸지 않는다: 옛 기록의 이력은 그대로
    assert db.conn.execute("SELECT COUNT(*) FROM judgment_rechecks WHERE record_id = ?", (r_tp.record_id,)).fetchone()[0] == 2
    assert r_j.record_id in ids


def test_signals_since_disputed_evidence_and_no_links_tables(db):
    r = apply_record_input(db, {**judgment(), "evidence": [{"document_id": DOC, "verification_status": "disputed", "locator": "p.3"}]})
    it = recheck_status(db, A[0])["records"][0]
    assert it["signals"]["counts"]["disputed_evidence"] == 1 and it["status"] == "recheck_needed" and it["signals"]["disputed_evidence"][0]["locator"] == "p.3"
    rec = {"record_id": r.record_id, "record_type": "investment_judgment", "observed_at": "2026-09-05", "recorded_at": db.conn.execute("SELECT recorded_at FROM records WHERE record_id = ?", (r.record_id,)).fetchone()[0], "payload": {}}
    assert signals_since(db, A[0], rec)["total"] == 1


def test_recheck_input_validation(db, tmp_path):
    r = apply_record_input(db, target())
    obs = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    observe(db, obs, "2026-09-03")
    for doc, word in (
        ({**recheck(r.record_id), "kind": "x"}, "judgment_recheck.schema"),
        ({**recheck(r.record_id), "outcome": "maybe"}, "judgment_recheck.schema"),
        ({**recheck(r.record_id), "reviewed_on": "2026-02-30"}, "is not a 'date'"),
        (recheck(r.record_id, counter_evidence=[{"note": "x", "observed_on": "2026-13-01"}]), "observed_on"),
    ):
        with pytest.raises(ValidationError) as e:
            load_recheck_input(write(tmp_path, doc, "rc.json"))
        assert word in str(e.value), str(e.value)
    for doc, word in (
        (recheck(obs), "목표 매수가·투자판단 기록이어야"),
        (recheck("7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"), "정본에 없다"),
        ({**recheck(r.record_id), "asset_id": A[1]}, "이 물건의"),
        (recheck(r.record_id, on="2026-08-31"), "앞선다"),
        (recheck(r.record_id, counter_evidence=[{"note": "x", "document_id": "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"}]), "source_documents"),
    ):
        with pytest.raises(ValidationError) as e:
            apply_recheck_input(db, load_recheck_input(write(tmp_path, doc, "rc.json")))
        assert word in str(e.value), str(e.value)
    assert db.conn.execute("SELECT COUNT(*) FROM judgment_rechecks").fetchone()[0] == 0, "거절된 입력은 아무것도 남기지 않는다"


def test_cli_recheck_and_recheck_add(db, tmp_path, capsys, monkeypatch):
    r = apply_record_input(db, target())
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(tmp_path / "home"))
    rc = cli.main(["db", "recheck", "--asset", A[0]])
    out = capsys.readouterr().out
    assert rc == 0 and "[새 신호 없음] target_price" in out and "조건 [유효 조건] 용도지역 변경 없음" in out
    f = write(tmp_path, recheck(r.record_id, note="분기 점검"))
    rc = cli.main(["db", "recheck-add", str(f)])
    out = capsys.readouterr().out
    assert rc == 0 and "재확인 기록: 재확인됨" in out
    rc = cli.main(["db", "recheck", "--asset", A[0], "--json"])
    j = json.loads(capsys.readouterr().out)
    assert rc == 0 and j["records"][0]["status"] == "reconfirmed" and j["records"][0]["last_recheck"]["note"] == "분기 점검"
    assert cli.main(["db", "recheck-add", str(write(tmp_path, recheck(r.record_id, on="2026-08-01"), "bad.json"))]) == 1
    assert "입력 거절" in capsys.readouterr().err
    assert cli.main(["db", "recheck-add", str(tmp_path / "none.json")]) == cli.USAGE_ERROR
    assert cli.main(["db", "recheck", "--asset", "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"]) == 1
