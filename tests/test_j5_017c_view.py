"""J5-017C: 구파생본(.j5view.zip) 열람·검증. 릴리스 계획 §8 R6 ("구패키지 열람"), 데이터 사전 §4·§12 (구본 보존용 내보내기는 구버전 표시).

시험: ZIP·폴더 열람과 요약(버전·범위·기록 종류·관측일·사진 포함), 정본 비교(최신/구본/정본보다 새로움/다른 정본/미확인), 변조·누락·적대적 ZIP(경로 탈출·
허용되지 않은 이름·암호화·중복·크기 한도) 거절, 도구가 모르는 projection_schema 버전 표시, 입력 불변, CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import warnings
import zipfile
from pathlib import Path

import pytest

from j5 import cli
from j5.db.importer import import_package
from j5.db.projection import build_projection
from j5.db.store import Db
from j5.db.viewpkg import ViewError, extract_view_zip, inspect_view, view_text
from j5.package.limits import Limits
from j5.package.reader import ContainerError
from tests.conftest import PACKAGES

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json"
STUDY = "j5-synthetic-study"


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def db(home):
    d = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
    d.load_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
    assert import_package(d, PACKAGES / "valid", home).outcome == "applied"  # dataset_version 2, 기록 3, 사진 2
    yield d
    d.close()


def _zip(home: Path, r) -> Path:
    return home / r.output_dir / r.zip_name


def _rezip(src: Path, dst: Path, mutate) -> Path:
    """ZIP 항목을 (이름, 바이트) 로 읽어 mutate 로 바꾼 뒤 다시 쓴다."""
    with zipfile.ZipFile(src) as zf:
        items = [(n, zf.read(n)) for n in zf.namelist()]
    items = mutate(items)
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for n, data in items:
            zf.writestr(n, data)
    return dst


def test_inspect_zip_and_dir_with_summary_and_freshness(db, home):
    r = build_projection(db, home, photos=True)
    z = _zip(home, r)
    before = z.read_bytes()
    v = inspect_view(z)
    assert v["verdict"] == "ok" and v["kind"] == "zip" and v["schema_supported"] is True, v["message"]
    m, s = v["manifest"], v["summary"]
    assert m["source_dataset_version"] == 2 and m["counts"]["records"] == 3 and m["counts"]["photos"] == 2 and m["scope_count"] == 5 and m["study_id"] == STUDY
    assert s["records_by_type"] == {"field_observation": 3} and s["observed_from"] == "2026-09-22" and s["photos_included"] is True and s["attachments_with_photo"] == 3
    assert v["freshness"]["checked"] is False and v["freshness"]["stale"] is None
    assert z.read_bytes() == before, "입력 ZIP 은 바뀌지 않는다"
    t = view_text(v)
    assert "판정: ok" in t and "field_observation 3" in t and "최신 여부: 미확인" in t and "사진: 포함" in t
    # 폴더도 같은 결과. 정본을 주면 최신
    vd = inspect_view(home / r.output_dir, db=db)
    assert vd["verdict"] == "ok" and vd["kind"] == "dir" and vd["freshness"] == {"checked": True, "state": "최신 (정본 v2)", "stale": False, "dataset_version": 2}
    # 정본이 바뀌면 구본. 정본보다 새로운 파생본과 다른 정본도 구분한다
    with db.transaction():
        db.bump_dataset_version()
    f = inspect_view(z, db=db)["freshness"]
    assert f["stale"] is True and f["state"] == "구본 (정본 v3, 파생본 v2)"
    assert "구본" in view_text(inspect_view(z, db=db))
    other = Db.create(home / "other.sqlite3", study_id="other-study", data_mode="synthetic")
    try:
        with other.transaction():
            other.set_meta("dataset_version", "1")
        assert inspect_view(z, db=other)["freshness"]["state"].startswith("다른 정본의 파생본")
        with other.transaction():
            other.set_meta("study_id", STUDY)
        assert "정본보다 새로움" in inspect_view(z, db=other)["freshness"]["state"]
    finally:
        other.close()


def test_rejects_tampered_and_adversarial_zips(db, home, tmp_path):
    r = build_projection(db, home, photos=True)
    z = _zip(home, r)
    # 항목 변조 → 해시 불일치
    bad = _rezip(z, tmp_path / "hash.zip", lambda items: [(n, d + b"\n" if n == "records.jsonl" else d) for n, d in items])
    v = inspect_view(bad)
    assert v["verdict"] == "invalid" and v["problem"] == "verify_hash"
    # manifest 없음
    v = inspect_view(_rezip(z, tmp_path / "nomf.zip", lambda items: [(n, d) for n, d in items if n != "manifest.json"]))
    assert v["problem"] == "manifest_missing"
    # 파일 누락
    v = inspect_view(_rezip(z, tmp_path / "missing.zip", lambda items: [(n, d) for n, d in items if not n.startswith("photos/")]))
    assert v["problem"] == "verify_file_missing"
    # 경로 탈출·허용되지 않은 이름·중복·암호화 표시
    v = inspect_view(_rezip(z, tmp_path / "trav.zip", lambda items: items + [("../evil.json", b"{}")]))
    assert v["problem"] == "entry_traversal"
    v = inspect_view(_rezip(z, tmp_path / "name.zip", lambda items: items + [("extra.txt", b"x")]))
    assert v["problem"] == "entry_not_allowed"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # zipfile 이 중복 이름을 경고하며 쓴다 (적대적 ZIP 을 만들려는 것)
        dup = _rezip(z, tmp_path / "dup.zip", lambda items: items + [("records.jsonl", b"")])
    v = inspect_view(dup)
    assert v["problem"] == "entry_duplicate_name"
    enc = tmp_path / "enc.zip"
    shutil.copy(z, enc)
    from tests.test_j5_008_inspect import _patch_central_header
    _patch_central_header(enc, "records.jsonl", 8, (0x1).to_bytes(2, "little"))  # 중앙 디렉터리의 일반 플래그에 암호화 비트
    assert inspect_view(enc)["problem"] == "entry_encrypted"
    # 크기 한도: 실제 읽은 바이트로 강제한다
    with pytest.raises(ContainerError) as e:
        extract_view_zip(z, tmp_path / "out", Limits(manifest=10))
    assert e.value.code == "entry_size_exceeded"
    # ZIP 이 아님·없음
    (tmp_path / "x.j5view.zip").write_bytes(b"not a zip")
    assert inspect_view(tmp_path / "x.j5view.zip")["problem"] == "zip_bad_file"
    assert inspect_view(tmp_path / "nope")["problem"] == "not_found"
    assert "판정: invalid" in view_text(v)


def test_unknown_projection_schema_version_is_flagged(db, home, tmp_path):
    r = build_projection(db, home)
    z = _zip(home, r)

    def bump(items):
        out = []
        for n, d in items:
            if n == "manifest.json":
                m = json.loads(d)
                m["projection_schema_version"] = "9.9.9"
                d = json.dumps(m).encode("utf-8")
            out.append((n, d))
        return out
    v = inspect_view(_rezip(z, tmp_path / "future.zip", bump))
    # 도구의 manifest 스키마가 enum 으로 막으므로 검증 실패로 나타난다. 도구가 모르는 버전은 열지 않는다
    assert v["verdict"] == "invalid" and v["problem"] == "verify_manifest_schema"


def test_cli_view_inspect(db, home, tmp_path, capsys):
    r = build_projection(db, home)
    z = _zip(home, r)
    db.close()
    assert cli.main(["view", "inspect", str(z)]) == 0
    out = capsys.readouterr().out
    assert "판정: ok" in out and "최신 여부: 미확인" in out
    assert cli.main(["view", "inspect", str(z), "--db", str(home / "db" / "j5.sqlite3"), "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["freshness"]["state"] == "최신 (정본 v2)" and j["manifest"]["counts"]["records"] == 3
    bad = _rezip(z, tmp_path / "bad.zip", lambda items: [(n, d + b"x" if n == "assets.geojson" else d) for n, d in items])
    assert cli.main(["view", "inspect", str(bad)]) == 1
    assert cli.main(["view", "inspect", str(tmp_path / "nope.zip")]) == 3
    assert cli.main(["view", "inspect", str(z), "--db", str(tmp_path / "no.sqlite3")]) == 3
    assert hashlib.sha256(z.read_bytes()).hexdigest() == r.zip_sha256
