"""계산 입력 파일 읽기·검증·실행 (schemas/calc_inputs.schema.json)."""

from __future__ import annotations

import json
from pathlib import Path

from j5.calc.cash import cash_text, max_required_equity
from j5.calc.equity import equity_text, required_equity
from j5.calc.far import far_headroom, far_text
from j5.db.validate import ValidationError
from j5.schemas_loader import schema_errors

INPUT_SCHEMA = "calc_inputs.schema.json"
RUNNERS = {"far": (far_headroom, far_text), "equity": (required_equity, equity_text), "cash": (max_required_equity, cash_text)}


def load_calc_input(path: Path, expected_kind: str | None = None) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValidationError([f"입력 파일을 읽을 수 없다: {e}"]) from e
    errs = schema_errors(INPUT_SCHEMA, doc)
    if errs:
        raise ValidationError([f"입력이 {INPUT_SCHEMA} 에 맞지 않는다"] + errs[:10])
    if expected_kind is not None and doc["kind"] != expected_kind:
        raise ValidationError([f"입력 kind 가 {doc['kind']} 인데 {expected_kind} 계산을 요청했다"])
    return doc


def run_calc(doc: dict) -> dict:
    fn, _ = RUNNERS[doc["kind"]]
    return fn(doc)


def calc_text(r: dict) -> str:
    _, fn = RUNNERS[r["kind"]]
    return fn(r)
