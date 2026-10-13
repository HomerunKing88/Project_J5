"""J5-069: 앱 파일이 바뀌면 서비스 워커 캐시 이름을 올린다 (PR #115 리뷰 P1 의 재발 방지).

서비스 워커는 캐시 이름(`web/sw.js` 의 CACHE)이 같으면 이미 설치한 폰에 캐시해 둔 앱 파일을 계속 준다. 앱 파일을 바꾸고 이름을 올리지 않으면
폰이 이전 코드로 새 자료를 읽는다. 고정값 파일(`tests/fixtures/app_cache.lock.json`)에 캐시 이름과 캐시 목록(APP_FILES) 파일들의 해시를 적어 두고,
파일이 바뀌었는데 이름이 그대로면 실패한다. 캐시 이름과 앱 버전(`APP_VERSION`)이 같은지도 본다.

이름을 올린 뒤 고정값을 갱신한다: `python tests/test_j5_069_app_cache_lock.py --write`
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
LOCK = ROOT / "tests" / "fixtures" / "app_cache.lock.json"
UPDATE = "python tests/test_j5_069_app_cache_lock.py --write"


def cache_name(sw_text: str) -> str:
    m = re.search(r'const CACHE = CACHE_PREFIX \+ "(v[0-9]+\.[0-9]+\.[0-9]+)"', sw_text)
    assert m, "sw.js 에서 CACHE 이름을 찾지 못했다"
    return m.group(1)


def app_files(sw_text: str) -> list[str]:
    """캐시 목록(APP_FILES)의 파일 (web 기준 상대경로, "./" 는 index.html, 중복 없이 정렬)."""
    files = re.findall(r'"(\./[^"]*)"', sw_text.split("APP_FILES")[1].split("];")[0])
    return sorted({f[2:] or "index.html" for f in files})


def digest(web: Path = WEB) -> tuple[str, str, list[str]]:
    """(캐시 이름, 캐시 목록 파일들의 해시, 파일 목록). 해시는 파일마다 '경로\\0내용 sha256' 을 이어 다시 sha256 한 값이다."""
    sw = (web / "sw.js").read_text(encoding="utf-8")
    files = app_files(sw)
    h = hashlib.sha256()
    for rel in files:
        h.update(f"{rel}\0{hashlib.sha256((web / rel).read_bytes()).hexdigest()}\n".encode("utf-8"))
    return cache_name(sw), h.hexdigest(), files


def test_app_version_matches_cache_name():
    cache, _, _ = digest()
    m = re.search(r'export const APP_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"', (WEB / "app" / "main.js").read_text(encoding="utf-8"))
    assert m and f"v{m.group(1)}" == cache, f"main.js APP_VERSION({m and m.group(1)})과 sw.js CACHE({cache})가 다르다. 둘을 같이 올린다"


def test_cache_name_bumped_when_app_files_change():
    cache, sha, files = digest()
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if lock["cache"] == cache:
        assert lock["sha256"] == sha, (f"캐시 목록의 앱 파일이 바뀌었는데 sw.js CACHE({cache})가 그대로다. 이미 설치한 폰은 이전 파일을 계속 쓴다."
                                       f" sw.js CACHE 와 main.js APP_VERSION 을 올리고(e2e 의 버전 기대값도) `{UPDATE}` 로 고정값을 갱신한다")
    else:
        raise AssertionError(f"sw.js CACHE 가 {lock['cache']} → {cache} 로 바뀌었다. `{UPDATE}` 로 고정값을 갱신한다")
    assert lock["files"] == files, f"캐시 목록이 바뀌었다. `{UPDATE}` 로 고정값을 갱신한다"


def test_guard_catches_unbumped_change(tmp_path):
    """시험 자체: 앱 파일 하나를 바꾸고 이름을 그대로 두면 해시가 달라지고, 이름만 올려도 이름이 달라진다."""
    import shutil
    web = tmp_path / "web"
    shutil.copytree(WEB, web, ignore=shutil.ignore_patterns("data"))
    cache, sha, _ = digest(web)
    (web / "app" / "view.js").write_text((web / "app" / "view.js").read_text(encoding="utf-8") + "\n// 바뀜\n", encoding="utf-8")
    cache2, sha2, _ = digest(web)
    assert cache2 == cache and sha2 != sha
    sw = (web / "sw.js").read_text(encoding="utf-8")
    (web / "sw.js").write_text(sw.replace(f'"{cache}"', '"v9.9.9"'), encoding="utf-8")
    assert digest(web)[0] == "v9.9.9" and digest(web)[1] == sha2, "sw.js 는 해시에 들지 않는다 (캐시 이름 줄만 바뀌는 파일)"


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        print(f"사용: {UPDATE}", file=sys.stderr)
        sys.exit(2)
    cache, sha, files = digest()
    LOCK.write_text(json.dumps({"cache": cache, "sha256": sha, "files": files, "note": "J5-069: sw.js 의 CACHE 와 캐시 목록 파일들의 해시. 앱 파일을 바꾸면 CACHE 를 올리고 이 파일을 다시 쓴다"},
                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{LOCK.relative_to(ROOT)}: {cache} {sha[:12]}… ({len(files)}개 파일)")
