"""J5-008: 독립 사본. 덮어쓰기 금지, 원본 불변, 복사 후 재검증."""

from __future__ import annotations

import hashlib

import pytest

from j5 import cli
from j5.package import preserve
from j5.package.preserve import PreserveError, copy_package


def test_copy_creates_file_and_sidecar(tmp_path, zip_of):
    src = zip_of("valid")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    dest = tmp_path / "backup"
    dest.mkdir()
    r = copy_package(src, dest)
    assert r.dest == dest / src.name and r.dest.read_bytes() == src.read_bytes()
    assert r.sha256 == before
    assert r.sidecar.read_text(encoding="utf-8") == f"{before}  {src.name}\n"
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert not (dest / (src.name + ".part")).exists()


def test_copy_refuses_overwrite(tmp_path, zip_of):
    src = zip_of("valid")
    dest = tmp_path / "backup"
    dest.mkdir()
    copy_package(src, dest)
    first = (dest / src.name).read_bytes()
    with pytest.raises(PreserveError) as e:
        copy_package(src, dest)
    assert e.value.code == "dest_exists"
    assert (dest / src.name).read_bytes() == first
    assert (dest / (src.name + ".sha256")).exists()


def test_copy_refuses_same_path_and_missing_inputs(tmp_path, zip_of):
    src = zip_of("valid")
    with pytest.raises(PreserveError) as e:
        copy_package(src, src.parent)
    assert e.value.code == "same_path"
    with pytest.raises(PreserveError) as e:
        copy_package(tmp_path / "nope.zip", tmp_path)
    assert e.value.code == "src_not_file"
    with pytest.raises(PreserveError) as e:
        copy_package(src, tmp_path / "missing_dir")
    assert e.value.code == "dest_not_dir"


def test_copy_detects_corrupted_copy(tmp_path, zip_of, monkeypatch):
    src = zip_of("valid")
    dest = tmp_path / "backup"
    dest.mkdir()
    real = preserve._hash_file

    def bad(p, chunk):
        h, n = real(p, chunk)
        return "0" * 64, n
    monkeypatch.setattr(preserve, "_hash_file", bad)
    with pytest.raises(PreserveError) as e:
        copy_package(src, dest)
    assert e.value.code == "copy_verify_failed"
    assert list(dest.iterdir()) == []


def test_cli_copy(capsys, tmp_path, zip_of):
    src = zip_of("valid")
    dest = tmp_path / "b"
    dest.mkdir()
    assert cli.main(["copy", str(src), str(dest)]) == 0
    out = capsys.readouterr().out
    assert "사본 작성됨" in out and "백업 완료를 뜻하지 않는다" in out
    assert cli.main(["copy", str(src), str(dest)]) == 1
