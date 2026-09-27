"""web/ 정적 앱 규칙 (J5-004): 외부 통신 없음, CSP 있음, ES Modules, 인라인 스크립트 없음."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
URL = re.compile(r"https?://", re.IGNORECASE)


def web_files(*suffixes: str) -> list[Path]:
    return sorted(p for p in WEB.rglob("*") if p.is_file() and p.suffix in suffixes)


# ADR-15 (J5-021): 사용자가 누르는 외부 지도 링크만 app/extmap.js 에 둔다. 다른 파일에는 외부 URL 이 없다.
EXT_MAP_FILE = "app/extmap.js"
EXT_MAP_HOSTS = {"maps.apple.com", "map.naver.com", "map.kakao.com", "www.google.com"}
HOST = re.compile(r"https://([a-z0-9.-]+)/", re.IGNORECASE)


# ADR-18 (J5-024): 배경 타일 호스트. index.html 의 CSP img-src 와 app/tiles.js·sw.js 의 허용목록에만 나온다 (사용자가 설정에서 켠 경우만 요청).
TILE_HOSTS = {"api.vworld.kr"}
CSP_IMG_SRC = "img-src 'self' blob: " + " ".join(f"https://{h}" for h in sorted(TILE_HOSTS))


def test_no_external_urls_in_app_code():
    for p in web_files(".html", ".js", ".css"):
        rel = p.relative_to(WEB).as_posix()
        if rel == EXT_MAP_FILE:
            continue
        text = p.read_text(encoding="utf-8")
        if rel == "index.html":
            assert CSP_IMG_SRC in text, "index.html CSP 의 img-src 는 타일 허용 호스트만"
            text = text.replace(CSP_IMG_SRC, "")
        if rel == "app/tiles.js":
            # 타일 모듈은 스킴 문자열("https://" 결합·검사)만 쓰고 호스트가 붙은 완성 URL 은 쓰지 않는다 (호스트는 TILE_HOSTS 허용목록에만)
            assert not re.search(r"https?://[A-Za-z0-9]", text), f"{p}: 완성된 외부 URL 금지 (호스트는 TILE_HOSTS 에만)"
            continue
        assert not URL.search(text), f"{p}: 외부 URL 금지 (ADR-01 숨은 통신 차단, 외부 지도 링크는 {EXT_MAP_FILE} 에만, 타일 호스트는 CSP 에만)"


def test_tile_hosts_allowlist_is_consistent():
    """ADR-18: tiles.js·sw.js 의 허용 호스트가 CSP 와 같고, 타일 주소는 사용자 설정에서만 온다(코드에 완성된 타일 URL 없음)."""
    tiles = (WEB / "app" / "tiles.js").read_text(encoding="utf-8")
    sw = (WEB / "sw.js").read_text(encoding="utf-8")
    hosts_js = set(re.findall(r'TILE_HOSTS = Object\.freeze\(\[([^\]]*)\]\)', tiles)[0].replace('"', "").replace(" ", "").split(","))
    hosts_sw = set(re.findall(r'TILE_HOSTS = \[([^\]]*)\]', sw)[0].replace('"', "").replace(" ", "").split(","))
    assert hosts_js == TILE_HOSTS == hosts_sw
    for pat in (r"\bfetch\s*\(", r"^\s*import\b", r"\blocation\b", r"\bdocument\.", r"\bwindow\b", r"\bnavigator\b"):
        assert not re.search(pat, tiles, re.MULTILINE), f"tiles.js: {pat} 사용 금지 (계산·URL 만)"


def test_external_map_links_are_link_only():
    """ADR-15: extmap.js 는 허용한 지도 호스트의 https 문자열만 만들고, 통신·이동·저장 API 를 쓰지 않는다."""
    text = (WEB / EXT_MAP_FILE).read_text(encoding="utf-8")
    hosts = {m.group(1).lower() for m in HOST.finditer(text)}
    assert hosts == EXT_MAP_HOSTS, f"허용목록 밖 호스트: {hosts ^ EXT_MAP_HOSTS}"
    assert not re.search(r"http://", text, re.IGNORECASE), "http 링크 금지"
    for token in ("fetch", "import", "location", "window.open", "navigator", "localStorage", "indexedDB", "document."):
        assert token not in text, f"{EXT_MAP_FILE}: {token} 사용 금지 (링크 문자열만 만든다)"
    for token in ("label", "asset_id", "pnu", "note"):
        assert not re.search(rf"\b{token}\b", text), f"{EXT_MAP_FILE}: {token} 를 링크에 넣지 않는다 (좌표와 고정 문구만)"
    main = (WEB / "app" / "main.js").read_text(encoding="utf-8")
    for h in EXT_MAP_HOSTS:
        assert h not in main, f"main.js 에 지도 호스트 직접 사용 금지: {h}"


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
