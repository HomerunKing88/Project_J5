"""공개 배포 대상 검사 (J5-059, ADR-24).

정적 호스팅은 web/ 폴더를 그대로 올린다. 그 안에는 앱 코드·문서와 가상자료만 있어야 한다(AGENTS "공개 앱에는 코드·가상자료만").
허용한 확장자·이름 밖의 파일, 가상자료가 아닌 data/ 파일, 실데이터 형식(사진·ZIP·DB·번들)이 web/ 에 생기면 실패한다.
배포 헤더(web/_headers)가 앱이 쓰는 권한을 막지 않는지도 본다.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CODE_SUFFIXES = {".html", ".js", ".css", ".svg", ".md"}
CONFIG_FILES = {"_headers"}
SYNTHETIC_DATA = re.compile(r"^data/[a-z0-9_.-]+\.synthetic(\.[a-z0-9_]+)?\.json$")


def published_files() -> list[str]:
    return sorted(p.relative_to(WEB).as_posix() for p in WEB.rglob("*") if p.is_file())


def test_only_code_and_synthetic_data_are_published():
    bad = []
    for rel in published_files():
        if rel in CONFIG_FILES:
            continue
        if rel.startswith("data/"):
            if not SYNTHETIC_DATA.match(rel):
                bad.append(f"{rel}: data/ 에는 가상자료(*.synthetic.*.json)만")
            continue
        if Path(rel).suffix.lower() not in CODE_SUFFIXES:
            bad.append(f"{rel}: 공개 배포에 올리지 않는 형식")
    assert not bad, "web/ 공개 배포 위반:\n" + "\n".join(bad)


def test_synthetic_data_is_marked_synthetic():
    for rel in published_files():
        if rel.startswith("data/"):
            text = (WEB / rel).read_text(encoding="utf-8")
            assert '"synthetic"' in text, f"{rel}: data_mode synthetic 표시가 없다"
            assert '"private_real"' not in text and '"data_mode": "real"' not in text, f"{rel}: 실제 자료 표시가 있다"


def test_headers_keep_app_permissions():
    text = (WEB / "_headers").read_text(encoding="utf-8")
    rules = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    pp = next(ln for ln in rules if ln.startswith("Permissions-Policy:"))
    assert "geolocation=(self)" in pp, "내 위치(ADR-20)는 이 앱에서만 허용"
    assert "camera=" not in pp, "사진 찍기(파일 입력)를 막지 않는다"
    csp = [ln for ln in rules if ln.startswith("Content-Security-Policy:")]
    assert csp == ["Content-Security-Policy: frame-ancestors 'none'"], "헤더 CSP 는 frame-ancestors 만 (나머지는 페이지 meta)"
    assert "/sw.js" in rules and "Cache-Control: no-cache" in rules, "서비스 워커는 매번 새로 확인"
    assert not re.search(r"https?://", text), "헤더 파일에 외부 주소를 두지 않는다"
