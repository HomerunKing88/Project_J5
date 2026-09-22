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
    for p in web_files(".js"):
        text = p.read_text(encoding="utf-8")
        for token in ("fetch(", "XMLHttpRequest", "WebSocket", "navigator.sendBeacon", "serviceWorker"):
            assert token not in text, f"{p}: {token} 사용 금지 (R1a 뼈대에서는 외부 통신·서비스 워커 없음)"


def test_no_vendor_yet():
    """고정 배포본(MapLibre·fflate)은 해당 단계(J5-005·007)에서 검증 후 넣는다."""
    assert not (WEB / "vendor").exists()
