"""web/ 정적 앱 규칙 (J5-004): 외부 통신 없음, CSP 있음, ES Modules, 인라인 스크립트 없음."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
URL = re.compile(r"https?://", re.IGNORECASE)


def web_files(*suffixes: str) -> list[Path]:
    return sorted(p for p in WEB.rglob("*") if p.is_file() and p.suffix in suffixes)


def test_no_external_urls_in_app_code():
    for p in web_files(".html", ".js", ".css"):
        assert not URL.search(p.read_text(encoding="utf-8")), f"{p}: 외부 URL 금지 (ADR-01 숨은 통신 차단)"


def test_every_page_has_csp_and_no_inline_script():
    for p in web_files(".html"):
        text = p.read_text(encoding="utf-8")
        assert 'http-equiv="Content-Security-Policy"' in text, p
        assert "default-src 'self'" in text, p
        assert re.search(r"<script(?![^>]*\bsrc=)", text) is None, f"{p}: 인라인 스크립트 금지"
        assert re.search(r"\son\w+=", text) is None, f"{p}: 인라인 이벤트 핸들러 금지"
        for m in re.finditer(r'<script[^>]*src="([^"]+)"', text):
            assert 'type="module"' in m.group(0), f"{p}: ES Modules만"
            assert (p.parent / m.group(1)).is_file(), f"{p}: {m.group(1)} 없음"


def test_no_network_apis_in_js():
    """외부 통신 금지. fetch 는 같은 출처의 상대경로 리터럴만, 서비스 워커는 main.js 등록과 sw.js 만 허용."""
    for p in web_files(".js"):
        text = p.read_text(encoding="utf-8")
        for token in ("XMLHttpRequest", "WebSocket", "navigator.sendBeacon", "EventSource"):
            assert token not in text, f"{p}: {token} 사용 금지"
        for m in re.finditer(r"\bfetch\(\s*([^)]*)", text):
            arg = m.group(1).strip()
            if p.name == "sw.js":
                assert arg.startswith("e.request"), f"{p}: 서비스 워커는 받은 요청만 전달"
            else:
                assert re.match(r'^"(\./)?[a-z][a-z0-9_./-]*"', arg), f"{p}: fetch 는 상대경로 리터럴만 허용: {arg}"
        if "serviceWorker" in text:
            assert p.name in ("main.js", "sw.js"), f"{p}: 서비스 워커는 main.js 등록·sw.js 만"


def test_service_worker_caches_only_app_files():
    """ADR-01: 서비스 워커에는 앱 실행 파일만 캐시한다. 시드·사진·데이터는 넣지 않는다."""
    sw = (WEB / "sw.js").read_text(encoding="utf-8")
    files = re.findall(r'"(\./[^"]*)"', sw.split("APP_FILES")[1].split("];")[0])
    assert files, "APP_FILES 비어 있음"
    for f in files:
        rel = f[2:] or "index.html"
        assert not rel.startswith(("data/", "photos/")), f"데이터 캐시 금지: {f}"
        assert (WEB / rel).is_file(), f"캐시 목록의 파일이 없음: {f}"
    for js in web_files(".js"):
        rel = js.relative_to(WEB).as_posix()
        if rel not in ("sw.js", "app/devtest.js"):
            assert f"./{rel}" in files, f"앱 모듈이 캐시 목록에 없음: {rel}"


def test_bundled_seed_matches_fixture():
    a = (WEB / "data" / "assets.seed.synthetic.json").read_bytes()
    b = (Path(__file__).resolve().parent / "fixtures" / "assets.seed.synthetic.json").read_bytes()
    assert a == b, "web/data 의 가상 시드는 tests/fixtures 와 같아야 한다"


def test_no_html_string_injection_in_js():
    """파일명 등 외부 값이 DOM 에 마크업으로 들어가지 않도록 innerHTML·outerHTML·insertAdjacentHTML 을 쓰지 않는다."""
    for p in web_files(".js"):
        text = p.read_text(encoding="utf-8")
        for token in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert token not in text, f"{p}: {token} 사용 금지"


def test_no_vendor_yet():
    """외부 고정 배포본은 없다. ZIP 은 자체 STORED 작성기(ADR-11), 지도는 자체 SVG 점 지도(ADR-12).
    MapLibre 는 타일 이용조건이 확인되는 시점에 새 ADR 로 검토한다."""
    assert not (WEB / "vendor").exists()
