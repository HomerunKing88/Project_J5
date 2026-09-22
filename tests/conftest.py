from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PACKAGES = FIXTURES / "packages"
FIXED_TIME = (2026, 9, 22, 9, 0, 0)


def zip_dir(src: Path, dst: Path, *, compression=zipfile.ZIP_DEFLATED) -> Path:
    """디렉터리를 결정적으로 ZIP으로 묶는다 (고정 시각, 이름 정렬)."""
    files = sorted(p for p in src.rglob("*") if p.is_file())
    with zipfile.ZipFile(dst, "w", compression=compression) as zf:
        for p in files:
            info = zipfile.ZipInfo(p.relative_to(src).as_posix(), date_time=FIXED_TIME)
            info.compress_type = compression
            zf.writestr(info, p.read_bytes())
    return dst


@pytest.fixture
def zip_of(tmp_path):
    def _make(case: str) -> Path:
        return zip_dir(PACKAGES / case, tmp_path / f"{case}.j5field.zip")
    return _make
