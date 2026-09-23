"""실데이터 홈의 config.env 읽기 (J5-014). KEY=VALUE 줄, `#` 주석, 따옴표 허용. 값은 돌려주되 절대 출력하지 않는다.

인증키는 환경변수 `DATA_GO_KR_SERVICE_KEY` 가 있으면 그것을, 없으면 `J5_DATA_HOME/config.env` 의 같은 이름을 쓴다.
저장소 안의 파일(.env 예시 포함)에서는 읽지 않는다.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

CONFIG_NAME = "config.env"
SERVICE_KEY_NAME = "DATA_GO_KR_SERVICE_KEY"


class ConfigError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_config(data_home: Path | None, *, path: Path | None = None) -> tuple[dict[str, str], Path | None]:
    """(설정, 읽은 파일 경로). 파일이 없으면 빈 설정. 다른 사용자가 읽을 수 있는 권한이면 경고용 코드로 알린다(값은 그대로 읽는다)."""
    p = path if path is not None else (Path(data_home) / CONFIG_NAME if data_home is not None else None)
    if p is None or not p.is_file():
        return {}, None
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError("config_unreadable", f"{p} 를 읽을 수 없다: {type(e).__name__}") from e
    return parse_env(text), p


def config_permission_warning(p: Path | None) -> str | None:
    if p is None or os.name == "nt":
        return None
    try:
        mode = stat.S_IMODE(p.stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return f"{p} 의 권한이 {oct(mode)} 이다. `chmod 600 {p}` 로 본인만 읽게 한다"
    return None


def service_key(data_home: Path | None, *, config_path: Path | None = None) -> tuple[str, str]:
    """(인증키, 출처 설명). 환경변수 우선, 없으면 config.env. 둘 다 없으면 ConfigError."""
    env = os.environ.get(SERVICE_KEY_NAME, "").strip()
    if env:
        return env, f"환경변수 {SERVICE_KEY_NAME}"
    cfg, p = load_config(data_home, path=config_path)
    key = (cfg.get(SERVICE_KEY_NAME) or "").strip()
    if not key:
        where = str(p) if p else (str(Path(data_home) / CONFIG_NAME) if data_home else "J5_DATA_HOME/config.env")
        raise ConfigError("service_key_missing", f"{SERVICE_KEY_NAME} 가 없다. {where} 에 `{SERVICE_KEY_NAME}=<키>` 한 줄을 두거나 환경변수로 준다 (저장소·채팅에 넣지 않는다)")
    return key, str(p)


def redact(text: str, key: str | None) -> str:
    """로그·결과에 키가 섞이지 않게 한다. 원문 키와 퍼센트 부호화 형태 모두 가린다."""
    if not key:
        return text
    from urllib.parse import quote, unquote
    out = text.replace(key, "<redacted>")
    for variant in {quote(key, safe=""), unquote(key)}:
        if variant and variant != key:
            out = out.replace(variant, "<redacted>")
    return out
