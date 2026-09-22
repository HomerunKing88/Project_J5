"""패키지 읽기. ZIP과 풀어 놓은 디렉터리를 같은 인터페이스로 다룬다.

헤더의 크기를 믿지 않고 실제 읽은 바이트로 한도를 강제한다. 입력을 수정·삭제하지 않는다.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from j5.package.limits import DEFAULT_LIMITS, Limits

ALLOWED_NAME = re.compile(r"^(manifest\.json|observations\.jsonl|photos/[0-9a-f]{64}\.(jpg|png|webp))$")
_DRIVE = re.compile(r"^[A-Za-z]:")


class ContainerError(Exception):
    """컨테이너(ZIP·디렉터리) 수준의 치명적 문제. 검사는 여기서 중단한다."""

    def __init__(self, code: str, path: str, message: str):
        super().__init__(f"{code} {path}: {message}")
        self.code, self.path, self.message = code, path, message


@dataclass(frozen=True)
class Entry:
    name: str
    declared_size: int


def check_entry_name(name: str) -> None:
    """항목 이름 안전성. 실패 시 ContainerError."""
    if name.startswith("/") or _DRIVE.match(name):
        raise ContainerError("entry_absolute", name, "절대경로 항목")
    if "\\" in name:
        raise ContainerError("entry_backslash", name, "백슬래시 경로")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ContainerError("entry_control_char", name, "제어문자 포함 이름")
    parts = PurePosixPath(name).parts
    if ".." in parts or "." in parts:
        raise ContainerError("entry_traversal", name, "경로 탈출 항목")
    if name.endswith("/"):
        if name != "photos/":
            raise ContainerError("entry_dir_not_photos", name, "photos/ 외 디렉터리 항목")
        return
    if not ALLOWED_NAME.match(name):
        raise ContainerError("entry_not_allowed", name, "허용되지 않은 파일 이름")


def sha256_stream(f: BinaryIO, max_bytes: int, chunk: int, path: str,
                  code: str = "entry_size_exceeded") -> tuple[bytes, str]:
    h = hashlib.sha256()
    buf = bytearray()
    total = 0
    while True:
        b = f.read(chunk)
        if not b:
            break
        total += len(b)
        if total > max_bytes:
            raise ContainerError(code, path, f"항목 크기 한도 {max_bytes} 바이트 초과")
        h.update(b)
        buf += b
    return bytes(buf), h.hexdigest()


class _Source:
    kind = ""

    def __init__(self, limits: Limits):
        self.limits = limits
        self._total_read = 0

    def _account(self, n: int, path: str) -> None:
        self._total_read += n
        if self._total_read > self.limits.uncompressed:
            raise ContainerError("total_size_exceeded", path, f"전체 해제 크기 한도 {self.limits.uncompressed} 바이트 초과")

    def entries(self) -> list[Entry]:
        raise NotImplementedError

    def read(self, name: str, max_bytes: int, code: str = "entry_size_exceeded") -> tuple[bytes, str]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ZipSource(_Source):
    kind = "zip"

    def __init__(self, path: Path, limits: Limits = DEFAULT_LIMITS):
        super().__init__(limits)
        self.path = path
        size = path.stat().st_size
        if size > limits.compressed:
            raise ContainerError("zip_compressed_size", str(path), f"ZIP 크기 {size} 바이트가 한도 {limits.compressed}를 넘음")
        try:
            self.zf = zipfile.ZipFile(path, "r")
        except zipfile.BadZipFile as e:
            raise ContainerError("zip_bad_file", str(path), f"ZIP 파일이 아니거나 손상됨: {e}") from e
        infos = self.zf.infolist()
        if len(infos) > limits.max_entries:
            raise ContainerError("zip_entry_count", str(path), f"항목 수 {len(infos)}가 한도 {limits.max_entries}를 넘음")
        declared = sum(i.file_size for i in infos)
        if declared > limits.uncompressed:
            raise ContainerError("zip_declared_total", str(path), f"선언된 해제 크기 {declared}가 한도 {limits.uncompressed}를 넘음")
        seen: set[str] = set()
        self._entries: list[Entry] = []
        for info in infos:
            name = info.filename
            check_entry_name(name)
            if info.flag_bits & 0x1:
                raise ContainerError("entry_encrypted", name, "암호화된 항목")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ContainerError("entry_compression", name, f"지원하지 않는 압축 방식 {info.compress_type}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ContainerError("entry_symlink", name, "심볼릭 링크 항목")
            if name in seen:
                raise ContainerError("entry_duplicate_name", name, "중복 항목 이름")
            seen.add(name)
            if not name.endswith("/"):
                self._entries.append(Entry(name, info.file_size))
        self._entries.sort(key=lambda e: e.name)

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def read(self, name: str, max_bytes: int, code: str = "entry_size_exceeded") -> tuple[bytes, str]:
        try:
            with self.zf.open(name, "r") as f:
                data, digest = sha256_stream(f, max_bytes, self.limits.chunk, name, code)
        except zipfile.BadZipFile as e:
            raise ContainerError("zip_crc", name, f"항목 손상(CRC·길이 불일치): {e}") from e
        self._account(len(data), name)
        return data, digest

    def close(self) -> None:
        self.zf.close()


class DirSource(_Source):
    kind = "dir"

    def __init__(self, path: Path, limits: Limits = DEFAULT_LIMITS):
        super().__init__(limits)
        self.path = path
        entries: list[Entry] = []
        count = 0
        for root, dirs, files in os.walk(path, followlinks=False):
            rel_root = Path(root).relative_to(path)
            for d in list(dirs):
                rel = (rel_root / d).as_posix() + "/"
                if (Path(root) / d).is_symlink():
                    raise ContainerError("entry_symlink", rel, "심볼릭 링크 디렉터리")
                check_entry_name(rel)
            for fn in files:
                rel = (rel_root / fn).as_posix()
                full = Path(root) / fn
                if full.is_symlink():
                    raise ContainerError("entry_symlink", rel, "심볼릭 링크 파일")
                check_entry_name(rel)
                count += 1
                if count > limits.max_entries:
                    raise ContainerError("zip_entry_count", str(path), f"항목 수가 한도 {limits.max_entries}를 넘음")
                entries.append(Entry(rel, full.stat().st_size))
        self._entries = sorted(entries, key=lambda e: e.name)

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def read(self, name: str, max_bytes: int, code: str = "entry_size_exceeded") -> tuple[bytes, str]:
        with open(self.path / name, "rb") as f:
            data, digest = sha256_stream(f, max_bytes, self.limits.chunk, name, code)
        self._account(len(data), name)
        return data, digest


def open_package(path: Path, limits: Limits = DEFAULT_LIMITS) -> _Source:
    if path.is_dir():
        return DirSource(path, limits)
    if path.is_file():
        return ZipSource(path, limits)
    raise FileNotFoundError(path)
