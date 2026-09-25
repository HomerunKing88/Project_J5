"""J5-015A: 목표 매수가·투자판단 기록과 당시 기록 기준 조회 (데이터 사전 §8·§1, ADR-14, db_schema 9).

시험: records 표 재작성 마이그레이션(기존 기록·근거·첨부·수입 대장·외래키·트리거·인덱스 보존, 실패 시 되돌림·외래키 재활성), 입력 검증(스키마·판단일·formal 10항목·승인은 formal 만·
수정 사유·이전 기록 조건·근거 문서), 기록 추가·수정 체인, 당시 기록 기준 조회(K 이후 기록·정정·연결 철회·나중에 넣은 과거 거래·취소 표시 제외, 마지막 확인일, 경계 기준일 주의), CLI. 가상자료만 쓴다.
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
from j5.db.importer import import_package
from j5.db.judgment import apply_record_input, asof, asof_text, load_record_input
from j5.db.parcels import apply_links, load_bundle
from j5.db.store import Db, DbError
from j5.db.transactions import identity_hash, load_run
from j5.db.txlinks import apply_decisions
from j5.db.validate import ValidationError
from tests.conftest import PACKAGES
from tests.test_j5_014_collect import KEY, _Handler, item, server  # noqa: F401

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
BUNDLE = Path(__file__).resolve().parent / "fixtures" / "parcels" / "synthetic.j5parcels.json"
A = [f"7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a5{i}" for i in range(5)]
P1 = "9999900100100010000"
DOC = "2f5d7c9a-1b3e-4d6f-8a0c-2e4f6a8b0c1d"


@pytest.fixture(autouse=True)
def sequential_run_ids(monkeypatch):
    counter = iter(range(1, 10_000))
    monkeypatch.setattr(rt, "_new_run_id", lambda: f"20260925-150000-{next(counter):06x}")


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


def target(price=1_500_000_000, decided="2026-09-01", **over) -> dict:
    doc = {"kind": "record", "asset_id": A[0], "record_type": "target_price", "source_kind": "personal_estimate", "observed_at": decided, "supersedes_id": None,
           "evidence": [], "payload": {"price_kind": "target_buy", "price_krw": price, "decided_on": decided, "strategy": "보유 후 리모델링", "composition_as_of": decided,
                                        "assumptions": ["취득세 4.6%", "일반상업 용적률 600% 가정"], "comparison_basis": [{"kind": "personal_estimate", "ref_id": None, "note": "인근 거래 평당가 추정"}],
                                        "valid_conditions": ["용도지역 변경 없음"], "revision_reason": None}}
    doc.update(over)
    return doc


def judgment(status="draft", decision="none", decided="2026-09-05", **over) -> dict:
    p = {"status": status, "decision": decision, "strategy": "보유", "alternatives": None, "current_price_gap": None, "price_gap_change_conditions": None, "demand": None,
         "usability_and_constraints": None, "financing_cash_flow": None, "counter_evidence": None, "judgment_change_conditions": None, "purchase_conditions_next_checks": None, "note": None}
    if status == "formal":
        for k in ("alternatives", "current_price_gap", "price_gap_change_conditions", "demand", "usability_and_constraints", "financing_cash_flow", "counter_evidence",
                  "judgment_change_conditions", "purchase_conditions_next_checks"):
            p[k] = f"{k} 작성"
    doc = {"kind": "record", "asset_id": A[0], "record_type": "investment_judgment", "source_kind": "personal_estimate", "observed_at": decided, "supersedes_id": None,
           "evidence": [], "payload": p}
    doc.update(over)
    return doc


def write(tmp_path, doc, name="rec.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def test_records_table_rebuild_keeps_data_and_constraints(tmp_path, home, monkeypatch, zip_of):
    """버전 8 까지의 정본에 기록·근거·첨부·수입 대장이 있는 상태에서 열면 표 재작성 마이그레이션이 붙고 모두 보존된다."""
    monkeypatch.setattr(S, "MIGRATIONS", S.MIGRATIONS[:8])
    monkeypatch.setattr(S, "DB_SCHEMA_VERSION", 8)
    path = home / "db" / "j5.sqlite3"
    d = Db.create(path, study_id="j5-synthetic-study", data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    assert import_package(d, zip_of("valid"), home).outcome == "applied"
    TABLES = ("records", "record_evidence", "attachments", "import_events", "subjects")
    before = {t: d.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    recs_before = [dict(r) for r in d.conn.execute("SELECT * FROM records ORDER BY record_id")]
    assert before["records"] == 3 and before["record_evidence"] == 3 and before["attachments"] == 3 and d.schema_version() == 8
    with pytest.raises(sqlite3.IntegrityError):
        with d.transaction():
            d.insert_record({"record_id": "11111111-1111-4111-8111-111111111111", "subject_id": A[0], "subject_type": "asset", "record_type": "target_price", "source_kind": "personal_estimate",
                             "schema_version": "1.0.0", "payload": target()["payload"], "observed_at": "2026-09-01", "observed_at_precision": "date"})
    d.close()
    monkeypatch.undo()
    d2 = Db.open(path)
    try:
        st = d2.status()
        assert st["ok"] and st["db_schema_version"] == 9 and st["foreign_keys"] == 1
        assert {t: d2.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES} == before
        assert [dict(r) for r in d2.conn.execute("SELECT * FROM records ORDER BY record_id")] == recs_before
        names = {r[0] for r in d2.conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'records'")}
        assert {"records_by_subject", "records_by_supersedes", "records_no_update", "records_no_delete"} <= names and "records_new" not in {r[0] for r in d2.conn.execute("SELECT name FROM sqlite_master")}
        # 자식 표의 외래키가 새 records 를 가리키고, 불변 트리거·자기 참조도 살아 있다
        for child in ("record_evidence", "attachments", "import_events", "unit_observations", "record_survey_refs"):
            assert any(fk[2] == "records" for fk in d2.conn.execute(f"PRAGMA foreign_key_list({child})")), child
        assert any(fk[2] == "records" and fk[3] == "supersedes_id" for fk in d2.conn.execute("PRAGMA foreign_key_list(records)"))
        with pytest.raises(sqlite3.IntegrityError):
            with d2.transaction():
                d2.conn.execute("DELETE FROM records")
        with pytest.raises(sqlite3.IntegrityError):
            with d2.transaction():
                d2.conn.execute("INSERT INTO record_evidence (record_id, field_path, document_id, recorded_at) VALUES ('22222222-2222-4222-8222-222222222222', '$', ?, '2026-09-25T00:00:00Z')", (recs_before[0]["record_id"],))
        # 새 기록 종류가 들어간다
        r = apply_record_input(d2, target())
        assert r.record_id and d2.get_record(r.record_id)["record_type"] == "target_price"
    finally:
        d2.close()


def test_fk_off_migration_rolls_back_and_restores_fk_on_failure(home, monkeypatch):
    path = home / "db" / "j5.sqlite3"
    monkeypatch.setattr(S, "MIGRATIONS", S.MIGRATIONS[:8])
    monkeypatch.setattr(S, "DB_SCHEMA_VERSION", 8)
    d = Db.create(path, study_id="j5-synthetic-study", data_mode="synthetic")
    d.close()
    monkeypatch.undo()
    broken = S.MIGRATIONS[:8] + ((9, "r4_record_types", S.MIGRATION_0009.replace("CREATE INDEX records_by_supersedes ON records (supersedes_id);", "CREATE INDEX x ON no_such_table (y);")),)
    monkeypatch.setattr(S, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        Db.open(path)
    monkeypatch.undo()
    d3 = Db.open(path)
    try:
        assert d3.schema_version() == 9 and d3.status()["ok"] and d3.status()["foreign_keys"] == 1, "실패한 시도는 되돌려지고 정상 마이그레이션이 다시 붙는다"
    finally:
        d3.close()


def test_record_input_validation_and_chain(db, tmp_path):
    r1 = apply_record_input(db, load_record_input(write(tmp_path, target())))
    assert r1.record_type == "target_price" and r1.dataset_version == 2
    # 수정: 사유 필요, 이전 기록은 같은 물건·종류, 이미 수정된 기록은 다시 가리킬 수 없다
    with pytest.raises(ValidationError) as e:
        apply_record_input(db, target(price=1_400_000_000, decided="2026-09-10", supersedes_id=r1.record_id))
    assert "revision_reason" in str(e.value)
    p2 = target(price=1_400_000_000, decided="2026-09-10", supersedes_id=r1.record_id)
    p2["payload"]["revision_reason"] = "인근 거래 하향"
    r2 = apply_record_input(db, p2)
    assert db.get_record(r2.record_id)["supersedes_id"] == r1.record_id
    with pytest.raises(ValidationError) as e:
        apply_record_input(db, {**p2, "observed_at": "2026-09-11", "payload": {**p2["payload"], "decided_on": "2026-09-11"}})
    assert "이미 다른 기록으로 수정" in str(e.value)
    with pytest.raises(ValidationError) as e:
        apply_record_input(db, judgment(supersedes_id=r1.record_id))
    assert "같은 물건·같은 종류" in str(e.value)
    with pytest.raises(ValidationError):
        apply_record_input(db, target(asset_id="7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"))
    with pytest.raises(ValidationError) as e:
        apply_record_input(db, target(evidence=[{"document_id": "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"}]))
    assert "근거 문서" in str(e.value)
    # 입력 파일 검증: 스키마, 판단일 불일치, 존재하지 않는 날짜, formal 10항목, 승인은 formal 만, 철회는 이전 기록 필요
    bad_cases = [
        ({**target(), "kind": "x"}, "judgment_records.schema"),
        (target(decided="2026-09-01", observed_at="2026-09-02"), "같은 판단일"),
        (target(decided="2026-02-30"), "date"),
        (judgment(status="formal", **{"payload": {**judgment(status="formal")["payload"], "demand": None}}), "judgment_records.schema"),
        (judgment(status="draft", decision="approved"), "judgment_records.schema"),
    ]
    for doc, word in bad_cases:
        with pytest.raises(ValidationError) as e:
            load_record_input(write(tmp_path, doc))
        assert word in str(e.value), (doc["record_type"], str(e.value))
    with pytest.raises(ValidationError) as e:
        apply_record_input(db, judgment(status="formal", decision="withdrawn"))
    assert "철회" in str(e.value)
    # 근거 문서 연결과 정식 판단 승인 → 철회 체인
    j1 = apply_record_input(db, judgment(status="formal", decision="approved", evidence=[{"document_id": DOC, "field_path": "$.strategy", "verification_status": "verified"}]))
    assert db.conn.execute("SELECT COUNT(*) FROM record_evidence WHERE record_id = ?", (j1.record_id,)).fetchone()[0] == 1
    j2 = apply_record_input(db, judgment(status="formal", decision="withdrawn", decided="2026-09-20", supersedes_id=j1.record_id))
    assert db.get_record(j2.record_id)["supersedes_id"] == j1.record_id and db.get_record(j1.record_id)["payload"]["decision"] == "approved", "원 기록은 그대로"
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.conn.execute("UPDATE records SET payload_json = '{}' WHERE record_id = ?", (j1.record_id,))


def test_asof_observation_vs_as_recorded(db, home, server, tmp_path):
    """T=2026-09-15 의 상태. 나중에(9/25) 넣은 정정·철회·과거 거래·취소는 K=9/15 당시 보기에 들어가지 않는다."""
    clock = {"now": "2026-09-05T00:00:00Z"}
    db.now = lambda: clock["now"]
    # 9/5: 현장 관측(9/3), 목표가 v1(9/1), 필지 연결(9/1~), 거래 연결 확정
    with db.transaction():
        db.insert_record({"record_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "subject_id": A[0], "subject_type": "asset", "record_type": "field_observation", "source_kind": "field_observation",
                          "schema_version": "1.0.0", "payload": {"change_status": "no_change", "note": None}, "observed_at": "2026-09-03", "observed_at_precision": "date"})
    r1 = apply_record_input(db, target())
    load_bundle(db, json.loads(BUNDLE.read_text(encoding="utf-8")))
    apply_links(db, {"kind": "asset_components", "links": [{"asset_id": A[0], "pnu": P1, "effective_from": "2026-09-01", "effective_to": None, "basis": "manual", "note": None}]})
    aug0 = {**item(0), "umdNm": "가상동", "jibun": "1", "dealYear": "2026", "dealMonth": "8", "dealDay": "10"}
    aug1 = {**item(1), "umdNm": "가상동", "jibun": "1", "dealYear": "2026", "dealMonth": "8", "dealDay": "12"}
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [aug0, aug1]}}
    run1 = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=["202608"], endpoint=server, sleep=lambda s: None)
    load_run(db, home, "11110", run1.run_id)
    t1 = db.conn.execute("SELECT transaction_id FROM transactions WHERE identity_hash = ?", (identity_hash(aug0),)).fetchone()[0]
    t1b = db.conn.execute("SELECT transaction_id FROM transactions WHERE identity_hash = ?", (identity_hash(aug1),)).fetchone()[0]
    apply_decisions(db, [{"_line": 2, "transaction_id": t1, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual", "reviewed_on": "2026-09-05"},
                         {"_line": 3, "transaction_id": t1b, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual", "reviewed_on": "2026-09-05"}])
    # 9/25: 목표가 v2(판단일 9/10), 관측 정정(9/3 기록을 정정), 두 번째 8월 거래의 연결 철회, 과거 거래(7월) 추가 반영·확정, 첫 8월 거래에 취소 표시
    clock["now"] = "2026-09-25T00:00:00Z"
    p2 = target(price=1_400_000_000, decided="2026-09-10", supersedes_id=r1.record_id)
    p2["payload"]["revision_reason"] = "인근 거래 하향"
    apply_record_input(db, p2)
    with db.transaction():
        db.insert_record({"record_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2", "subject_id": A[0], "subject_type": "asset", "record_type": "field_observation", "source_kind": "field_observation",
                          "schema_version": "1.0.0", "payload": {"change_status": "change_observed", "note": "정정: 간판 교체"}, "observed_at": "2026-09-03", "observed_at_precision": "date",
                          "supersedes_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"})
    apply_decisions(db, [{"_line": 2, "transaction_id": t1b, "decision": "withdrawn", "reviewed_on": "2026-09-25", "note": "지분 거래로 확인"}])
    _Handler.scenarios = {"202607": {"kind": "pages", "items": [{**item(3), "umdNm": "가상동", "jibun": "1", "dealYear": "2026", "dealMonth": "7", "dealDay": "2"}]},
                          "202608": {"kind": "pages", "items": [{**aug0, "cdealType": "O", "cdealDay": "26.09.20"}, aug1]}}
    run2 = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=["202607", "202608"], endpoint=server, sleep=lambda s: None)
    load_run(db, home, "11110", run2.run_id)
    t7 = db.conn.execute("SELECT transaction_id FROM transactions WHERE deal_ymd = '202607'").fetchone()[0]
    apply_decisions(db, [{"_line": 2, "transaction_id": t7, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual", "reviewed_on": "2026-09-25"}])
    # 관측 기준 (현재 보유 근거 전부): 정정된 관측, 목표가 v2(판단일 9/10 ≤ T), 7월 거래와 첫 8월 거래(취소 표시 있음). 철회된 두 번째 8월 거래는 없다
    obs = asof(db, A[0], "2026-09-15")
    assert obs["observation"]["payload"]["note"] == "정정: 간판 교체" and obs["target_price"]["payload"]["price_krw"] == 1_400_000_000 and obs["target_price_chain"] == 2
    assert sorted(l["transaction_id"] for l in obs["transactions"]) == sorted([t7, t1]) and next(l for l in obs["transactions"] if l["transaction_id"] == t1)["cancelled_known"] is True
    assert len(obs["parcels"]) == 1 and obs["parcels"][0]["pnu"] == P1
    # 당시 기록 기준 K=T=9/15: 원 관측, 목표가 v1, 8월 거래 둘 다 확정 상태(철회는 9/25), 취소 표시 없음, 7월 거래는 아직 모름
    rec = asof(db, A[0], "2026-09-15", known_by="2026-09-15")
    assert rec["observation"]["payload"]["change_status"] == "no_change" and rec["observation"]["record_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    assert rec["target_price"]["payload"]["price_krw"] == 1_500_000_000 and rec["target_price_chain"] == 1
    assert sorted(l["transaction_id"] for l in rec["transactions"]) == sorted([t1, t1b]) and all(l["cancelled_known"] is False for l in rec["transactions"])
    assert rec["parcels"][0]["boundary_note"] is None, "도형 기준일 2026-09-01 ≤ T 이므로 당시 경계"
    text = asof_text(rec)
    assert "당시 기록 기준" in text and "1,500,000,000원" in text and "마지막 확인일 2026-09-03" in text
    # K=9/25 (모든 기록 반영) 이지만 T=9/15: 목표가 v2 는 판단일 9/10 이라 들어오고, 7월 거래는 확정, 두 번째 8월 거래는 철회돼 빠지고, 첫 8월 거래의 취소 표시를 안다
    late = asof(db, A[0], "2026-09-15", known_by="2026-09-25")
    assert late["target_price"]["payload"]["price_krw"] == 1_400_000_000 and sorted(l["transaction_id"] for l in late["transactions"]) == sorted([t7, t1])
    assert next(l for l in late["transactions"] if l["transaction_id"] == t1)["cancelled_known"] is True
    # T 를 9/2 로 두면 관측·목표가·연결 모두 아직 없다 (판단일 9/1 목표가는 있음)
    early = asof(db, A[0], "2026-09-02", known_by="2026-09-02")
    assert early["observation"] is None and early["target_price"] is None and early["transactions"] == [] and early["parcels"] == [], "9/2 에는 아무것도 기록되지 않았다(기록일 9/5)"
    early_obs = asof(db, A[0], "2026-09-02")
    assert early_obs["target_price"]["payload"]["decided_on"] == "2026-09-01" and early_obs["observation"] is None and early_obs["records_after_at"] >= 1
    # 도형 기준일이 T 보다 뒤인 필지: 현재 경계 위의 과거 속성
    b3 = json.loads(BUNDLE.read_text(encoding="utf-8"))
    b3["source"]["geometry_version"] = "2026-10-01"
    load_bundle(db, b3)
    later = asof(db, A[0], "2026-09-15")
    assert later["parcels"][0]["boundary_note"] and any("현재 경계" in n for n in later["notes"])
    with pytest.raises(ValidationError):
        asof(db, A[0], "2026-13-01")
    with pytest.raises(ValidationError):
        asof(db, "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99", "2026-09-15")


def test_cli_record_add_and_asof(db, tmp_path, capsys, monkeypatch):
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(tmp_path / "home"))
    f = write(tmp_path, target())
    rc = cli.main(["db", "record-add", str(f)])
    out = capsys.readouterr()
    assert rc == 0 and "기록 추가: target_price" in out.out
    j = write(tmp_path, judgment(status="formal", decision="approved"), "j.json")
    assert cli.main(["db", "record-add", str(j), "--json"]) == 0
    rid = json.loads(capsys.readouterr().out)["record_id"]
    assert cli.main(["db", "record-add", str(write(tmp_path, {**target(), "kind": "nope"}, "bad.json"))]) == 1
    assert "입력 거절" in capsys.readouterr().err
    assert cli.main(["db", "record-add", str(tmp_path / "none.json")]) == cli.USAGE_ERROR
    capsys.readouterr()
    rc = cli.main(["db", "asof", "--asset", A[0], "--at", "2026-09-30", "--as-recorded", "--json"])
    a = json.loads(capsys.readouterr().out)
    assert rc == 0 and a["known_by"] == "2026-09-30" and a["target_price"]["payload"]["price_krw"] == 1_500_000_000 and a["judgment"]["record_id"] == rid
    rc = cli.main(["db", "asof", "--asset", A[0], "--at", "2026-08-01"])
    out = capsys.readouterr().out
    assert rc == 0 and "관측 기준" in out and "목표 매수가: 없음" in out
    assert cli.main(["db", "asof", "--asset", A[0], "--at", "2026-02-30"]) == 1
