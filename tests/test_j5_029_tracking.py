"""J5-029 관심 단계(관찰목록) 관리 시험 (ADR-22).

- 마이그레이션 17 `tracking_changes`: 새 정본에 있음, 불변(UPDATE·DELETE 거절), 옛 정본(16)의 readiness_decisions 를 백필.
- `set_status`: 단계 변경 + 불변 이력 + dataset_version 증가, purchase_ready 로/에서의 변경 거절, hold·excluded·archived 는 사유 필요, 같은 단계는 변경 없음, 없는 물건·잘못된 날짜 거절.
- readiness 승인·철회가 같은 이력 표에 decision_id 와 함께 남는다.
- 파생본 시드가 tracking_status·resolution_status 를 싣고, load-seed 는 그 시드를 거절한다(정본 출력값을 되돌려 넣지 않는다).
- CLI `db asset-track`(변경·이력)·`db watchlist`.
가상자료만 쓴다.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.projection import build_projection
from j5.db.readiness import approve, withdraw
from j5.db.store import Db
from j5.db.tracking import history, history_text, overview, overview_text, set_status
from j5.db.validate import ValidationError
from tests.test_j5_015a_judgment import A, DOC, SEED_PATH
from tests.test_j5_018b_stage import _ready_case
from tests.test_j5_018a_readiness import TODAY

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
        return f"2026-09-27T00:{n // 60:02d}:{n % 60:02d}Z"
    d.now = clock
    yield d
    d.close()


def changes(db, asset_id):
    return [dict(r) for r in db.conn.execute("SELECT previous_status, new_status, changed_on, reason, source, decision_id FROM tracking_changes WHERE asset_id = ? ORDER BY recorded_at, rowid", (asset_id,))]


def test_same_second_changes_keep_insertion_order(db):
    """같은 초에 기록된 변경은 삽입 순서로 보인다 (UUID 는 무작위라 정렬 키가 아니다, 리뷰 반영 PR #75)."""
    db.now = lambda: "2026-09-27T01:00:00Z"
    seq = ["watch", "detailed_review", "hold", "background", "watch", "excluded", "unreviewed", "watch"]
    for i, st in enumerate(seq):
        set_status(db, A[0], st, reason=f"r{i}", changed_on=f"2026-09-{10 + i:02d}")
    h = history(db, A[0])
    assert [c["new_status"] for c in h["changes"]] == seq and len({c["recorded_at"] for c in h["changes"]}) == 1
    assert [c["changed_on"] for c in h["changes"]] == [f"2026-09-{10 + i:02d}" for i in range(len(seq))]
    assert overview(db)["watchlist"][0]["last_changed_on"] == "2026-09-17"


def test_schema_17_table_and_immutability(db):
    st = db.status()
    assert st["db_schema_version"] == S.DB_SCHEMA_VERSION == 17 and st["counts"]["tracking_changes"] == 0 and st["ok"]
    assert 17 not in S.FK_OFF_MIGRATIONS and S.WATCHLIST_STATUSES == ("watch", "detailed_review", "purchase_ready")
    set_status(db, A[0], "watch", reason="가상", changed_on="2026-09-20")
    with pytest.raises(sqlite3.IntegrityError, match="불변"):
        with db.transaction():
            db.conn.execute("UPDATE tracking_changes SET reason = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="삭제하지 않는다"):
        with db.transaction():
            db.conn.execute("DELETE FROM tracking_changes")
    assert db.status()["counts"]["tracking_changes"] == 1


def test_set_status_changes_with_history_and_version(db):
    v0 = db.status()["dataset_version"]
    r = set_status(db, A[0], "watch", reason="가상 관찰", changed_on="2026-09-20")
    assert r["changed"] and (r["previous_status"], r["new_status"], r["dataset_version"]) == ("unreviewed", "watch", v0 + 1)
    assert db.conn.execute("SELECT tracking_status FROM assets WHERE asset_id = ?", (A[0],)).fetchone()[0] == "watch"
    # 같은 단계: 변경 없음, 이력·버전 그대로
    r2 = set_status(db, A[0], "watch")
    assert not r2["changed"] and r2["dataset_version"] == v0 + 1 and len(changes(db, A[0])) == 1
    # 사유 없이 옮기기 (사유 선택인 단계) 와 detailed_review → hold (사유 필수)
    set_status(db, A[0], "detailed_review", changed_on="2026-09-21")
    with pytest.raises(ValidationError, match="--reason"):
        set_status(db, A[0], "hold", changed_on="2026-09-22")
    set_status(db, A[0], "hold", reason="자금 확인 대기 (가상)", changed_on="2026-09-22")
    assert [(c["previous_status"], c["new_status"], c["changed_on"], c["reason"], c["source"], c["decision_id"]) for c in changes(db, A[0])] == [
        ("unreviewed", "watch", "2026-09-20", "가상 관찰", "asset_track", None), ("watch", "detailed_review", "2026-09-21", None, "asset_track", None),
        ("detailed_review", "hold", "2026-09-22", "자금 확인 대기 (가상)", "asset_track", None)]
    h = history(db, A[0])
    assert h["tracking_status"] == "hold" and not h["watchlist"] and len(h["changes"]) == 3
    txt = history_text(h)
    assert "현재 보류(hold)" in txt and "2026-09-22 detailed_review → hold [asset-track]: 자금 확인 대기 (가상)" in txt
    assert db.status()["dataset_version"] == v0 + 3


def test_set_status_rejections(db):
    for status, kw, msg in (("purchase_ready", {}, "readiness-approve"), ("favorite", {}, "중 하나"), ("excluded", {}, "--reason"), ("archived", {"reason": " "}, "--reason"),
                            ("watch", {"changed_on": "2026-02-30"}, "달력")):
        with pytest.raises(ValidationError, match=msg):
            set_status(db, A[0], status, **kw)
    with pytest.raises(ValidationError, match="정본에 없다"):
        set_status(db, "00000000-0000-4000-8000-000000000000", "watch")
    assert changes(db, A[0]) == [] and db.status()["dataset_version"] == 1


def test_readiness_decisions_share_history_and_block_asset_track(db):
    _ready_case(db)
    set_status(db, A[0], "detailed_review", changed_on="2026-09-20")
    d = approve(db, A[0], TODAY)
    with pytest.raises(ValidationError, match="readiness-withdraw"):
        set_status(db, A[0], "hold", reason="x")
    w = withdraw(db, A[0], TODAY, reason="새 위험 (가상)")
    assert [(c["previous_status"], c["new_status"], c["source"], c["decision_id"], c["reason"]) for c in changes(db, A[0])] == [
        ("unreviewed", "detailed_review", "asset_track", None, None), ("detailed_review", "purchase_ready", "readiness", d["decision_id"], None),
        ("purchase_ready", "detailed_review", "readiness", w["decision_id"], "새 위험 (가상)")]
    assert "[매입 준비 결정]" in history_text(history(db, A[0]))


def test_migration_17_backfills_readiness_decisions(db):
    _ready_case(db)
    d = approve(db, A[0], TODAY)
    path = db.path
    db.close()
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE tracking_changes")
    conn.execute("DELETE FROM schema_migrations WHERE version = 17")
    conn.commit()
    conn.close()
    with Db.open(path) as d2:
        assert d2.schema_version() == 17 and d2.status()["ok"]
        rows = changes(d2, A[0])
        assert rows == [{"previous_status": "unreviewed", "new_status": "purchase_ready", "changed_on": TODAY, "reason": None, "source": "readiness", "decision_id": d["decision_id"]}]
        assert all(len(r[0]) == 36 for r in d2.conn.execute("SELECT change_id FROM tracking_changes"))


def test_overview_and_watchlist(db):
    set_status(db, A[0], "watch", changed_on="2026-09-20")
    set_status(db, A[1], "detailed_review", changed_on="2026-09-21")
    set_status(db, A[2], "excluded", reason="가상 제외", changed_on="2026-09-22")
    set_status(db, A[3], "background")
    o = overview(db)
    assert o["counts"] == {"unreviewed": 1, "background": 1, "watch": 1, "detailed_review": 1, "purchase_ready": 0, "hold": 0, "excluded": 1, "archived": 0} and o["total"] == 5 and o["pending"] == 0
    assert [(w["asset_id"], w["tracking_status"], w["last_changed_on"]) for w in o["watchlist"]] == [(A[1], "detailed_review", "2026-09-21"), (A[0], "watch", "2026-09-20")]
    txt = overview_text(o)
    assert "관찰목록 (watch·detailed_review·purchase_ready) 2개" in txt and "제외 1" in txt and "30~50개를 넘는다" not in txt


def test_projection_seed_carries_status_and_load_seed_rejects_it(db, home):
    set_status(db, A[0], "watch", changed_on="2026-09-20")
    r = build_projection(db, home, photos=False)
    assert r.outcome == "published", r.to_text()
    seed = json.loads((home / r.output_dir / "assets.seed.json").read_text(encoding="utf-8"))
    by_id = {a["asset_id"]: a for a in seed}
    assert by_id[A[0]]["tracking_status"] == "watch" and by_id[A[1]]["tracking_status"] == "unreviewed" and all(a["resolution_status"] == "confirmed" for a in seed)
    geo = json.loads((home / r.output_dir / "assets.geojson").read_text(encoding="utf-8"))
    assert next(f["properties"]["tracking_status"] for f in geo["features"] if f["properties"]["asset_id"] == A[0]) == "watch"
    # 파생본 시드를 정본 입력으로 되돌리지 않는다 (관심 단계는 asset-track, 확인은 asset-confirm 으로만)
    with pytest.raises(ValidationError, match="정본 출력값"):
        db.load_seed(seed)
    stripped = [{k: v for k, v in a.items() if k not in ("tracking_status", "resolution_status")} for a in seed]
    assert db.load_seed(stripped).unchanged == 5


def test_cli_asset_track_and_watchlist(db, home, capsys, monkeypatch):
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "asset-track", A[0], "watch", "--reason", "가상 관찰", "--on", "2026-09-20"]) == 0
    out = capsys.readouterr().out
    assert "관심 단계 변경: 가상 물건 1" in out and "unreviewed → watch (관찰)" in out and "dataset_version 2" in out
    assert cli.main(["db", "asset-track", A[0], "watch"]) == 0 and "변경 없음" in capsys.readouterr().out
    assert cli.main(["db", "asset-track", A[0], "purchase_ready"]) == 1 and "readiness-approve" in capsys.readouterr().err
    assert cli.main(["db", "asset-track", A[0], "excluded"]) == 1 and "--reason" in capsys.readouterr().err
    assert cli.main(["db", "asset-track", A[0]]) == 0
    assert "현재 관찰(watch) · 관찰목록" in capsys.readouterr().out
    assert cli.main(["db", "asset-track", A[0], "--json"]) == 0
    h = json.loads(capsys.readouterr().out)
    assert h["tracking_status"] == "watch" and len(h["changes"]) == 1 and h["changes"][0]["source"] == "asset_track"
    assert cli.main(["db", "watchlist"]) == 0
    assert "관찰목록 (watch·detailed_review·purchase_ready) 1개" in capsys.readouterr().out
    assert cli.main(["db", "watchlist", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["watch"] == 1
    assert cli.main(["db", "asset-track", "not-an-id", "watch"]) == 1 and "정본에 없다" in capsys.readouterr().err
