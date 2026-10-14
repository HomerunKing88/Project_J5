"""공개 배포 대상 검사 (J5-059·060, ADR-24).

정적 호스팅은 web/ 폴더를 그대로 올린다. 그 안에는 앱 코드·문서와 가상자료만 있어야 한다(AGENTS "공개 앱에는 코드·가상자료만").
허용한 확장자·이름 밖의 파일, 가상자료가 아닌 data/ 파일, 실데이터 형식(사진·ZIP·DB·번들)이 web/ 에 생기면 실패한다.
배포 헤더(web/_headers)가 앱이 쓰는 권한을 막지 않는지, 배포 설정(wrangler.jsonc)이 web/ 만 올리고 서버 코드·비밀값을 두지 않는지도 본다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
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
    # J5-078: VWorld 격리 프레임 두 주소만 같은 출처의 틀을 허용한다(앞 규칙의 틀 금지를 떼고). 나머지 CSP 는 페이지 meta
    assert csp == ["Content-Security-Policy: frame-ancestors 'none'"] + ["Content-Security-Policy: frame-ancestors 'self'"] * 2, "헤더 CSP 는 frame-ancestors 만"
    blocks = re.split(r"\n(?=/)", "\n".join(rules))
    framed = [b for b in blocks if "frame-ancestors 'self'" in b]
    assert [b.splitlines()[0] for b in framed] == ["/vworld-frame.html", "/vworld-frame"], "틀 허용은 격리 프레임 주소만"
    assert all("! Content-Security-Policy" in b and "! X-Frame-Options" in b and "X-Frame-Options: SAMEORIGIN" in b for b in framed)
    assert "/sw.js" in rules and "Cache-Control: no-cache" in rules, "서비스 워커는 매번 새로 확인"
    assert not re.search(r"https?://", text), "헤더 파일에 외부 주소를 두지 않는다"


def test_wrangler_config_publishes_only_web():
    """Cloudflare 의 배포 명령(wrangler deploy)이 읽는 설정. 저장소 전체가 아니라 web/ 만 정적 자산으로 올린다(J5-060).
    서버 코드(main)·환경 값(vars)·저장소 연결(bindings)을 두지 않는다. 설정은 주석 없는 JSON 으로 둔다(여기서 그대로 읽는다)."""
    cfg = json.loads((ROOT / "wrangler.jsonc").read_text(encoding="utf-8"))
    assert set(cfg) == {"name", "compatibility_date", "assets"}, f"정적 자산 밖의 설정: {sorted(set(cfg) - {'name', 'compatibility_date', 'assets'})}"
    assert cfg["name"] == "project-j5", "Cloudflare 의 Worker 이름과 같아야 빌드가 이 설정을 쓴다"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", cfg["compatibility_date"])
    assets = cfg["assets"]
    assert (ROOT / assets["directory"]).resolve() == WEB, "올리는 폴더는 web/ 하나"
    assert "binding" not in assets and "run_worker_first" not in assets, "서버 코드 없이 파일만 내준다"
    assert assets.get("not_found_handling") == "none", "없는 주소는 404 (index.html 로 바꿔 내주지 않는다)"
    assert assets.get("html_handling") == "auto-trailing-slash", "index.html 넘김은 서비스 워커가 처리한다(J5-059)"
