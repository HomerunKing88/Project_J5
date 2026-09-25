"""J5-014B-1: 정본 거래 원본·정규화·수집 기록 (데이터 사전 §7.1·§7.2, db_schema 6).

시험: 마이그레이션·상태 행 수, 실행 기록 반영(수집 기록·페이지·원본 행 불변·정규화), 같은 실행 재반영 없음, 같은 값의 별개 행을 순번으로 구분,
같은 달 재수집(다시 확인·취소 갱신·사라짐 표시·신규), 오래된 실행 반영, 원본 해시 불일치·손상 기록 거절(정본 무변화), data_mode 보호,
결측 null+사유, 범위 현황, rt-fetch 의 건너뛰기, CLI. 로컬 가짜 서버만 쓴다.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from j5 import cli
from j5.collect import rt
from j5.collect.rt import collect_months, month_range, months_done_on_disk
from j5.db import schema as S
from j5.db.backup import check_raw_files, create_backup, restore_backup, verify_backup_dir
from j5.db.store import Db, DbError
from j5.db.transactions import IDENTITY_FIELDS, assign_ordinals, coverage, coverage_text, identity_hash, is_real_endpoint, load_run, normalize, unloaded_run_ids
from tests.test_j5_014_collect import KEY, _Handler, item, server  # noqa: F401  (server 픽스처 재사용)

STUDY = "j5-synthetic-study"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture(autouse=True)
def sequential_run_ids(monkeypatch):
    """실제 실행은 초 단위 UTC 시각으로 시작하는 ID 라 이름순이 시간순이지만, 테스트는 한 초 안에 여러 실행을 만들므로 순서를 고정한다."""
    counter = iter(range(1, 10_000))
    monkeypatch.setattr(rt, "_new_run_id", lambda: f"20260925-120000-{next(counter):06x}")


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    yield d
    d.close()


def fetch(home, server, months, **kw):
    return collect_months(home, key=KEY, key_source="test", lawd_cd="11110", months=months, endpoint=server, sleep=lambda s: None, **kw)


def row(i: int, **over) -> dict:
    it = item(i)
    it.update({"sggCd": "11110", "landUse": "일반상업", "shareDealingType": "", "cdealDay": "", "estateAgentSggNm": "", "buyerGbn": "", "slerGbn": ""})
    it.update(over)
    return it


def test_schema_6_tables_and_status_counts(db):
    st = db.status()
    assert st["db_schema_version"] == S.DB_SCHEMA_VERSION >= 6
    for t in ("collection_runs", "transaction_observations", "transactions"):
        assert st["counts"][t] == 0
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(transactions)")}
    for c in ("identity_hash", "ordinal", "amount_krw", "missing_reasons_json", "jibun_masked", "building_kind", "cancel_status", "scope", "link_status", "missing_since_run_id"):
        assert c in cols, c


def test_normalize_values_and_missing_reasons():
    n = normalize(row(0, dealAmount="1,234", dealYear="2026", dealMonth="8", dealDay="3", buildingAr="50.5", plottageAr="", buildYear="1985", jibun="1**",
                      buildingType="일반", shareDealingType="", cdealType="", floor=""))
    assert n["amount_krw"] == 12_340_000 and n["deal_date"] == "2026-08-03" and n["building_area_m2"] == 50.5 and n["plottage_area_m2"] is None and n["build_year"] == 1985
    assert json.loads(n["missing_reasons_json"]) == {"plottageAr": "not_provided"}
    assert n["jibun_masked"] == 1 and n["jibun_prefix"] == "1" and n["building_kind"] == "general" and n["scope"] == "unclear" and n["cancel_status"] == "none" and n["floor_raw"] is None
    n2 = normalize(row(0, dealAmount="abc", dealYear="2021", dealMonth="2", dealDay="30", buildingType="집합", jibun="160", cdealType="O", cdealDay="21.10.15", shareDealingType="지분", buildYear="99"))
    assert n2["amount_krw"] is None and n2["deal_date"] is None and n2["build_year"] is None
    assert json.loads(n2["missing_reasons_json"]) == {"dealAmount": "unparsable", "dealDate": "unparsable", "buildYear": "unparsable"}
    assert n2["jibun_masked"] == 0 and n2["jibun_prefix"] == "160" and n2["building_kind"] == "strata" and n2["share_deal"] == 1 and n2["scope"] == "partial_share", "지분이면 집합이라도 partial_share"
    assert n2["cancel_status"] == "cancelled" and n2["cancel_date"] == "2021-10-15"
    n3 = normalize(row(0, buildingType="집합", cdealType="O", cdealDay=""))
    assert n3["scope"] == "strata_unit" and n3["cancel_status"] == "cancelled" and n3["cancel_date"] is None and json.loads(n3["missing_reasons_json"])["cdealDay"] == "not_provided"
    assert normalize(row(0, buildingType="기타"))["building_kind"] == "unknown"
    # 식별 해시는 가변 필드를 무시한다
    a, b = row(1), row(1, cdealType="O", cdealDay="26.09.01", dealingGbn="직거래", buyerGbn="법인")
    assert identity_hash(a) == identity_hash(b) and identity_hash(a) != identity_hash(row(2))
    assert "cdealType" not in IDENTITY_FIELDS and "dealAmount" in IDENTITY_FIELDS
    assert is_real_endpoint("https://apis.data.go.kr/1613000/x/y") and not is_real_endpoint("http://127.0.0.1:8/api")


def test_load_run_records_observations_and_transactions(db, home, server):
    items = [row(0), row(1), row(2), row(2), row(3, cdealType="O", cdealDay="26.08.20")]  # row(2) 두 번: 같은 값의 별개 행
    _Handler.scenarios = {"202608": {"kind": "pages", "items": items}, "200603": {"kind": "pages", "items": []}, "201501": {"kind": "api_error"}}
    run = fetch(home, server, ["202608", "200603", "201501"], num_rows=2)
    r = load_run(db, home, "11110", run.run_id)
    assert r.outcome == "applied" and r.months == 3 and r.pages == 5 and r.observations == 5 and r.transactions_new == 5 and r.transactions_seen == 0 and r.dataset_version == 1
    st = db.status()
    assert st["ok"] and st["counts"]["collection_runs"] == 3 and st["counts"]["transaction_observations"] == 5 and st["counts"]["transactions"] == 5 and st["counts"]["source_documents"] == 1
    runs = {r_["deal_ymd"]: dict(r_) for r_ in db.conn.execute("SELECT * FROM collection_runs")}
    assert runs["202608"]["outcome"] == "complete" and runs["202608"]["total_count"] == 5 and runs["202608"]["pages"] == 3
    assert runs["200603"]["outcome"] == "empty" and runs["201501"]["outcome"] == "failed" and runs["201501"]["items"] == 0
    pages = db.conn.execute("SELECT * FROM collection_pages WHERE deal_ymd = '201501'").fetchall()
    assert len(pages) == 1 and pages[0]["outcome"] == "api_error" and pages[0]["raw_path"] is not None, "API 오류 응답도 원본 XML 로 남아 경로가 있다"
    ok_page = db.conn.execute("SELECT * FROM collection_pages WHERE deal_ymd = '202608' AND page_no = 1").fetchone()
    assert ok_page["item_count"] == 2 and ok_page["raw_sha256"] == run.months[0].pages[0].sha256 and json.loads(ok_page["fields_json"])["dealAmount"] == 2
    doc = db.conn.execute("SELECT * FROM source_documents").fetchone()
    assert doc["document_kind"] == "official_api" and doc["location"] == run.run_path and run.run_id in doc["title"]
    # 같은 값의 별개 행은 순번 0·1 로 두 거래
    dup = db.conn.execute("SELECT ordinal, amount_krw FROM transactions WHERE identity_hash = ? ORDER BY ordinal", (identity_hash(row(2)),)).fetchall()
    assert [d["ordinal"] for d in dup] == [0, 1] and dup[0]["amount_krw"] == 102_000 * 10000
    canc = db.conn.execute("SELECT cancel_status, cancel_date, scope, link_status FROM transactions WHERE identity_hash = ?", (identity_hash(row(3)),)).fetchone()
    assert canc["cancel_status"] == "cancelled" and canc["cancel_date"] == "2026-08-20" and canc["scope"] == "unclear" and canc["link_status"] == "unlinked"
    obs = db.conn.execute("SELECT * FROM transaction_observations WHERE row_index = 1 AND page_no = 1").fetchone()
    assert json.loads(obs["content_json"]) == row(1) and obs["ordinal"] == 0
    # 원본 행은 불변
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.conn.execute("UPDATE transaction_observations SET row_index = 9 WHERE observation_id = ?", (obs["observation_id"],))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.conn.execute("DELETE FROM transaction_observations WHERE observation_id = ?", (obs["observation_id"],))
    # 같은 실행 재반영 → 변화 없음
    r2 = load_run(db, home, "11110", run.run_id)
    assert r2.outcome == "unchanged" and r2.dataset_version == 1 and db.status()["counts"]["transactions"] == 5
    assert unloaded_run_ids(db, home, "11110") == []


def test_recollect_updates_seen_cancel_missing_and_new(db, home, server):
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(0), row(1), row(2)]}}
    run1 = fetch(home, server, ["202608"])
    load_run(db, home, "11110", run1.run_id)
    # 다시 받음: row(0) 그대로, row(1) 취소로 바뀜, row(2) 사라짐, row(5) 신규
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(0), row(1, cdealType="O", cdealDay="26.09.02", dealingGbn="직거래"), row(5)]}}
    run2 = fetch(home, server, ["202608"])
    r = load_run(db, home, "11110", run2.run_id)
    assert r.transactions_new == 1 and r.transactions_seen == 2 and r.transactions_changed == 1 and r.transactions_missing == 1 and r.dataset_version == 2
    t = {tr["identity_hash"]: dict(tr) for tr in db.conn.execute("SELECT * FROM transactions")}
    t0, t1, t2, t5 = (t[identity_hash(row(i))] for i in (0, 1, 2, 5))
    assert t0["first_seen_run_id"] == run1.run_id and t0["last_seen_run_id"] == run2.run_id and t0["missing_since_run_id"] is None
    assert t1["cancel_status"] == "cancelled" and t1["cancel_date"] == "2026-09-02" and t1["dealing_gbn_raw"] == "직거래" and t1["first_seen_run_id"] == run1.run_id
    assert t2["missing_since_run_id"] == run2.run_id and t2["cancel_status"] == "none", "응답에서 사라졌다고 취소로 확정하지 않는다"
    assert t5["first_seen_run_id"] == run2.run_id
    assert db.status()["counts"]["transaction_observations"] == 6 and db.status()["counts"]["transactions"] == 4, "원본 행은 실행마다 쌓이고 거래는 정체성으로 합쳐진다"
    assert t1["latest_observation_id"] != t1["first_observation_id"]
    # 세 번째 실행에서 row(2) 가 다시 보이면 사라짐 표시가 지워진다
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(0), row(1, cdealType="O", cdealDay="26.09.02", dealingGbn="직거래"), row(5), row(2)]}}
    run3 = fetch(home, server, ["202608"])
    r3 = load_run(db, home, "11110", run3.run_id)
    assert r3.transactions_new == 0 and r3.transactions_seen == 4 and r3.transactions_missing == 0
    assert db.conn.execute("SELECT missing_since_run_id FROM transactions WHERE identity_hash = ?", (identity_hash(row(2)),)).fetchone()[0] is None
    # 부분 응답(페이지 누락)에서는 사라짐을 표시하지 않는다
    _Handler.scenarios = {"202608": {"kind": "short_pages", "items": [row(0)], "claimed_total": 9}}
    run4 = fetch(home, server, ["202608"], num_rows=1)
    r4 = load_run(db, home, "11110", run4.run_id)
    assert run4.months[0].outcome == "partial" and r4.transactions_missing == 0 and r4.transactions_seen == 1
    assert db.conn.execute("SELECT COUNT(*) FROM transactions WHERE missing_since_run_id IS NOT NULL").fetchone()[0] == 0
    # 오래된 실행을 나중에 반영: 최신 상태를 건드리지 않는다 (run1 과 run2 사이에 만든 것처럼 기록 파일 이름을 바꿔 재현)
    old_id = run1.run_id[:15] + "-000000"
    raw_dir = home / "raw" / "rt_nrg" / "11110"
    src = json.loads((raw_dir / f"run-{run1.run_id}.json").read_text(encoding="utf-8"))
    src["run_id"] = old_id
    for m in src["months"]:
        for p in m["pages"]:
            new_name = Path(p["path"]).name.replace(run1.run_id, old_id)
            (home / p["path"]).replace(home / Path(p["path"]).with_name(new_name))
            p["path"] = str(Path(p["path"]).with_name(new_name).as_posix())
    (raw_dir / f"run-{old_id}.json").write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    assert old_id < run1.run_id
    r_old = load_run(db, home, "11110", old_id)
    assert r_old.transactions_new == 0 and r_old.transactions_seen == 3 and r_old.transactions_missing == 0
    t1b = db.conn.execute("SELECT * FROM transactions WHERE identity_hash = ?", (identity_hash(row(1)),)).fetchone()
    assert t1b["cancel_status"] == "cancelled" and t1b["last_seen_run_id"] == run3.run_id and t1b["first_seen_run_id"] == old_id, "부분 응답(run4)에는 row(1) 이 없었으므로 마지막 확인은 run3"


def test_duplicate_identity_ordinals_follow_content_not_order(db, home, server):
    """같은 값의 별개 행(순번 0·1)이 다음 수집에서 순서가 바뀌거나 하나만 취소돼도 취소 상태가 다른 거래에 붙지 않는다 (Codex P2)."""
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(2), row(2)]}}
    run1 = fetch(home, server, ["202608"])
    load_run(db, home, "11110", run1.run_id)
    # 두 번째 응답: 순서가 바뀌고 두 번째 행(원래 순번 0 자리)이 취소
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(2, cdealType="O", cdealDay="26.09.10"), row(2)]}}
    run2 = fetch(home, server, ["202608"])
    r2 = load_run(db, home, "11110", run2.run_id)
    assert r2.transactions_new == 0 and r2.transactions_seen == 2 and r2.transactions_changed == 1 and r2.transactions_missing == 0
    st = {t["ordinal"]: dict(t) for t in db.conn.execute("SELECT * FROM transactions")}
    assert sorted(st) == [0, 1] and sum(t["cancel_status"] == "cancelled" for t in st.values()) == 1
    cancelled_ordinal = next(o for o, t in st.items() if t["cancel_status"] == "cancelled")
    # 세 번째 응답: 취소 행이 먼저 오든 나중에 오든 같은 거래에 붙는다 (내용 해시로 먼저 맞춤)
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(2), row(2, cdealType="O", cdealDay="26.09.10")]}}
    run3 = fetch(home, server, ["202608"])
    r3 = load_run(db, home, "11110", run3.run_id)
    assert r3.transactions_new == 0 and r3.transactions_changed == 0 and r3.transactions_missing == 0
    st3 = {t["ordinal"]: dict(t) for t in db.conn.execute("SELECT * FROM transactions")}
    assert st3[cancelled_ordinal]["cancel_status"] == "cancelled" and st3[1 - cancelled_ordinal]["cancel_status"] == "none"
    # 네 번째 응답: 취소 안 된 행만 남음 → 취소된 거래가 아니라 남은 행과 맞는 거래가 다시 확인되고, 취소 거래는 사라짐
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(2)]}}
    run4 = fetch(home, server, ["202608"])
    r4 = load_run(db, home, "11110", run4.run_id)
    assert r4.transactions_new == 0 and r4.transactions_seen == 1 and r4.transactions_missing == 1
    st4 = {t["ordinal"]: dict(t) for t in db.conn.execute("SELECT * FROM transactions")}
    assert st4[cancelled_ordinal]["missing_since_run_id"] == run4.run_id and st4[1 - cancelled_ordinal]["missing_since_run_id"] is None
    # 세 번째 같은 행이 새로 오면 새 순번 2
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(2), row(2), row(2)]}}
    run5 = fetch(home, server, ["202608"])
    r5 = load_run(db, home, "11110", run5.run_id)
    assert r5.transactions_new == 1 and sorted(t[0] for t in db.conn.execute("SELECT ordinal FROM transactions")) == [0, 1, 2]
    assert assign_ordinals(db, rt.PROVIDER, "11110", "209912", [(1, 0, row(7)), (1, 1, row(7))]) == [0, 1]


def test_older_run_new_rows_are_reconciled_with_later_complete_runs(db, home, server):
    """새 완전한 실행을 먼저 반영하고 오래된 실행을 나중에 반영하면, 오래된 실행에만 있던 거래는 그 뒤의 완전한 실행 기준으로 사라짐 표시된다 (Codex P2)."""
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(0), row(9)]}}
    run_old = fetch(home, server, ["202608"])
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(0)]}}
    run_new = fetch(home, server, ["202608"])
    assert run_old.run_id < run_new.run_id
    load_run(db, home, "11110", run_new.run_id)
    r = load_run(db, home, "11110", run_old.run_id)
    assert r.transactions_new == 1 and r.transactions_seen == 1 and r.transactions_missing == 1
    t9 = db.conn.execute("SELECT * FROM transactions WHERE identity_hash = ?", (identity_hash(row(9)),)).fetchone()
    assert t9["first_seen_run_id"] == run_old.run_id and t9["last_seen_run_id"] == run_old.run_id and t9["missing_since_run_id"] == run_new.run_id
    t0 = db.conn.execute("SELECT * FROM transactions WHERE identity_hash = ?", (identity_hash(row(0)),)).fetchone()
    assert t0["first_seen_run_id"] == run_old.run_id and t0["last_seen_run_id"] == run_new.run_id and t0["missing_since_run_id"] is None
    assert coverage(db, "11110", ["202608"])["months"][0]["missing"] == 1
    # 그 뒤 완전한 실행이 없는 달(부분 실행만)이면 사라짐 표시를 하지 않는다
    _Handler.scenarios = {"202609": {"kind": "pages", "items": [row(1), row(8)]}}
    old2 = fetch(home, server, ["202609"])
    _Handler.scenarios = {"202609": {"kind": "short_pages", "items": [row(1)], "claimed_total": 9}}
    new2 = fetch(home, server, ["202609"], num_rows=1)
    load_run(db, home, "11110", new2.run_id)
    r2 = load_run(db, home, "11110", old2.run_id)
    assert r2.transactions_new == 1 and r2.transactions_missing == 0


def test_rejects_leave_db_unchanged(db, home, server):
    _Handler.scenarios = {"202608": {"kind": "pages", "items": [row(0), row(1)]}}
    run = fetch(home, server, ["202608"])
    raw = home / run.months[0].pages[0].path
    original = raw.read_bytes()
    raw.write_bytes(original.replace(b"100,000", b"999,999"))
    with pytest.raises(DbError) as e:
        load_run(db, home, "11110", run.run_id)
    assert e.value.code == "raw_hash_mismatch"
    assert db.status()["counts"]["collection_runs"] == 0 and db.status()["counts"]["source_documents"] == 0 and db.status()["dataset_version"] == 0
    raw.write_bytes(original)
    # 손상된 기록
    rp = home / run.run_path
    good = rp.read_text(encoding="utf-8")
    rp.write_text('{"run_id": "x"}', encoding="utf-8")
    with pytest.raises(DbError) as e:
        load_run(db, home, "11110", run.run_id)
    assert e.value.code == "run_invalid"
    bad = json.loads(good)
    bad["months"][0]["pages"][0]["path"] = "../../etc/passwd"
    rp.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(DbError) as e:
        load_run(db, home, "11110", run.run_id)
    assert e.value.code == "run_invalid" and "밖" in e.value.message
    rp.write_text(good, encoding="utf-8")
    with pytest.raises(DbError) as e:
        load_run(db, home, "11110", "20260101-000000-abcdef")
    assert e.value.code == "run_missing"
    # 실제 제공자 응답은 가상 정본에 넣지 않는다
    real = json.loads(good)
    real["endpoint"] = "https://apis.data.go.kr/1613000/RTMSDataSvcNrgTrade/getRTMSDataSvcNrgTrade"
    rp.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DbError) as e:
        load_run(db, home, "11110", run.run_id)
    assert e.value.code == "data_mode_mismatch"
    rp.write_text(good, encoding="utf-8")
    assert load_run(db, home, "11110", run.run_id).outcome == "applied"
    # 원본 없는 페이지: 같은 원본을 가리키는 다른 실행 기록을 만든 뒤 원본을 지운다
    doc = json.loads(good)
    doc["run_id"] = run.run_id[:15] + "-ffffff"
    (home / "raw" / "rt_nrg" / "11110" / f"run-{doc['run_id']}.json").write_text(json.dumps(doc), encoding="utf-8")
    raw.unlink()
    with pytest.raises(DbError) as e:
        load_run(db, home, "11110", doc["run_id"])
    assert e.value.code == "raw_missing" and db.status()["counts"]["collection_runs"] == 1


def test_coverage_fetch_range_and_cli(db, home, server, capsys, monkeypatch):
    _Handler.scenarios = {"202607": {"kind": "pages", "items": [row(0)]}, "202608": {"kind": "pages", "items": [row(1), row(2, cdealType="O", cdealDay="26.08.30")]},
                          "202609": {"kind": "api_error"}}
    assert month_range("2026-07", "2026-09") == ["202607", "202608", "202609"]
    with pytest.raises(rt.CollectError):
        month_range("2026-09", "2026-07")
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", KEY)
    rc = cli.main(["collect", "rt-fetch", "--lawd-cd", "11110", "--from", "2026-07", "--to", "2026-09", "--endpoint", server])
    out = capsys.readouterr()
    assert rc == 1 and "2026-09: failed" in out.out and "rt-load" in out.out and KEY not in out.out + out.err
    assert months_done_on_disk(home, "11110") == {"202607": "complete", "202608": "complete", "202609": "failed"}
    # 두 번째 rt-fetch: 완전한 달은 건너뛰고 실패한 달만 다시 받는다
    _Handler.calls = []
    _Handler.scenarios["202609"] = {"kind": "pages", "items": []}
    rc = cli.main(["collect", "rt-fetch", "--lawd-cd", "11110", "--from", "2026-07", "--to", "2026-09", "--endpoint", server])
    out = capsys.readouterr()
    assert rc == 0 and [q["DEAL_YMD"] for q in _Handler.calls] == ["202609"] and "건너뜀" in out.err and "2026-09: empty" in out.out
    assert cli.main(["collect", "rt-fetch", "--lawd-cd", "11110", "--from", "2026-07", "--to", "2026-09", "--endpoint", server]) == 0
    assert "모두 이미" in capsys.readouterr().err
    # 정본 반영 (정본에 없는 실행을 모두, 오래된 순)
    db.close()
    rc = cli.main(["db", "rt-load", "--lawd-cd", "11110"])
    out = capsys.readouterr()
    assert rc == 0 and out.out.count("거래 반영 [") == 2 and "거래 신규 3" in out.out
    rc = cli.main(["db", "rt-load", "--lawd-cd", "11110"])
    assert rc == 0 and "실행 기록이 없다" in capsys.readouterr().err
    rc = cli.main(["db", "rt-coverage", "--lawd-cd", "11110", "--from", "2026-06", "--to", "2026-10", "--json"])
    c = json.loads(capsys.readouterr().out)
    assert rc == 0 and [m["collection"] for m in c["months"]] == ["none", "complete", "complete", "empty", "none"] and c["gap_months"] == ["202606", "202610"]
    m8 = next(m for m in c["months"] if m["deal_ymd"] == "202608")
    assert m8["transactions"] == 1 and m8["rows_all"] == 2 and m8["cancelled"] == 1 and m8["runs"] == 1, "현재 통계는 취소 확정 거래를 뺀다"
    assert c["transactions"] == 2 and c["cancelled"] == 1 and c["linked"] == 0
    d2 = Db.open(home / "db" / "j5.sqlite3")
    try:
        text = coverage_text(coverage(d2, "11110", ["202608", "202609"]))
        assert "완전 수집 2/2개월" in text and "거래 1건 (취소 확정 1건 제외)" in text and "수집 완료와 연결 완료는 별개" in text
        # 백업에 새 테이블과 정본이 참조한 수집 원본(실행 기록·응답 XML)이 포함된다
        b = create_backup(d2, home)
        assert b.outcome == "completed", b.to_text()
        assert b.counts["transactions"] == 3 and b.counts["collection_runs"] == 4 and b.raw_files == 2 + 4 and b.photos == 0
        manifest = json.loads((home / b.backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
        raw_listed = sorted(f["path"] for f in manifest["files"] if f["path"].startswith("raw/"))
        assert len(raw_listed) == 6 and sum(p.endswith(".json") for p in raw_listed) == 2 and all(p.startswith("raw/rt_nrg/11110/") for p in raw_listed)
        assert verify_backup_dir(home / b.backup_dir)["raw_files"] == 6
        # 참조한 원본이 사라지거나 바뀌면 백업을 완료로 표시하지 않는다
        xml_path = home / d2.conn.execute("SELECT raw_path FROM collection_pages WHERE deal_ymd = '202608'").fetchone()[0]
        orig = xml_path.read_bytes()
        xml_path.write_bytes(orig + b" ")
        b_bad = create_backup(d2, home)
        assert b_bad.outcome == "failed" and any(f["code"] == "raw_incomplete" and "hash_mismatch" in f["message"] for f in b_bad.findings)
        xml_path.write_bytes(orig)
        assert check_raw_files(d2, home)["ok_all"]
    finally:
        d2.close()
    dest = home / "restored"
    dest.mkdir()
    r = restore_backup(home / b.backup_dir, dest)
    assert r.outcome == "completed", r.to_text()
    assert r.counts["transactions"] == 3 and r.counts["transaction_observations"] == 3
    assert (dest / "raw" / "rt_nrg" / "11110").is_dir() and len(list((dest / "raw").rglob("*.xml"))) == 4
    with Db.open(dest / "db" / "j5.sqlite3") as rdb:
        assert check_raw_files(rdb, dest)["ok_all"] and coverage(rdb, "11110", ["202608"])["transactions"] == 1
    # 백업 폴더에서 원본 하나가 빠지면 복구를 거절한다
    (home / b.backup_dir / raw_listed[0]).unlink()
    rr = restore_backup(home / b.backup_dir, home / "restored2")
    assert rr.outcome == "failed" and any(f["code"] in ("file_missing", "extra_or_missing") for f in rr.findings)
    assert cli.main(["db", "rt-coverage", "--lawd-cd", "11110", "--from", "2026-10", "--to", "2026-01"]) == 3
