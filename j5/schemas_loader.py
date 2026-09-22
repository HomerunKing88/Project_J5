"""저장소 루트 schemas/ 의 JSON Schema를 읽어 검증기를 만든다."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"


class SchemaNotFound(RuntimeError):
    pass


@lru_cache(maxsize=None)
def get_validator(name: str) -> Draft202012Validator:
    path = SCHEMAS_DIR / name
    if not path.is_file():
        raise SchemaNotFound(f"스키마 파일이 없다: {path}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def schema_errors(name: str, doc) -> list[str]:
    v = get_validator(name)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message}" for e in v.iter_errors(doc)]
