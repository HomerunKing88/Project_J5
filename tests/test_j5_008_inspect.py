"""J5-008: 패키지 검사 도구. fixture 9종(디렉터리·ZIP)과 적대적 ZIP.

통과는 실제 폰이 만든 ZIP·기기 검증이 아니다. 테스트는 코드(finding code)로 단언한다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from j5 import cli
from j5.package.limits import Limits
from j5.package.validate import event_hash, inspect_package, sniff_image
from tests.conftest import FIXED_TIME, FIXTURES, PACKAGES, zip_dir

SEED = json.loads((FIXTURES / "assets.seed.synthetic.json").read_text(encoding="utf-8"))

EXPECTED = {
    # case: (verdict, required code)   -- tests/fixtures/README.md 표와 일치해야 한다
    "valid": ("ok", None),
    "duplicate_same_content": ("ok", "event_duplicate_same_bytes"),
    "duplicate_conflict": ("hold", "event_id_conflict"),
    "corrupt_hash_mismatch": ("reject", "file_hash_mismatch"),
    "corrupt_path_traversal": ("reject", "manifest_schema"),
    "missing_attachment": ("reject", "attachment_not_in_manifest"),
    "unsupported_heic": ("reject", "event_schema"),
    "unknown_asset": ("reject", "asset_not_in_seed"),
    "invalid_change_without_evidence": ("reject", "event_schema"),
}


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


@pytest.mark.parametrize("case", sorted(EXPECTED))
@pytest.mark.parametrize("form", ["dir", "zip"])
def test_fixture_verdicts(case, form, zip_of):
    path = PACKAGES / case if form == "dir" else zip_of(case)
    report = inspect_package(path, seed=SEED)
    verdict, code = EXPECTED[case]
    assert report.verdict() == verdict, report.to_text()
    if code:
        assert code in codes(report), report.to_text()
    assert report.kind == form


def test_fixture_readme_lists_every_case():
    text = (FIXTURES / "README.md").read_text(encoding="utf-8")
    for case in EXPECTED:
        assert f"| {case} |" in text


def test_unknown_asset_passes_without_seed():
    report = inspect_package(PACKAGES / "unknown_asset")
    assert report.verdict() == "ok"
    assert "study_id_not_checked" in codes(report)


def test_study_id_check():
    ok = inspect_package(PACKAGES / "valid", study_id="j5-synthetic-study")
    assert ok.verdict() == "ok" and "study_id_not_checked" not in codes(ok)
    bad = inspect_package(PACKAGES / "valid", study_id="other-study")
    assert bad.verdict() == "reject" and "manifest_study_id_mismatch" in codes(bad)


def test_valid_counts_and_metadata():
    r = inspect_package(PACKAGES / "valid", seed=SEED)
    assert r.counts == {"lines": 3, "events": 3, "duplicates": 0, "conflicts": 0, "photos": 2, "photos_referenced": 2}
    assert r.package_id == "5e5e0000-0000-4000-8000-000000000001"
    assert r.data_mode == "synthetic" and r.schema_version == "1.0.0"


def test_heic_hint_in_message():
    r = inspect_package(PACKAGES / "unsupported_heic")
    assert any("HEIC" in f.message for f in r.findings if f.code == "event_schema")


def test_json_output_is_deterministic_and_ascii():
    a = inspect_package(PACKAGES / "duplicate_conflict").to_json()
    b = inspect_package(PACKAGES / "duplicate_conflict").to_json()
    assert a == b
    assert a.isascii()
    d = json.loads(a)
    assert d["verdict"] == "hold" and d["findings"][0]["level"] == "hold"


def test_event_hash_includes_trailing_lf():
    raw = (PACKAGES / "valid" / "observations.jsonl").read_bytes()
    first = raw.split(b"\n")[0]
    assert event_hash(first + b"\n") != event_hash(first)


@pytest.mark.parametrize("head,expected", [
    (b"\xff\xd8\xff\xe0", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"), (b"\x00\x00\x00\x18ftypheic", None), (b"", None),
])
def test_sniff_image(head, expected):
    assert sniff_image(head) == expected


# ---- 적대적 ZIP ----

def _zip_with(tmp_path: Path, name: str, extra: dict[str, bytes] | None = None, *, base="valid",
              writer=None) -> Path:
    dst = tmp_path / name
    src = PACKAGES / base
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(x for x in src.rglob("*") if x.is_file()):
            zf.writestr(zipfile.ZipInfo(p.relative_to(src).as_posix(), FIXED_TIME), p.read_bytes())
        for n, data in (extra or {}).items():
            zf.writestr(zipfile.ZipInfo(n, FIXED_TIME), data)
        if writer:
            writer(zf)
    return dst


@pytest.mark.parametrize("entry,code", [
    ("../escape.txt", "entry_traversal"),
    ("photos/../x.png", "entry_traversal"),
    ("/etc/passwd", "entry_absolute"),
    ("C:evil.txt", "entry_absolute"),
    ("photos\\x.png", "entry_backslash"),
    ("notes.txt", "entry_not_allowed"),
    ("photos/notahash.png", "entry_not_allowed"),
    ("photos/" + "a" * 64 + ".heic", "entry_not_allowed"),
    ("other/", "entry_dir_not_photos"),
    ("bad\x01name", "entry_control_char"),
])
def test_zip_rejects_unsafe_entry_names(tmp_path, entry, code):
    z = _zip_with(tmp_path, "bad.zip", {entry: b"x"})
    r = inspect_package(z)
    assert r.verdict() == "reject" and code in codes(r), r.to_text()


def test_zip_rejects_duplicate_entry_names(tmp_path):
    def w(zf):
        with pytest.warns(UserWarning, match="Duplicate name"):
            zf.writestr(zipfile.ZipInfo("observations.jsonl", FIXED_TIME), b"{}\n")
    z = _zip_with(tmp_path, "dup.zip", writer=w)
    r = inspect_package(z)
    assert "entry_duplicate_name" in codes(r)


def test_zip_rejects_symlink_entry(tmp_path):
    def w(zf):
        info = zipfile.ZipInfo("photos/" + "b" * 64 + ".png", FIXED_TIME)
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, b"target")
    r = inspect_package(_zip_with(tmp_path, "link.zip", writer=w))
    assert "entry_symlink" in codes(r)


def _patch_central_header(z: Path, name: str, offset: int, value: bytes) -> None:
    """중앙 디렉터리의 해당 항목 헤더(46바이트)의 offset 위치를 덮어쓴다."""
    data = bytearray(z.read_bytes())
    with zipfile.ZipFile(z) as zf:
        start = zf.start_dir
    pos = start
    while True:
        assert data[pos:pos + 4] == b"PK\x01\x02", "central header expected"
        n_len = int.from_bytes(data[pos + 28:pos + 30], "little")
        e_len = int.from_bytes(data[pos + 30:pos + 32], "little")
        c_len = int.from_bytes(data[pos + 32:pos + 34], "little")
        if data[pos + 46:pos + 46 + n_len] == name.encode():
            data[pos + offset:pos + offset + len(value)] = value
            break
        pos += 46 + n_len + e_len + c_len
    z.write_bytes(bytes(data))


def test_zip_rejects_encrypted_flag(tmp_path, zip_of):
    z = zip_of("valid")
    _patch_central_header(z, "observations.jsonl", 8, (0x1).to_bytes(2, "little"))  # general purpose flag
    r = inspect_package(z)
    assert "entry_encrypted" in codes(r), r.to_text()


def test_zip_rejects_unsupported_compression(tmp_path):
    def w(zf):
        info = zipfile.ZipInfo("photos/" + "d" * 64 + ".png", FIXED_TIME)
        info.compress_type = zipfile.ZIP_BZIP2
        zf.writestr(info, b"x")
    r = inspect_package(_zip_with(tmp_path, "bz2.zip", writer=w))
    assert "entry_compression" in codes(r)


def test_not_a_zip(tmp_path):
    p = tmp_path / "x.j5field.zip"
    p.write_bytes(b"this is not a zip")
    r = inspect_package(p)
    assert r.verdict() == "reject" and "zip_bad_file" in codes(r)


def test_missing_manifest_and_observations(tmp_path):
    dst = tmp_path / "empty.zip"
    with zipfile.ZipFile(dst, "w") as zf:
        zf.writestr(zipfile.ZipInfo("photos/", FIXED_TIME), b"")
    r = inspect_package(dst)
    assert {"manifest_missing", "file_missing_in_package"} <= codes(r)


def test_extra_photo_not_in_manifest(tmp_path):
    png = (PACKAGES / "valid" / "photos").iterdir().__next__().read_bytes()
    other = bytes(png[:-1]) + b"\x00"  # 다른 내용 → 다른 해시
    import hashlib
    name = "photos/" + hashlib.sha256(other).hexdigest() + ".png"
    r = inspect_package(_zip_with(tmp_path, "extra.zip", {name: other}))
    assert {"file_not_in_manifest", "photo_unreferenced"} <= codes(r)
    assert "photo_magic_mismatch" not in codes(r)


def test_photo_magic_mismatch(tmp_path):
    import hashlib
    jpeg_like = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    name = "photos/" + hashlib.sha256(jpeg_like).hexdigest() + ".png"
    r = inspect_package(_zip_with(tmp_path, "magic.zip", {name: jpeg_like}))
    assert "photo_magic_mismatch" in codes(r)


def test_photo_name_not_content_hash(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    r = inspect_package(_zip_with(tmp_path, "name.zip", {"photos/" + "e" * 64 + ".png": png}))
    assert "photo_name_not_content_hash" in codes(r)


def test_understated_file_size_is_caught(tmp_path, zip_of):
    """헤더가 실제보다 작게 신고하면 읽기가 잘리거나 CRC가 틀려 거절된다 (한도 우회 시도)."""
    z = zip_of("valid")
    with zipfile.ZipFile(z) as zf:
        size = zf.getinfo("observations.jsonl").file_size
    _patch_central_header(z, "observations.jsonl", 24, (size - 5).to_bytes(4, "little"))
    r = inspect_package(z)
    assert r.verdict() == "reject"
    assert codes(r) & {"zip_crc", "zip_bad_file", "file_bytes_mismatch", "file_hash_mismatch"}, r.to_text()


def test_overstated_file_size_hashes_real_bytes(tmp_path, zip_of):
    """헤더를 부풀려도 실제 읽은 바이트로 해시·크기를 재므로 결과는 실제 내용 기준이다."""
    z = zip_of("valid")
    with zipfile.ZipFile(z) as zf:
        size = zf.getinfo("observations.jsonl").file_size
    _patch_central_header(z, "observations.jsonl", 24, (size + 5).to_bytes(4, "little"))
    r = inspect_package(z)
    assert r.verdict() == "ok", r.to_text()


def test_limits_uncompressed(tmp_path, zip_of):
    r = inspect_package(zip_of("valid"), limits=Limits(uncompressed=100))
    assert r.verdict() == "reject" and codes(r) & {"zip_declared_total", "total_size_exceeded", "entry_size_exceeded"}


def test_limits_photo_size(tmp_path, zip_of):
    r = inspect_package(zip_of("valid"), limits=Limits(photo=10))
    assert "photo_size_exceeded" in codes(r)


def test_limits_max_photos(tmp_path, zip_of):
    r = inspect_package(zip_of("valid"), limits=Limits(max_photos=1))
    assert codes(r) & {"photo_count_exceeded", "zip_entry_count"}


def test_limits_compressed(tmp_path, zip_of):
    r = inspect_package(zip_of("valid"), limits=Limits(compressed=10))
    assert "zip_compressed_size" in codes(r)


# ---- 이벤트 파일 형식 ----

def _variant(tmp_path: Path, raw: bytes) -> Path:
    """valid 패키지의 observations.jsonl만 바꾼 ZIP (manifest 불일치는 무시하고 이벤트 코드만 본다)."""
    src = PACKAGES / "valid"
    dst = tmp_path / "variant.zip"
    with zipfile.ZipFile(dst, "w") as zf:
        for p in sorted(x for x in src.rglob("*") if x.is_file()):
            rel = p.relative_to(src).as_posix()
            zf.writestr(zipfile.ZipInfo(rel, FIXED_TIME), raw if rel == "observations.jsonl" else p.read_bytes())
    return dst


def _valid_lines() -> list[bytes]:
    return [ln for ln in (PACKAGES / "valid" / "observations.jsonl").read_bytes().split(b"\n") if ln]


def test_obs_crlf(tmp_path):
    r = inspect_package(_variant(tmp_path, b"\r\n".join(_valid_lines()) + b"\r\n"))
    assert "obs_crlf" in codes(r)


def test_obs_bom(tmp_path):
    r = inspect_package(_variant(tmp_path, b"\xef\xbb\xbf" + b"\n".join(_valid_lines()) + b"\n"))
    assert "obs_bom" in codes(r)


def test_obs_no_trailing_newline_and_empty_line(tmp_path):
    lines = _valid_lines()
    r = inspect_package(_variant(tmp_path, lines[0] + b"\n\n" + lines[1]))
    assert {"obs_empty_line", "obs_no_trailing_newline"} <= codes(r)


def test_obs_duplicate_json_key(tmp_path):
    line = _valid_lines()[1]
    dup = line[:-1] + b',"asset_id":"7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50"}'
    r = inspect_package(_variant(tmp_path, dup + b"\n"))
    assert "obs_line_not_json" in codes(r)


def test_obs_not_utf8(tmp_path):
    r = inspect_package(_variant(tmp_path, b'{"a":"\xff"}\n'))
    assert "obs_not_utf8" in codes(r)


def test_event_corrects_self_and_unknown_target(tmp_path):
    ev = json.loads(_valid_lines()[1])
    ev["corrects_event_id"] = ev["event_id"]
    r = inspect_package(_variant(tmp_path, json.dumps(ev, sort_keys=True).encode() + b"\n"))
    assert "event_corrects_self" in codes(r)
    ev["corrects_event_id"] = "1a2b3c4d-0001-4000-8000-0000000000ff"
    r = inspect_package(_variant(tmp_path, json.dumps(ev, sort_keys=True).encode() + b"\n"))
    assert "corrects_target_unknown" in codes(r)


def test_attachment_mime_ext_mismatch(tmp_path):
    ev = json.loads(_valid_lines()[0])
    ev["attachment_refs"][0]["mime"] = "image/jpeg"
    r = inspect_package(_variant(tmp_path, json.dumps(ev, sort_keys=True).encode() + b"\n"))
    assert "attachment_mime_ext_mismatch" in codes(r)


# ---- CLI ----

def test_cli_exit_codes(capsys, zip_of):
    assert cli.main(["inspect", str(PACKAGES / "valid")]) == 0
    assert cli.main(["inspect", str(PACKAGES / "duplicate_conflict")]) == 2
    assert cli.main(["inspect", str(PACKAGES / "corrupt_hash_mismatch")]) == 1
    assert cli.main(["inspect", str(PACKAGES / "does_not_exist")]) == 3
    assert cli.main(["inspect", str(PACKAGES / "valid"), "--seed", str(PACKAGES / "valid" / "manifest.json")]) == 3
    assert cli.main(["bogus"]) == 3
    out = capsys.readouterr().out
    assert "판정: ok" in out


def test_cli_json_and_env_study_id(capsys, monkeypatch):
    monkeypatch.setenv("J5_STUDY_ID", "other")
    assert cli.main(["inspect", str(PACKAGES / "valid"), "--json"]) == 1
    d = json.loads(capsys.readouterr().out)
    assert any(f["code"] == "manifest_study_id_mismatch" for f in d["findings"])
    monkeypatch.setenv("J5_STUDY_ID", "j5-synthetic-study")
    assert cli.main(["inspect", str(PACKAGES / "valid"), "--json"]) == 0


def test_cli_survives_ascii_console():
    env = dict(os.environ, PYTHONIOENCODING="ascii")
    p = subprocess.run([sys.executable, "-m", "j5", "inspect", str(PACKAGES / "duplicate_conflict")],
                       capture_output=True, env=env, cwd=Path(__file__).resolve().parents[1])
    assert p.returncode == 2, p.stderr
    assert b"event_id_conflict" in p.stdout
