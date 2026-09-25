"""J5-015B: 사진 연차 비교 (데이터 사전 §6·§8, db_schema 10).

시험: 첨부의 촬영 지점·방향·이전 사진 열(반영·소급 채움·검증), 시계열 묶기(촬영 지점 → 이전 사진 사슬 → 태그), 연도별 첫 사진·인접 연도 비교·빠진 해,
태그 필터, 정정된 관측 표시, 내보내기(해시 검증·덮어쓰기 금지·index.json), 파생본 records.jsonl 의 선택 필드, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from j5 import cli
from j5.db import schema as S
from j5.db.importer import import_package
from j5.db.photos import export_series, photo_series, series_text
from j5.db.projection import build_projection
from j5.db.store import Db
from j5.db.validate import ValidationError
from tests.conftest import PACKAGES, zip_dir
from tests.fixtures.make_packages import CREATED_AT, STUDY_ID, event, line_bytes, png_1x1, sha

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
A = [f"7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a5{i}" for i in range(5)]
E = [f"5e1f0000-0000-4000-8000-00000000000{i}" for i in range(1, 8)]
PHOTOS = {"2024f": png_1x1((10, 0, 0)), "2025f": png_1x1((20, 0, 0)), "2026f": png_1x1((30, 0, 0)), "2026g": png_1x1((0, 40, 0)), "2026r": png_1x1((0, 0, 50)), "2026c": png_1x1((0, 0, 60))}


def make_package(d: Path, events: list[dict], photos: list[bytes]) -> Path:
    if d.exists():
        shutil.rmtree(d)
    (d / "photos").mkdir(parents=True)
    obs = b"".join(line_bytes(e) for e in events)
    (d / "observations.jsonl").write_bytes(obs)
    files = [{"path": "observations.jsonl", "bytes": len(obs), "sha256": sha(obs)}]
    for p in photos:
        (d / "photos" / f"{sha(p)}.png").write_bytes(p)
        files.append({"path": f"photos/{sha(p)}.png", "bytes": len(p), "sha256": sha(p)})
    manifest = {"format": "j5field", "schema_version": "1.0.0", "study_id": STUDY_ID, "package_id": "b1f0c2d3-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "created_at": CREATED_AT,
                "data_mode": "synthetic", "files": files}
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return d


def ev(eid: str, observed: str, photos: list[bytes], refs_extra: list[dict], status: str = "no_change", corrects: str | None = None, asset: str = A[0]) -> dict:
    e = event(eid, asset, status, None if status == "no_change" else "변화", photos, corrects=corrects)
    e["observed_at"] = observed
    e["device_created_at"] = observed
    for ref, extra in zip(e["attachment_refs"], refs_extra):
        ref.update(extra)
    return e


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY_ID, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    yield d
    d.close()


def series_package(tmp_path) -> Path:
    """물건 1: 2024·2025·2026 전면(촬영 지점 명시 2024·2025, 2026 은 이전 사진 사슬), 2026 1층(태그만), 2026 도로(정정된 관측), 물건 2: 2026 전면."""
    f24, f25, f26, g26, r26, c26 = (PHOTOS[k] for k in ("2024f", "2025f", "2026f", "2026g", "2026r", "2026c"))
    events = [
        ev(E[0], "2024-09-01T10:00:00+09:00", [f24], [{"viewpoint_id": "전면-남측", "heading_deg": 180}]),
        ev(E[1], "2025-09-02T10:00:00+09:00", [f25], [{"viewpoint_id": "전면-남측", "heading_deg": 182, "previous_photo_sha256": sha(f24)}]),
        ev(E[2], "2026-09-03T10:00:00+09:00", [f26, g26], [{"previous_photo_sha256": sha(f25)}, {}], status="change_observed"),
        ev(E[3], "2026-09-04T10:00:00+09:00", [r26], [{"tags": ["road"]}]),
        ev(E[4], "2026-09-04T10:00:00+09:00", [r26], [{"tags": ["road"], "viewpoint_id": "도로-동측"}], corrects=E[3]),
        ev(E[5], "2026-09-05T10:00:00+09:00", [c26], [{"viewpoint_id": "전면"}], asset=A[1]),
    ]
    return make_package(tmp_path / "pkg", events, [f24, f25, f26, g26, r26, c26])


def test_schema_10_columns_import_and_backfill(db, home, tmp_path):
    assert db.status()["db_schema_version"] == S.DB_SCHEMA_VERSION >= 10
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(attachments)")}
    assert {"viewpoint_id", "heading_deg", "previous_photo_sha256"} <= cols
    r = import_package(db, series_package(tmp_path), home)
    assert r.outcome == "applied", r.to_text()
    rows = {(a["record_id"], a["sha256"]): dict(a) for a in db.conn.execute("SELECT * FROM attachments")}
    f24, f25, f26 = sha(PHOTOS["2024f"]), sha(PHOTOS["2025f"]), sha(PHOTOS["2026f"])
    assert rows[(E[0], f24)]["viewpoint_id"] == "전면-남측" and rows[(E[0], f24)]["heading_deg"] == 180 and rows[(E[0], f24)]["previous_photo_sha256"] is None
    assert rows[(E[1], f25)]["previous_photo_sha256"] == f24 and rows[(E[2], f26)]["previous_photo_sha256"] == f25 and rows[(E[2], f26)]["viewpoint_id"] is None
    # 소급 채움: 버전 9 정본(열 없음)에 반영된 첨부를 10 으로 올리면 수입 대장의 행 바이트에서 채운다.
    # 버전 9 상태는 반영 뒤 열·인덱스·마이그레이션 행을 지워 만든다(옛 도구는 이 열을 모른 채 첨부를 넣었다)
    old = home / "db" / "old.sqlite3"
    d9 = Db.create(old, study_id=STUDY_ID, data_mode="synthetic")
    d9.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    assert import_package(d9, series_package(tmp_path), home).outcome == "applied"
    with d9.transaction():
        d9.conn.execute("DROP INDEX attachments_by_viewpoint")
        for col in ("viewpoint_id", "heading_deg", "previous_photo_sha256"):
            d9.conn.execute(f"ALTER TABLE attachments DROP COLUMN {col}")
        d9.conn.execute("DROP TABLE IF EXISTS judgment_rechecks")  # 마이그레이션 11 (J5-015C)
        d9.conn.execute("DELETE FROM schema_migrations WHERE version >= 10")
    assert d9.schema_version() == 9 and "viewpoint_id" not in {r[1] for r in d9.conn.execute("PRAGMA table_info(attachments)")}
    d9.close()
    d10 = Db.open(old)
    try:
        assert d10.schema_version() == S.DB_SCHEMA_VERSION and d10.status()["ok"]
        back = {(a["record_id"], a["sha256"]): dict(a) for a in d10.conn.execute("SELECT * FROM attachments")}
        assert back[(E[0], f24)]["viewpoint_id"] == "전면-남측" and back[(E[0], f24)]["heading_deg"] == 180 and back[(E[1], f25)]["previous_photo_sha256"] == f24
        assert back[(E[2], sha(PHOTOS["2026g"]))]["viewpoint_id"] is None and back[(E[2], sha(PHOTOS["2026g"]))]["previous_photo_sha256"] is None
    finally:
        d10.close()
    # 저장소 검증: 범위 밖 방향·자기 자신을 이전 사진으로·빈 촬영 지점
    with db.transaction():
        rid = db.insert_record({"record_id": E[6], "subject_id": A[0], "subject_type": "asset", "record_type": "field_observation", "source_kind": "field_observation",
                                "schema_version": "1.0.0", "payload": {"change_status": "no_change", "note": None}, "observed_at": "2026-09-06", "observed_at_precision": "date"})
    for bad, word in (({"heading_deg": 360}, "heading_deg"), ({"previous_photo_sha256": "a" * 64}, "previous_photo"), ({"viewpoint_id": " "}, "viewpoint_id")):
        with pytest.raises(ValidationError) as e:
            with db.transaction():
                db.add_attachment(rid, {"attachment_id": "0a0a0a0a-0a0a-4a0a-8a0a-0a0a0a0a0a0a", "rel_path": "photos/aa/" + "a" * 64 + ".png", "sha256": "a" * 64, "mime": "image/png", "bytes": 1, "tags": [], **bad})
        assert word in str(e.value)


def test_photo_series_grouping_and_comparisons(db, home, tmp_path):
    assert import_package(db, series_package(tmp_path), home).outcome == "applied"
    s = photo_series(db, A[0])
    assert s["photos"] == 6 and [g["name"] for g in s["groups"]] == ["도로-동측", "전면-남측", "태그 ground_floor", "태그 road"], [g["name"] for g in s["groups"]]
    front = next(g for g in s["groups"] if g["name"] == "전면-남측")
    assert front["basis"] == "viewpoint" and [p["year"] for p in front["photos"]] == ["2024", "2025", "2026"], "2026 사진은 촬영 지점 없이 이전 사진 사슬로 묶였다"
    assert front["yearly"] == {"2024": sha(PHOTOS["2024f"]), "2025": sha(PHOTOS["2025f"]), "2026": sha(PHOTOS["2026f"])} and front["missing_years"] == []
    assert [(c["from_year"], c["to_year"], c["gap_years"]) for c in front["comparisons"]] == [("2024", "2025", 1), ("2025", "2026", 1)]
    assert front["photos"][2]["change_status"] == "change_observed" and front["photos"][1]["heading_deg"] == 182
    road_tag = next(g for g in s["groups"] if g["name"] == "태그 road")
    assert road_tag["photos"][0]["superseded"] is True and road_tag["photos"][0]["record_id"] == E[3], "정정된 관측의 사진은 태그 묶음에 남되 superseded 표시"
    road_vp = next(g for g in s["groups"] if g["name"] == "도로-동측")
    assert road_vp["photos"][0]["record_id"] == E[4] and road_vp["photos"][0]["superseded"] is False
    only_front = photo_series(db, A[0], tag="front")
    assert only_front["photos"] == 3 and [g["name"] for g in only_front["groups"]] == ["전면-남측"]
    assert photo_series(db, A[1])["groups"][0]["name"] == "전면" and photo_series(db, A[2])["groups"] == []
    text = series_text(s)
    assert "비교 2024 → 2025 (1년)" in text and "정정됨" in text and "보간하지 않는다" in text
    with pytest.raises(ValidationError):
        photo_series(db, "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99")
    # 빠진 해: 2024·2026 만 있으면 2025 가 빠진 해
    with db.transaction():
        db.conn.execute("DELETE FROM record_evidence WHERE record_id = ?", (E[1],))
    gap = photo_series(db, A[0])
    assert next(g for g in gap["groups"] if g["name"] == "전면-남측")["missing_years"] == [], "기록은 그대로다 (근거만 지운 것은 시계열과 무관)"


def test_export_series_verifies_hashes_and_never_overwrites(db, home, tmp_path):
    assert import_package(db, series_package(tmp_path), home).outcome == "applied"
    r = export_series(db, home, A[0])
    assert r["copied"] == 6 and r["skipped"] == 0 and r["problems"] == [] and r["groups"] == 4
    dest = home / r["dest"]
    assert dest.is_dir() and (dest / "index.json").is_file() and r["dest"].startswith("exports/private/photo_series/")
    front_dir = dest / "전면-남측"
    files = sorted(p.name for p in front_dir.iterdir())
    assert files == [f"2024-09-01-{sha(PHOTOS['2024f'])[:8]}.png", f"2025-09-02-{sha(PHOTOS['2025f'])[:8]}.png", f"2026-09-03-{sha(PHOTOS['2026f'])[:8]}.png"]
    index = json.loads((dest / "index.json").read_text(encoding="utf-8"))
    assert index["groups"][1]["photos"][0]["export_path"].endswith(files[0]) and "저장소·공개 배포에 넣지 않는다" in index["note"]
    # 다시 내보내면 같은 내용은 건너뛴다. 다른 내용의 파일이 자리에 있으면 거절한다. 원본이 변조되면 문제로 보고한다
    r2 = export_series(db, home, A[0])
    assert r2["copied"] == 0 and r2["skipped"] == 6
    (front_dir / files[0]).write_bytes(b"other")
    r3 = export_series(db, home, A[0])
    assert any("덮어쓰지 않음" in p for p in r3["problems"]) and (front_dir / files[0]).read_bytes() == b"other"
    src = home / db.conn.execute("SELECT rel_path FROM attachments WHERE sha256 = ?", (sha(PHOTOS["2025f"]),)).fetchone()[0]
    src.write_bytes(src.read_bytes() + b"x")
    r4 = export_series(db, home, A[0], tag="front")
    assert any("해시 불일치" in p for p in r4["problems"]) and r4["photos"] == 3
    # 파생본 records.jsonl 에 선택 필드가 있을 때만 들어간다
    pr = build_projection(db, home)
    assert pr.outcome == "published", pr.to_text()
    rows = [json.loads(l) for l in (home / pr.output_dir / "records.jsonl").read_text(encoding="utf-8").splitlines() if l]
    a0 = next(r for r in rows if r["record_id"] == E[0])["attachments"][0]
    a2 = next(r for r in rows if r["record_id"] == E[2])["attachments"]
    assert a0["viewpoint_id"] == "전면-남측" and a0["heading_deg"] == 180 and "previous_photo_sha256" not in a0
    assert "viewpoint_id" not in next(a for a in a2 if a["sha256"] == sha(PHOTOS["2026g"]))


def test_cli_photo_series(db, home, tmp_path, capsys, monkeypatch):
    assert import_package(db, series_package(tmp_path), home).outcome == "applied"
    db.close()
    monkeypatch.setenv("J5_DATA_HOME", str(home))
    rc = cli.main(["db", "photo-series", "--asset", A[0]])
    out = capsys.readouterr().out
    assert rc == 0 and "사진 6장, 묶음 4개" in out and "비교 2025 → 2026" in out
    rc = cli.main(["db", "photo-series", "--asset", A[0], "--tag", "front", "--json"])
    j = json.loads(capsys.readouterr().out)
    assert rc == 0 and j["photos"] == 3
    rc = cli.main(["db", "photo-series", "--asset", A[0], "--export"])
    out = capsys.readouterr().out
    assert rc == 0 and "복사 6" in out and (home / "exports" / "private" / "photo_series").is_dir()
    assert cli.main(["db", "photo-series", "--asset", "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"]) == 1
    assert cli.main(["db", "photo-series", "--asset", A[0], "--tag", "nope"]) == cli.USAGE_ERROR
