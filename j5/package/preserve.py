"""독립 사본 (J5-008). 원본을 바꾸지 않고, 덮어쓰지 않으며, 복사 후 다시 읽어 해시를 확인한다.

'독립'은 사용자가 위치를 고른 결과다. 이 도구는 같은 장치인지 정도만 알려줄 수 있다.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from j5.package.limits import DEFAULT_LIMITS, Limits


class PreserveError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass(frozen=True)
class CopyResult:
    dest: Path
    sidecar: Path
    sha256: str
    bytes: int
    same_device: bool


def _hash_file(p: Path, chunk: int) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            n += len(b)
            h.update(b)
    return h.hexdigest(), n


def copy_package(src: Path, dest_dir: Path, limits: Limits = DEFAULT_LIMITS) -> CopyResult:
    if not src.is_file():
        raise PreserveError("src_not_file", f"원본 파일이 없음: {src}")
    if not dest_dir.is_dir():
        raise PreserveError("dest_not_dir", f"대상 폴더가 없음: {dest_dir}")
    final = dest_dir / src.name
    sidecar = dest_dir / (src.name + ".sha256")
    part = dest_dir / (src.name + ".part")
    if src.resolve() == final.resolve():
        raise PreserveError("same_path", "원본과 대상이 같은 파일")
    if final.exists():
        raise PreserveError("dest_exists", f"대상에 같은 이름의 파일이 이미 있음: {final}")
    if sidecar.exists():
        raise PreserveError("sidecar_exists", f"해시 파일이 이미 있음: {sidecar}")
    h = hashlib.sha256()
    n = 0
    try:
        with open(src, "rb") as fi, open(part, "xb") as fo:
            while True:
                b = fi.read(limits.chunk)
                if not b:
                    break
                n += len(b)
                h.update(b)
                fo.write(b)
            fo.flush()
            os.fsync(fo.fileno())
        digest = h.hexdigest()
        back, back_n = _hash_file(part, limits.chunk)
        if back != digest or back_n != n:
            raise PreserveError("copy_verify_failed", "복사본을 다시 읽은 해시가 원본과 다름. 사본을 남기지 않음")
    except PreserveError:
        part.unlink(missing_ok=True)
        raise
    except OSError as e:
        part.unlink(missing_ok=True)
        raise PreserveError("copy_failed", str(e)) from e
    os.replace(part, final)
    with open(sidecar, "x", encoding="utf-8") as f:
        f.write(f"{digest}  {src.name}\n")
    same_device = src.stat().st_dev == final.stat().st_dev
    return CopyResult(final, sidecar, digest, n, same_device)
