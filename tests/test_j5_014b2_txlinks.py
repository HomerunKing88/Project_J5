"""J5-014B-2: 거래↔물건 연결 후보 CSV·수동 연결·철회 (데이터 사전 §7.2·§2 review_decisions·§8, db_schema 7).

시험: 마이그레이션·상태 행 수, 지번 해석·대조(정확/앞자리/산/부번 마스킹), 후보 CSV(취소 제외·후보 물건·이미 연결·필터), 결정 CSV 반영(후보→확정, 앞자리만으로 확정 금지,
철회, 다른 물건으로 대체, 변화 없음, 전체 거절, 거래 link_status·범위 동기화, 검토 결정 이력 불변), CLI 왕복, 백업·복구 포함. 가상자료만 쓴다.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest

from j5 import cli
from j5.collect import rt
from j5.collect.rt import collect_months
from j5.db import schema as S
from j5.db.backup import create_backup, restore_backup
from j5.db.parcels import apply_links, load_bundle, suggest_links
from j5.db.store import Db
from j5.db.transactions import coverage, identity_hash, load_run
from j5.db.txlinks import CSV_COLUMNS, apply_decisions, candidates, candidates_csv, candidates_text, jibun_matches, link_history, parse_jibun, read_decisions_csv
from j5.db.validate import ValidationError
from tests.test_j5_014_collect import KEY, _Handler, item, server  # noqa: F401

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
BUNDLE = Path(__file__).resolve().parent / "fixtures" / "parcels" / "synthetic.j5parcels.json"
A = [f"7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a5{i}" for i in range(5)]
P42 = "9999900100100040002"


@pytest.fixture(autouse=True)
def sequential_run_ids(monkeypatch):
    counter = iter(range(1, 10_000))
    monkeypatch.setattr(rt, "_new_run_id", lambda: f"20260925-130000-{next(counter):06x}")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def db(home):
    """시드 5물건 + 가상 필지 6개 + 위치점 연결 4건(물건1↔1, 물건2↔1-1, 물건4↔2, 물건5↔3) + 수동 연결(물건3↔4-2)."""
    d = Db.create(home / "db" / "j5.sqlite3", study_id="j5-synthetic-study", data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    load_bundle(d, json.loads(BUNDLE.read_text(encoding="utf-8")))
    sug = suggest_links(d, effective_from="2026-09-01")
    doc = {k: v for k, v in sug.items() if not k.startswith("_")}
    doc["links"].append({"asset_id": A[2], "pnu": P42, "effective_from": "2026-09-01", "effective_to": None, "basis": "manual", "note": "현장 확인"})
    apply_links(d, doc)
    yield d
    d.close()


def row(i: int, **over) -> dict:
    it = item(i)
    it.update({"sggCd": "11110", "umdNm": "가상동", "landUse": "일반상업", "shareDealingType": "", "cdealDay": "", "estateAgentSggNm": "", "buyerGbn": "", "slerGbn": ""})
    it.update(over)
    return it


def load_month(db, home, server, items, ym="202608"):
    _Handler.scenarios = {ym: {"kind": "pages", "items": items}}
    run = collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=[ym], endpoint=server, sleep=lambda s: None)
    load_run(db, home, "11110", run.run_id)
    return run


def tid_of(db, r: dict) -> str:
    return db.conn.execute("SELECT transaction_id FROM transactions WHERE identity_hash = ? AND ordinal = 0", (identity_hash(r),)).fetchone()[0]


def test_schema_7_and_jibun_matching(db):
    st = db.status()
    assert st["db_schema_version"] == S.DB_SCHEMA_VERSION >= 7 and st["counts"]["transaction_links"] == 0 and st["counts"]["review_decisions"] == 0
    assert parse_jibun("160") == {"mountain": False, "bon": 160, "bu": 0, "masked": False}
    assert parse_jibun("160-3")["bu"] == 3 and parse_jibun("산12-1") == {"mountain": True, "bon": 12, "bu": 1, "masked": False}
    assert parse_jibun("1**") == {"mountain": False, "masked": True, "bon_prefix": "1", "bon_digits": 3, "bu_prefix": "0", "bu_digits": None}
    assert parse_jibun("1-*")["bu_digits"] == 1 and parse_jibun("*")["bon_prefix"] == "" and parse_jibun("") is None and parse_jibun("abc") is None
    p160, p163, p16, p1, p11, m12 = ({"mountain": 0, "bon": 160, "bu": 0}, {"mountain": 0, "bon": 160, "bu": 3}, {"mountain": 0, "bon": 16, "bu": 0},
                                     {"mountain": 0, "bon": 1, "bu": 0}, {"mountain": 0, "bon": 1, "bu": 1}, {"mountain": 1, "bon": 12, "bu": 1})
    assert jibun_matches(parse_jibun("160"), p160) == "jibun_exact" and jibun_matches(parse_jibun("160"), p163) is None
    assert jibun_matches(parse_jibun("1**"), p160) == "jibun_prefix" and jibun_matches(parse_jibun("1**"), p163) is None, "부번 없는 마스킹은 부번 0 만"
    assert jibun_matches(parse_jibun("1**"), p16) is None, "자릿수가 다르면 범위 밖"
    assert jibun_matches(parse_jibun("1**-*"), p163) == "jibun_prefix" and jibun_matches(parse_jibun("16*-3"), p163) == "jibun_prefix"
    assert jibun_matches(parse_jibun("1-*"), p1) == "jibun_prefix" and jibun_matches(parse_jibun("1-*"), p11) == "jibun_prefix"
    assert jibun_matches(parse_jibun("산12-1"), m12) == "jibun_exact" and jibun_matches(parse_jibun("12-1"), m12) is None, "산 여부가 다르면 다른 필지"
    assert jibun_matches(parse_jibun("*"), p1) is None and jibun_matches(None, p1) is None


def test_candidates_csv_lists_current_deals_with_matches(db, home, server):
    items = [row(0, jibun="1"), row(1, jibun="1-1"), row(2, jibun="1-*"), row(3, jibun="산1-2"), row(4, jibun="9"), row(5, jibun="*"),
             row(6, jibun="1", cdealType="O", cdealDay="26.08.30"), row(7, jibun="4-2", umdNm="다른동")]
    load_month(db, home, server, items)
    rows = candidates(db, "11110", ["202608"])
    assert len(rows) == 7, "취소 확정 거래는 후보 목록에 없다"
    by = {r["jibun"] + "@" + r["emd_name"]: r for r in rows}
    assert by["1@가상동"]["candidate_asset_ids"] == A[0] and by["1@가상동"]["candidate_basis"] == "jibun_exact" and "가상 물건 1" in by["1@가상동"]["candidate_labels"]
    assert by["1-1@가상동"]["candidate_asset_ids"] == A[1]
    assert set(by["1-*@가상동"]["candidate_asset_ids"].split(";")) == {A[0], A[1]} and by["1-*@가상동"]["candidate_basis"] == "jibun_prefix;jibun_prefix"
    assert by["산1-2@가상동"]["candidate_asset_ids"] == "" , "산1-2 필지는 어느 물건에도 연결되지 않았다"
    assert by["9@가상동"]["candidate_asset_ids"] == "" and by["*@가상동"]["candidate_asset_ids"] == ""
    assert by["4-2@다른동"]["candidate_asset_ids"] == "", "법정동이 다르면 지번이 같아도 후보가 아니다"
    assert all(r["decision"] == "" and r["asset_id"] == "" and r["link_status"] == "unlinked" for r in rows)
    text = candidates_csv(rows)
    assert text.splitlines()[0] == ",".join(CSV_COLUMNS)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == 7 and parsed[0]["amount_krw"] and parsed[0]["building_kind"] == "general"
    assert "거래 후보 7행" in candidates_text(rows) and "지번 일치 2" in candidates_text(rows)
    assert [r["jibun"] for r in candidates(db, "11110", ["202608"], emd_names=["다른동"])] == ["4-2"]
    assert candidates(db, "11110", ["202607"]) == []
    assert db.status()["counts"]["transaction_links"] == 0, "후보는 정본에 쓰지 않는다"


def test_apply_decisions_confirm_withdraw_supersede_and_history(db, home, server):
    load_month(db, home, server, [row(0, jibun="1"), row(2, jibun="1-*"), row(4, jibun="9"), row(6, jibun="1", cdealType="O", cdealDay="26.08.30")])
    t1, t1m, t9, tc = tid_of(db, row(0, jibun="1")), tid_of(db, row(2, jibun="1-*")), tid_of(db, row(4, jibun="9")), tid_of(db, row(6, jibun="1", cdealType="O", cdealDay="26.08.30"))
    rows = candidates(db, "11110", ["202608"])
    for r in rows:
        r["_line"] = 2
    dec = {r["transaction_id"]: r for r in rows}
    dec[t1].update({"decision": "confirmed", "asset_id": A[0], "basis_kind": "jibun_exact", "reviewed_on": "2026-09-25", "note": "지번 일치"})
    dec[t1m].update({"decision": "pending_evidence", "asset_id": A[0], "basis_kind": "jibun_prefix", "decision_scope": "land_only"})
    r = apply_decisions(db, rows)
    assert r.outcome == "applied" and r.decided == 2 and r.inserted == 2 and r.dataset_version == db.status()["dataset_version"] and db.status()["ok"]
    links = {l["transaction_id"]: dict(l) for l in db.conn.execute("SELECT * FROM transaction_links")}
    assert links[t1]["status"] == "confirmed" and links[t1]["asset_id"] == A[0] and links[t1]["scope"] == "unclear" and links[t1]["reviewed_on"] == "2026-09-25"
    assert links[t1m]["status"] == "pending_evidence" and links[t1m]["scope"] == "land_only" and links[t1m]["reviewed_on"] == db.now()[:10], "검토일 생략 시 오늘"
    tx = {t["transaction_id"]: dict(t) for t in db.conn.execute("SELECT * FROM transactions")}
    assert tx[t1]["link_status"] == "confirmed" and tx[t1]["scope"] == "unclear" and tx[t1]["scope_basis"] == "auto_provider_fields"
    assert tx[t1m]["link_status"] == "pending_evidence" and tx[t1m]["scope"] == "land_only" and tx[t1m]["scope_basis"] == "manual_review"
    assert coverage(db, "11110", ["202608"])["linked"] == 1
    assert candidates(db, "11110", ["202608"], unlinked_only=True)[0]["transaction_id"] == t9
    assert {r["transaction_id"]: r["linked_asset_id"] for r in candidates(db, "11110", ["202608"])}[t1] == A[0]
    # 같은 결정 다시 → 변화 없음 (검토일을 비우면 멱등). 검토일을 명시해 바꾸면 갱신이고 결정 이력에 남는다 (Codex P2)
    assert apply_decisions(db, rows).outcome == "unchanged"
    r_same = apply_decisions(db, [{"_line": 2, "transaction_id": t1, "decision": "confirmed", "asset_id": A[0], "basis_kind": "jibun_exact", "reviewed_on": "2026-09-25", "note": "지번 일치"}])
    assert r_same.outcome == "unchanged"
    r_date = apply_decisions(db, [{"_line": 2, "transaction_id": t1, "decision": "confirmed", "asset_id": A[0], "basis_kind": "jibun_exact", "reviewed_on": "2026-10-01", "note": "지번 일치"}])
    assert r_date.updated == 1 and db.conn.execute("SELECT reviewed_on FROM transaction_links WHERE transaction_id = ? AND status <> 'withdrawn'", (t1,)).fetchone()[0] == "2026-10-01"
    assert [h["decided_on"] for h in link_history(db, t1)] == ["2026-09-25", "2026-10-01"]
    # 앞자리 범위만으로 confirmed 는 거절, 취소 거래 확정 거절, 없는 물건·거래, 잘못된 값, 철회할 연결 없음, 같은 거래 두 번: 전체 거절이라 정본 무변화
    ver = db.status()["dataset_version"]
    bad_cases = [
        ({"transaction_id": t1m, "decision": "confirmed", "asset_id": A[0], "basis_kind": "jibun_prefix"}, "jibun_prefix"),
        ({"transaction_id": tc, "decision": "confirmed", "asset_id": A[0]}, "취소 확정"),
        ({"transaction_id": t9, "decision": "confirmed", "asset_id": "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"}, "정본에 없다"),
        ({"transaction_id": "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99", "decision": "confirmed", "asset_id": A[0]}, "정본에 없다"),
        ({"transaction_id": t9, "decision": "maybe", "asset_id": A[0]}, "decision"),
        ({"transaction_id": t9, "decision": "confirmed", "asset_id": A[0], "decision_scope": "everything"}, "decision_scope"),
        ({"transaction_id": t9, "decision": "confirmed", "asset_id": A[0], "reviewed_on": "2026-13-01"}, "reviewed_on"),
        ({"transaction_id": t9, "decision": "confirmed"}, "asset_id"),
        ({"transaction_id": t9, "decision": "withdrawn"}, "철회할"),
    ]
    for case, word in bad_cases:
        with pytest.raises(ValidationError) as e:
            apply_decisions(db, [{"_line": 2, **case}])
        assert word in str(e.value), (case, str(e.value))
    with pytest.raises(ValidationError) as e:
        apply_decisions(db, [{"_line": 2, "transaction_id": t9, "decision": "confirmed", "asset_id": A[0]}, {"_line": 3, "transaction_id": t9, "decision": "candidate", "asset_id": A[1]}])
    assert "두 번" in str(e.value) and db.status()["dataset_version"] == ver and db.status()["counts"]["transaction_links"] == 2
    # 다른 물건으로 대체: 이전 연결 철회(superseded_by) + 새 연결
    r2 = apply_decisions(db, [{"_line": 2, "transaction_id": t1, "decision": "confirmed", "asset_id": A[1], "basis_kind": "document", "reviewed_on": "2026-09-26", "note": "등기 확인"}])
    assert r2.inserted == 1 and r2.superseded == 1 and r2.withdrawn == 0
    old = db.conn.execute("SELECT * FROM transaction_links WHERE link_id = ?", (links[t1]["link_id"],)).fetchone()
    new = db.conn.execute("SELECT * FROM transaction_links WHERE transaction_id = ? AND status <> 'withdrawn'", (t1,)).fetchone()
    assert old["status"] == "withdrawn" and old["withdrawn_on"] == "2026-09-26" and old["superseded_by"] == new["link_id"] and "대체" in old["withdrawn_reason"]
    assert new["asset_id"] == A[1] and new["basis_kind"] == "document" and db.conn.execute("SELECT link_status FROM transactions WHERE transaction_id = ?", (t1,)).fetchone()[0] == "confirmed"
    # 철회
    r3 = apply_decisions(db, [{"_line": 2, "transaction_id": t1, "decision": "withdrawn", "reviewed_on": "2026-09-27", "note": "매매 아님"}])
    assert r3.withdrawn == 1 and db.conn.execute("SELECT link_status FROM transactions WHERE transaction_id = ?", (t1,)).fetchone()[0] == "unlinked"
    assert db.conn.execute("SELECT COUNT(*) FROM transaction_links WHERE transaction_id = ? AND status <> 'withdrawn'", (t1,)).fetchone()[0] == 0
    with pytest.raises(ValidationError):
        apply_decisions(db, [{"_line": 2, "transaction_id": t1, "decision": "withdrawn", "asset_id": A[0]}])  # 유효한 연결이 없다
    # 검토 결정 이력: 확정 → 철회(대체) / 확정 → 철회, 이전 결정 연결, 불변
    hist = link_history(db, t1)
    assert [h["decision"] for h in hist] == ["confirmed", "confirmed", "withdrawn", "confirmed", "withdrawn"] and hist[2]["previous_decision_id"] == hist[1]["decision_id"]
    assert hist[3]["previous_decision_id"] is None and hist[4]["previous_decision_id"] == hist[3]["decision_id"] and hist[4]["rationale"] == "매매 아님"
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.conn.execute("DELETE FROM review_decisions")
    # 상태만 바꾸는 갱신 (같은 물건): pending → confirmed with manual basis
    r4 = apply_decisions(db, [{"_line": 2, "transaction_id": t1m, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual", "note": "현장 확인"}])
    assert r4.updated == 1 and r4.inserted == 0 and [h["decision"] for h in link_history(db, t1m)] == ["pending_evidence", "confirmed"]
    assert coverage(db, "11110", ["202608"])["linked"] == 1
    # 백업·복구에 새 테이블 포함
    b = create_backup(db, home)
    assert b.outcome == "completed" and b.counts["transaction_links"] == 3 and b.counts["review_decisions"] == 7
    rr = restore_backup(home / b.backup_dir, home / "restored")
    assert rr.outcome == "completed" and rr.counts["review_decisions"] == 7


def test_apply_plans_inside_write_transaction(db, home, server):
    """계획(유효 연결 읽기)이 BEGIN IMMEDIATE 뒤에 이뤄진다: 같은 연결의 쓰기 트랜잭션 안에서 부르면 중첩(SAVEPOINT)으로 들어가고,
    오류 행이 있으면 바깥 트랜잭션의 이전 쓰기는 남고 이 단위의 쓰기만 되돌아간다 (Codex P2)."""
    load_month(db, home, server, [row(0, jibun="1"), row(4, jibun="9")])
    t1, t9 = tid_of(db, row(0, jibun="1")), tid_of(db, row(4, jibun="9"))
    calls = []
    orig = db.transaction

    def spy():
        calls.append(db._depth)
        return orig()
    db.transaction = spy  # type: ignore[assignment]
    apply_decisions(db, [{"_line": 2, "transaction_id": t1, "decision": "confirmed", "asset_id": A[0], "basis_kind": "manual"}])
    assert calls and calls[0] == 0, "반영은 바깥 쓰기 트랜잭션을 연다"
    with db.transaction():
        db.conn.execute("UPDATE transactions SET floor_raw = 'x' WHERE transaction_id = ?", (t9,))
        with pytest.raises(ValidationError):
            apply_decisions(db, [{"_line": 2, "transaction_id": t9, "decision": "withdrawn"}])
    assert db.conn.execute("SELECT floor_raw FROM transactions WHERE transaction_id = ?", (t9,)).fetchone()[0] == "x"
    assert db.conn.execute("SELECT COUNT(*) FROM transaction_links WHERE transaction_id = ?", (t9,)).fetchone()[0] == 0


def test_cli_candidates_and_link_roundtrip(db, home, server, capsys, monkeypatch):
    load_month(db, home, server, [row(0, jibun="1"), row(1, jibun="1-1")])
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    out_csv = home / "candidates.csv"
    rc = cli.main(["db", "rt-candidates", "--lawd-cd", "11110", "--from", "2026-08", "--to", "2026-08", "--out", str(out_csv)])
    out = capsys.readouterr()
    assert rc == 0 and out_csv.is_file() and "거래 후보 2행" in out.out
    assert cli.main(["db", "rt-candidates", "--lawd-cd", "11110", "--from", "2026-08", "--to", "2026-08", "--out", str(out_csv)]) == cli.USAGE_ERROR, "덮어쓰지 않는다"
    capsys.readouterr()
    rows = read_decisions_csv(out_csv)
    assert len(rows) == 2 and all(r["decision"] == "" for r in rows)
    # 스프레드시트에서 편집한 것처럼 결정 열을 채워 저장 (BOM 포함 UTF-8)
    for r in rows:
        if r["jibun"] == "1":
            r.update({"decision": "confirmed", "asset_id": r["candidate_asset_ids"], "basis_kind": r["candidate_basis"], "reviewed_on": "2026-09-25", "note": "지번 일치"})
    edited = home / "decided.csv"
    with open(edited, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_COLUMNS})
    rc = cli.main(["db", "rt-link", str(edited)])
    out = capsys.readouterr()
    assert rc == 0 and "신규 1" in out.out and "결정 있는 행 1" in out.out
    rc = cli.main(["db", "rt-coverage", "--lawd-cd", "11110", "--from", "2026-08", "--to", "2026-08", "--json"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["linked"] == 1
    # 잘못된 CSV: 머리글 없음 → 거절 (종료 코드 1), 없는 파일 → 1
    bad = home / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    assert cli.main(["db", "rt-link", str(bad)]) == 1 and "머리글" in capsys.readouterr().err
    assert cli.main(["db", "rt-link", str(home / "none.csv")]) == 1
    capsys.readouterr()
    rc = cli.main(["db", "rt-candidates", "--lawd-cd", "11110", "--from", "2026-08", "--to", "2026-08", "--json", "--emd", "가상동", "--unlinked-only"])
    js = json.loads(capsys.readouterr().out)
    assert rc == 0 and len(js) == 1 and js[0]["jibun"] == "1-1"
    rc = cli.main(["db", "rt-candidates", "--lawd-cd", "11110", "--from", "2026-08", "--to", "2026-08"])
    assert rc == 0 and capsys.readouterr().out.startswith("transaction_id,")
