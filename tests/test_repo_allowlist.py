"""저장소 허용목록 검사 (J5-002).

실데이터·사진·DB·비밀키 형식의 파일이 Git에 추적되고 있지 않은지 확인한다.
가상 fixture는 tests/fixtures 아래에서만 허용한다. 표준 라이브러리만 사용한다.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = "tests/fixtures/"

FORBIDDEN_SUFFIXES = {
    ".sqlite", ".sqlite3", ".db", ".db-journal", ".db-wal", ".db-shm",
    ".heic", ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".zip", ".pem", ".key", ".p12",
}
FORBIDDEN_DIR_PARTS = {
    "db", "raw", "photos", "inbox", "quarantine", "exports", "backups", "logs",
    "J5_DATA_HOME", "data",
}
FORBIDDEN_NAME_PATTERNS = [
    re.compile(r"^\.env(\..*)?$"),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
]
# 텍스트 파일 안의 비밀키 형태. 실제 키가 아닌 예시 문자열은 이 패턴에 걸리지 않게 둔다.
SECRET_CONTENT_PATTERNS = [
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(serviceKey|api_key|apikey|secret)\s*[=:]\s*['\"]?[A-Za-z0-9+/%_\-]{32,}"),
]
TEXT_SUFFIXES = {".md", ".py", ".js", ".html", ".css", ".json", ".jsonl", ".sql",
                 ".toml", ".yml", ".yaml", ".env", ".txt", ".cfg", ".ini"}


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT, check=True, capture_output=True,
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def is_fixture(path: str) -> bool:
    return path.startswith(FIXTURE_ROOT)


def test_no_forbidden_files_tracked() -> None:
    offenders: list[str] = []
    for path in tracked_files():
        p = Path(path)
        if p.suffix.lower() in FORBIDDEN_SUFFIXES and not is_fixture(path):
            offenders.append(f"{path}: 금지 확장자")
        if len(p.parts) > 1 and p.parts[0] in FORBIDDEN_DIR_PARTS:
            offenders.append(f"{path}: 루트 실데이터 폴더명")
        if p.name != "example.env" and any(rx.search(p.name) for rx in FORBIDDEN_NAME_PATTERNS):
            offenders.append(f"{path}: 비밀정보 파일명")
    assert not offenders, "허용목록 위반:\n" + "\n".join(offenders)


def test_no_secret_like_content() -> None:
    offenders: list[str] = []
    for path in tracked_files():
        p = REPO_ROOT / path
        if p.suffix.lower() not in TEXT_SUFFIXES or not p.is_file():
            continue
        if p.resolve() == Path(__file__).resolve():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for rx in SECRET_CONTENT_PATTERNS:
            if rx.search(text):
                offenders.append(f"{path}: {rx.pattern[:40]}")
    assert not offenders, "비밀키 형태 문자열 발견:\n" + "\n".join(offenders)


def test_fixture_size_limit() -> None:
    """가상 fixture는 작게 유지한다. 실사진 유입 방지용 상한(1MB)."""
    limit = 1_000_000
    big = [
        f"{path}: {(REPO_ROOT / path).stat().st_size} bytes"
        for path in tracked_files()
        if is_fixture(path) and (REPO_ROOT / path).stat().st_size > limit
    ]
    assert not big, "fixture 크기 초과:\n" + "\n".join(big)
