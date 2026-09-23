"""J5-010: 단방향 반영. 완료 조건 "중복·ID 불일치·파일/DB 실패 시험" (릴리스 계획 §10), R1b 인수(§5) 중 반영기 몫.

시험 항목: 정상 입력 한 번, 같은 파일 두 번, 재내보내기(같은 이벤트·다른 파일), 같은 ID·다른 내용(패키지 안·정본 대조),
누락 사진, 정정 대상 없음 → 대상 반영 후 정정 반영, 사진 복사 후 DB 실패(정리대기), 파일 실패, 원본 불변, CLI.
가상자료만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from j5 import cli
from j5.db.importer import LOG_FILE, import_package, package_sha256, photo_rel_path
from j5.db.store import Db
from tests.conftest import PACKAGES, zip_dir
from tests.fixtures.make_packages import ASSETS, EVENT, event, line_bytes, png_1x1, sha

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
STUDY = "j5-synthetic-study"
RED, BLUE, GREEN = png_1x1((255, 0, 0)), png_1x1((0, 0, 255)), png_1x1((0, 255, 0))
E4 = "1a2b3c4d-0001-4000-8000-000000000004"
E5 = "1a2b3c4d-0001-4000-8000-000000000005"


def write_pkg(d: Path, lines: list[bytes], photos: list[bytes], package_id: str, *, study_id: str = STUDY, data_mode: str = "synthetic", manifest_override=None) -> Path:
    """tests/fixtures/make_packages.write_package 와 같은 규칙으로 tmp 에 패키지 폴더를 만든다."""
    (d / "photos").mkdir(parents=True)
    obs = b"".join(lines)
    (d / "observations.jsonl").write_bytes(obs)
    files = [{"path": "observations.jsonl", "bytes": len(obs), "sha256": sha(obs)}]
    for p in photos:
        h = sha(p)
        (d / "photos" / f"{h}.png").write_bytes(p)
        files.append({"path": f"photos/{h}.png", "bytes": len(p), "sha256": h})
    m = {"format": "j5field", "schema_version": "1.0.0", "study_id": study_id, "package_id": package_id, "created_at": "2026-09-22T12:00:00+09:00",
         "data_mode": data_mode, "files": files}
    if manifest_override:
        manifest_override(m)
    (d / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return d


def tree_hash(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))  # dataset_version 1
    yield d
    d.close()


def codes(r, levels=("hold", "reject", "error")) -> set[str]:
    return {f["code"] for f in r.findings if f["level"] in levels}


def runs(db) -> list[tuple]:
    return [tuple(r) for r in db.conn.execute("SELECT outcome, events_new, events_skipped, photos_stored, dataset_version_before, dataset_version_after FROM import_runs ORDER BY finished_at, rowid")]


# ---- 정상 입력 ----

@pytest.mark.parametrize("form", ["dir", "zip"])
def test_apply_valid_package(db, home, tmp_path, form):
    src = PACKAGES / "valid" if form == "dir" else zip_dir(PACKAGES / "valid", tmp_path / "valid.j5field.zip")
    before = tree_hash(src) if src.is_dir() else {src.name: hashlib.sha256(src.read_bytes()).hexdigest()}
    r = import_package(db, src, home)
    assert r.outcome == "applied", r.to_text()
    assert (r.events_total, r.events_new, r.events_skipped, r.photos_stored, r.photos_reused) == (3, 3, 0, 2, 0)
    assert (r.dataset_version_before, r.dataset_version_after) == (1, 2) and db.status()["dataset_version"] == 2
    st = db.status()
    assert st["ok"] and st["counts"] == {"subjects": 5, "assets": 5, "source_documents": 1, "records": 3, "record_evidence": 3, "attachments": 3}
    # 기록: event_id 승계, 관측 시각·정밀도·기기 입력 시각 보존, collected_at 은 패키지 생성 시각, 정본 시각은 PC 부여
    rec = db.get_record(EVENT[1])
    assert rec["observed_at"] == "2026-09-22" and rec["observed_at_precision"] == "date" and rec["device_created_at"] == "2026-09-22T10:16:30+09:00"
    assert rec["collected_at"] == "2026-09-22T09:00:00+09:00" and rec["payload"] == {"change_status": "no_change", "note": None}
    # 사진: 내용 해시 경로에 보관, 첨부가 그 경로를 가리키고 파일이 실제로 있다
    for att in db.conn.execute("SELECT rel_path, sha256, bytes FROM attachments"):
        f = home / att["rel_path"]
        assert f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() == att["sha256"] and f.stat().st_size == att["bytes"]
        assert att["rel_path"] == photo_rel_path(att["sha256"], "png")
    shared = sha(RED)
    assert db.conn.execute("SELECT COUNT(*) FROM attachments WHERE sha256 = ?", (shared,)).fetchone()[0] == 2, "두 이벤트가 공유한 사진은 파일 하나, 첨부 두 행"
    # 근거: 패키지 출처 문서에 루트 근거
    doc = dict(db.conn.execute("SELECT * FROM source_documents").fetchone())
    assert doc["document_kind"] == "field_package" and doc["title"] == src.name and doc["collected_at"] == "2026-09-22T09:00:00+09:00"
    assert doc["sha256"] == (r.package_sha256 if src.is_file() else None)
    ev = [dict(x) for x in db.conn.execute("SELECT field_path, document_id, locator FROM record_evidence ORDER BY record_id")]
    assert all(e["field_path"] == "$" and e["document_id"] == doc["document_id"] and e["locator"].startswith("observations.jsonl#event_id=") for e in ev)
    # 수입 기록·대장·로그
    assert runs(db) == [("applied", 3, 0, 2, 1, 2)]
    ledger = {x[0]: x[1] for x in db.conn.execute("SELECT event_id, outcome FROM import_events")}
    assert ledger == {EVENT[0]: "inserted", EVENT[1]: "inserted", EVENT[2]: "inserted"}
    line = db.conn.execute("SELECT line FROM import_events WHERE event_id = ?", (EVENT[0],)).fetchone()[0]
    assert line in (PACKAGES / "valid" / "observations.jsonl").read_bytes() and line.endswith(b"\n"), "고정 행 바이트를 대장에 보관"
    log = [json.loads(l) for l in (home / LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert log[-1]["outcome"] == "applied" and log[-1]["run_id"] == r.run_id
    # 입력 원본 불변
    assert (tree_hash(src) if src.is_dir() else {src.name: hashlib.sha256(src.read_bytes()).hexdigest()}) == before


# ---- 중복 ----

def test_same_file_twice_is_duplicate_and_changes_nothing(db, home, tmp_path):
    z = zip_dir(PACKAGES / "valid", tmp_path / "valid.j5field.zip")
    import_package(db, z, home)
    snap = (db.status(), tree_hash(home / "photos"))
    r = import_package(db, z, home)
    assert r.outcome == "duplicate" and "package_already_imported" in {f["code"] for f in r.findings}
    assert (r.events_new, r.photos_stored, r.dataset_version_after) == (0, 0, 2)
    assert (db.status(), tree_hash(home / "photos")) == snap
    assert [x[0] for x in runs(db)] == ["applied", "duplicate"]
    assert db.conn.execute("SELECT COUNT(*) FROM import_events").fetchone()[0] == 3, "중복 실행은 대장에 행을 더하지 않는다"


def test_reexport_with_new_package_id_is_duplicate(db, home, tmp_path):
    """같은 이벤트 바이트를 다른 package_id·다른 파일로 다시 내보낸 경우: 새 이벤트 없음, 버전 유지 (§3.2.6)."""
    import_package(db, PACKAGES / "valid", home)
    lines = [l + b"\n" for l in (PACKAGES / "valid" / "observations.jsonl").read_bytes().split(b"\n") if l]
    again = write_pkg(tmp_path / "again", lines[::-1], [RED, BLUE], "5e5e0000-0000-4000-8000-0000000000aa")  # 순서도 바꿔 본다
    r = import_package(db, again, home)
    assert r.outcome == "duplicate" and (r.events_total, r.events_new, r.events_skipped) == (3, 0, 3)
    assert db.status()["dataset_version"] == 2 and db.status()["counts"]["records"] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM import_events WHERE outcome = 'inserted'").fetchone()[0] == 3


def test_partial_overlap_applies_only_new_events(db, home, tmp_path):
    import_package(db, PACKAGES / "valid", home)
    lines = [l + b"\n" for l in (PACKAGES / "valid" / "observations.jsonl").read_bytes().split(b"\n") if l]
    e4 = event(E4, ASSETS[3][0], "change_observed", "간판 교체", [GREEN])
    mixed = write_pkg(tmp_path / "mixed", [lines[0], line_bytes(e4)], [RED, GREEN], "5e5e0000-0000-4000-8000-0000000000ab")
    r = import_package(db, mixed, home)
    assert r.outcome == "applied" and (r.events_total, r.events_new, r.events_skipped, r.photos_stored, r.photos_reused) == (2, 1, 1, 1, 0)
    assert db.status()["dataset_version"] == 3 and db.status()["counts"]["records"] == 4
    assert {x[0]: x[1] for x in db.conn.execute("SELECT event_id, outcome FROM import_events WHERE run_id = ?", (r.run_id,))} == {EVENT[0]: "skipped_duplicate", E4: "inserted"}
    assert (home / photo_rel_path(sha(GREEN), "png")).is_file()


def test_in_package_same_bytes_repeat_is_skipped(db, home):
    r = import_package(db, PACKAGES / "duplicate_same_content", home)
    assert r.outcome == "applied" and (r.events_total, r.events_new, r.events_skipped) == (2, 2, 1)
    assert db.status()["counts"]["records"] == 2


# ---- ID 불일치 (같은 ID·다른 내용) ----

def test_conflict_inside_package_is_held(db, home):
    r = import_package(db, PACKAGES / "duplicate_conflict", home)
    assert r.outcome == "held" and "event_id_conflict" in codes(r)
    st = db.status()
    assert st["counts"]["records"] == 0 and st["counts"]["source_documents"] == 0 and st["dataset_version"] == 1
    assert not (home / "photos").exists(), "보류 시 사진을 보관하지 않는다"
    assert runs(db) == [("held", 0, 0, 0, 1, 1)]


def test_conflict_with_canonical_is_held(db, home, tmp_path):
    import_package(db, PACKAGES / "valid", home)
    e1_changed = event(EVENT[0], ASSETS[0][0], "change_observed", "다른 내용으로 바뀐 행", [RED])
    pkg = write_pkg(tmp_path / "changed", [line_bytes(e1_changed)], [RED], "5e5e0000-0000-4000-8000-0000000000ac")
    snap = db.status()
    r = import_package(db, pkg, home)
    assert r.outcome == "held" and "event_id_conflict_with_canonical" in codes(r)
    assert db.status() == snap, "정본은 그대로"
    assert db.get_record(EVENT[0])["payload"]["note"] == "1층 임대 광고 부착"


# ---- 거절 ----

@pytest.mark.parametrize("case, code", [("missing_attachment", "attachment_not_in_manifest"), ("unknown_asset", "asset_not_in_seed"),
                                        ("corrupt_hash_mismatch", "file_hash_mismatch"), ("unsupported_heic", "event_schema")])
def test_rejected_cases_change_nothing(db, home, case, code):
    r = import_package(db, PACKAGES / case, home)
    assert r.outcome == "rejected" and code in codes(r), r.to_text()
    assert db.status()["counts"]["records"] == 0 and db.status()["dataset_version"] == 1 and not (home / "photos").exists()
    assert runs(db)[-1][0] == "rejected"


def test_rejects_study_id_and_data_mode_mismatch_and_empty_db(home, tmp_path):
    with Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic") as empty:
        r = import_package(empty, PACKAGES / "valid", home)
        assert r.outcome == "rejected" and "no_assets_in_db" in codes(r)
    with Db.open(home / "db" / "j5.sqlite3") as db:
        db.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
        other = write_pkg(tmp_path / "other", [line_bytes(event(E4, ASSETS[0][0], "no_change", None, []))], [], "5e5e0000-0000-4000-8000-0000000000ad", study_id="other-study")
        r = import_package(db, other, home)
        assert r.outcome == "rejected" and "manifest_study_id_mismatch" in codes(r)
        real = write_pkg(tmp_path / "real", [line_bytes(event(E4, ASSETS[0][0], "no_change", None, []))], [], "5e5e0000-0000-4000-8000-0000000000ae", data_mode="private_real")
        r = import_package(db, real, home)
        assert r.outcome == "rejected" and "data_mode_mismatch" in codes(r)
        assert db.status()["counts"]["records"] == 0


# ---- 정정 연결 ----

def test_correction_needs_target_then_links(db, home, tmp_path):
    corr = event(E4, ASSETS[0][0], "hard_to_confirm", "정정: 광고는 옆 건물", [], corrects=EVENT[0])
    pkg_corr = write_pkg(tmp_path / "corr", [line_bytes(corr)], [], "5e5e0000-0000-4000-8000-0000000000b1")
    r = import_package(db, pkg_corr, home)
    assert r.outcome == "held" and "corrects_target_missing" in codes(r) and db.status()["counts"]["records"] == 0
    assert import_package(db, PACKAGES / "valid", home).outcome == "applied"
    r = import_package(db, pkg_corr, home)
    assert r.outcome == "applied", r.to_text()
    assert db.get_record(E4)["supersedes_id"] == EVENT[0] and db.get_record(EVENT[0])["payload"]["note"] == "1층 임대 광고 부착"
    # 같은 패키지 안에서 정정이 대상보다 앞에 와도 대상을 먼저 넣는다
    base = event(E5, ASSETS[4][0], "no_change", None, [])
    corr2 = event("1a2b3c4d-0001-4000-8000-000000000006", ASSETS[4][0], "change_observed", "정정", [], corrects=E5)
    pkg2 = write_pkg(tmp_path / "corr2", [line_bytes(corr2), line_bytes(base)], [], "5e5e0000-0000-4000-8000-0000000000b2")
    r = import_package(db, pkg2, home)
    assert r.outcome == "applied" and db.get_record("1a2b3c4d-0001-4000-8000-000000000006")["supersedes_id"] == E5


# ---- 파일·DB 실패 ----

def test_db_failure_after_photo_copy_rolls_back_and_keeps_photos_as_orphans(db, home, monkeypatch):
    def boom(self):
        raise RuntimeError("디스크 가득 (시험)")
    monkeypatch.setattr(Db, "bump_dataset_version", boom)
    r = import_package(db, PACKAGES / "valid", home)
    assert r.outcome == "failed" and "db_apply_failed" in codes(r) and "디스크 가득" in r.message
    st = db.status()
    assert st["ok"] and st["counts"]["records"] == 0 and st["counts"]["source_documents"] == 0 and st["dataset_version"] == 1, "DB 는 되돌아갔다"
    assert db.conn.execute("SELECT COUNT(*) FROM import_events").fetchone()[0] == 0
    assert sorted(r.orphan_photos) == sorted(photo_rel_path(sha(p), "png") for p in (RED, BLUE))
    for rel in r.orphan_photos:
        assert (home / rel).is_file(), "정리대기 사진은 지우지 않는다"
    assert runs(db) == [("failed", 3, 0, 2, 1, 1)]
    rec = json.loads(db.conn.execute("SELECT report_json FROM import_runs").fetchone()[0])
    assert rec["orphan_photos"] == r.orphan_photos
    log = [json.loads(l) for l in (home / LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert log[-1]["outcome"] == "failed" and log[-1]["orphan_photos"] == r.orphan_photos
    # 원인 제거 후 재시도: 보관된 사진을 재사용하고 정상 반영된다
    monkeypatch.undo()
    r2 = import_package(db, PACKAGES / "valid", home)
    assert r2.outcome == "applied" and (r2.photos_stored, r2.photos_reused) == (0, 2) and db.status()["dataset_version"] == 2


def test_file_failure_before_db_changes_nothing(db, home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "photos").write_text("not a dir", encoding="utf-8")  # 사진 보관 위치가 파일이면 저장이 실패한다
    r = import_package(db, PACKAGES / "valid", home)
    assert r.outcome == "failed" and "photo_store_failed" in codes(r)
    assert db.status()["counts"]["records"] == 0 and db.status()["dataset_version"] == 1 and r.orphan_photos == []
    assert runs(db) == [("failed", 3, 0, 0, 1, 1)]


def test_existing_photo_with_different_content_fails_without_touching_it(db, home):
    bad = home / photo_rel_path(sha(RED), "png")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"garbage")
    r = import_package(db, PACKAGES / "valid", home)
    assert r.outcome == "failed" and "photo_store_conflict" in codes(r)
    assert bad.read_bytes() == b"garbage" and db.status()["counts"]["records"] == 0


def test_package_sha256_is_stable_for_dir_and_zip(tmp_path):
    a = package_sha256(PACKAGES / "valid")
    copy = tmp_path / "copy"
    shutil.copytree(PACKAGES / "valid", copy)
    assert package_sha256(copy) == a
    z = zip_dir(PACKAGES / "valid", tmp_path / "v.zip")
    assert package_sha256(z) == hashlib.sha256(z.read_bytes()).hexdigest() != a


# ---- CLI ----

def test_cli_db_import(home, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    assert cli.main(["db", "init", "--study-id", STUDY, "--data-mode", "synthetic"]) == 0
    assert cli.main(["db", "load-seed", str(SEED_PATH)]) == 0
    capsys.readouterr()
    z = zip_dir(PACKAGES / "valid", tmp_path / "valid.j5field.zip")
    assert cli.main(["db", "import", str(z)]) == 0
    out = capsys.readouterr().out
    assert "판정: applied" in out and "dataset_version 1 → 2" in out and "백업·파생본 생성 완료를 뜻하지 않는다" in out
    assert cli.main(["db", "import", str(z), "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["outcome"] == "duplicate" and j["events_new"] == 0
    assert cli.main(["db", "import", str(PACKAGES / "duplicate_conflict")]) == 2
    assert "판정: held" in capsys.readouterr().out
    assert cli.main(["db", "import", str(PACKAGES / "unknown_asset")]) == 1
    assert "판정: rejected" in capsys.readouterr().out
    assert cli.main(["db", "import", str(tmp_path / "none.zip")]) == cli.USAGE_ERROR
    capsys.readouterr()
    monkeypatch.delenv("J5_DATA_HOME")
    assert cli.main(["db", "--db", str(home / "db" / "j5.sqlite3"), "import", str(z)]) == cli.USAGE_ERROR
    assert "data-home" in capsys.readouterr().err
    assert cli.main(["db", "--db", str(home / "db" / "j5.sqlite3"), "import", str(z), "--data-home", str(home)]) == 0
    capsys.readouterr()
