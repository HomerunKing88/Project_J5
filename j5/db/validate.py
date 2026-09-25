"""정본 입력의 공통 검증 (J5-009). ADR-09: record_type 과 허용 대상 타입의 조합, 시점 형식, payload 스키마를 한 곳에서 검사한다.

DB 의 CHECK 는 모양만 본다(UTC 글롭·열거값·JSON 유효성). 달력 유효성·시간대·정밀도 일치·payload 내용은 여기서 검사한다.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

from jsonschema import Draft202012Validator, FormatChecker

from j5.db.schema import DATA_MODES, PRECISIONS, RECORD_TYPES, SOURCE_KINDS, SUBJECT_TYPES
from j5.schemas_loader import SCHEMAS_DIR, schema_registry

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
DATETIME_OFFSET_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$")
UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

# 기록 종류별 허용 대상 타입. R1b 는 임장 관측 → 매입 검토 단위만. 점포(survey_unit)는 R2 에서 추가한다.
RECORD_TYPE_SUBJECTS: dict[str, frozenset[str]] = {
    "field_observation": frozenset({"asset"}),
    "target_price": frozenset({"asset"}),          # R4 J5-015A: 목표 매수가 (데이터 사전 §8)
    "investment_judgment": frozenset({"asset"}),   # R4 J5-015A: 투자판단 10항목 (초안 허용)
    "regulation_review": frozenset({"asset"}),     # R5 J5-016C: 규제 검토(용도지역·적용 용적률·규제 버전·제약) (데이터 사전 §9)
    "development_plan": frozenset({"asset"}),      # R5 J5-016C: 개발안(현상 유지·리모델링·철거신축·공동매입)과 여유면적 계산 스냅샷
    "financing_plan": frozenset({"asset"}),        # R5 J5-016C: 자금안(필요자기자본·최대 현금 계산 스냅샷) (데이터 사전 §10)
}
# 기록 종류별 payload 스키마 (schemas/ 의 $defs 참조) 와 payload 스키마 버전
RECORD_PAYLOAD_SCHEMAS: dict[str, tuple[str, str, str]] = {
    "field_observation": ("observation_event.schema.json", "field_observation_payload", "1.0.0"),
    "target_price": ("judgment_records.schema.json", "target_price_payload", "1.0.0"),
    "investment_judgment": ("judgment_records.schema.json", "investment_judgment_payload", "1.0.0"),
    "regulation_review": ("plan_records.schema.json", "regulation_review_payload", "1.0.0"),
    "development_plan": ("plan_records.schema.json", "development_plan_payload", "1.0.0"),
    "financing_plan": ("plan_records.schema.json", "financing_plan_payload", "1.0.0"),
}
assert set(RECORD_TYPE_SUBJECTS) == set(RECORD_TYPES) == set(RECORD_PAYLOAD_SCHEMAS)


class ValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def is_uuid(s) -> bool:
    return isinstance(s, str) and UUID_RE.match(s) is not None


def is_sha256(s) -> bool:
    return isinstance(s, str) and SHA256_RE.match(s) is not None


def parse_date(s) -> date | None:
    """YYYY-MM-DD 이고 달력상 존재하는 날짜면 date, 아니면 None."""
    if not isinstance(s, str) or not DATE_RE.match(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_datetime(s) -> datetime | None:
    """시간대 오프셋이 있는 ISO 8601 이면 aware datetime, 아니면 None. 오프셋 없는 시각은 거절한다 (데이터 사전 §1)."""
    if not isinstance(s, str) or not DATETIME_OFFSET_RE.match(s):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_utc_iso(s) -> bool:
    """정본 시각 형식: UTC, 초 단위, 'Z'."""
    return isinstance(s, str) and UTC_RE.match(s) is not None and parse_datetime(s) is not None


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("시간대 없는 시각은 정본 시각으로 변환하지 않는다")
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> str:
    return to_utc_iso(datetime.now(timezone.utc))


def observed_at_errors(value, precision, at: str = "observed_at") -> list[str]:
    if precision not in PRECISIONS:
        return [f"observed_at_precision 는 {'/'.join(PRECISIONS)} 중 하나"]
    if precision == "date":
        return [] if parse_date(value) else [f"{at}: 날짜 정밀도는 YYYY-MM-DD 이고 존재하는 날짜여야 한다"]
    return [] if parse_datetime(value) else [f"{at}: 시각 정밀도는 시간대 오프셋이 있는 ISO 8601 이어야 한다"]


_payload_validators: dict[str, Draft202012Validator] = {}


def payload_validator(record_type: str) -> Draft202012Validator:
    v = _payload_validators.get(record_type)
    if v is None:
        file, definition, _ = RECORD_PAYLOAD_SCHEMAS[record_type]
        root = json.loads((SCHEMAS_DIR / file).read_text(encoding="utf-8"))
        # $id 를 유지해 다른 스키마 파일로의 상대 $ref(예: calc_inputs.schema.json#/$defs/far)가 registry 에서 풀리게 한다
        schema = {"$id": root["$id"], "$ref": f"#/$defs/{definition}", "$defs": root["$defs"]}
        Draft202012Validator.check_schema(schema)
        v = _payload_validators[record_type] = Draft202012Validator(schema, format_checker=FormatChecker(), registry=schema_registry())
    return v


def record_errors(rec: dict) -> list[str]:
    """records 행(딕셔너리)의 오류 목록. 비어 있으면 통과. DB 제약과 겹치는 검사도 여기서 먼저 잡아 메시지를 명확히 한다."""
    errs: list[str] = []
    if not is_uuid(rec.get("record_id")):
        errs.append("record_id 는 소문자 UUID")
    if not is_uuid(rec.get("subject_id")):
        errs.append("subject_id 는 소문자 UUID")
    st, rt = rec.get("subject_type"), rec.get("record_type")
    if st not in SUBJECT_TYPES:
        errs.append(f"subject_type 는 {'/'.join(SUBJECT_TYPES)} 중 하나")
    if rt not in RECORD_TYPES:
        errs.append(f"record_type 는 {'/'.join(RECORD_TYPES)} 중 하나")
    elif st in SUBJECT_TYPES and st not in RECORD_TYPE_SUBJECTS[rt]:
        errs.append(f"record_type {rt} 의 대상은 {'/'.join(sorted(RECORD_TYPE_SUBJECTS[rt]))} 만 허용 (subject_type={st})")
    if rec.get("source_kind") not in SOURCE_KINDS:
        errs.append(f"source_kind 는 {'/'.join(SOURCE_KINDS)} 중 하나")
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        errs.append("payload 는 JSON 객체")
    elif rt in RECORD_TYPES:
        expected_version = RECORD_PAYLOAD_SCHEMAS[rt][2]
        if rec.get("schema_version") != expected_version:
            errs.append(f"schema_version 은 {rt} 의 현재 payload 스키마 {expected_version} 이어야 한다")
        for e in payload_validator(rt).iter_errors(payload):
            errs.append(f"payload/{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message}")
    errs += observed_at_errors(rec.get("observed_at"), rec.get("observed_at_precision"))
    for key in ("device_created_at", "source_published_at", "collected_at"):
        v = rec.get(key)
        if v is not None and not parse_datetime(v):
            errs.append(f"{key}: 시간대 오프셋이 있는 ISO 8601 또는 null")
    if not is_utc_iso(rec.get("recorded_at")):
        errs.append("recorded_at 은 UTC 'YYYY-MM-DDTHH:MM:SSZ' (PC 가 부여)")
    for key in ("effective_from", "effective_to"):
        v = rec.get(key)
        if v is not None and not (parse_date(v) or parse_datetime(v)):
            errs.append(f"{key}: 날짜 또는 시간대 있는 시각 또는 null")
    sup = rec.get("supersedes_id")
    if sup is not None:
        if not is_uuid(sup):
            errs.append("supersedes_id 는 UUID 또는 null")
        elif sup == rec.get("record_id"):
            errs.append("supersedes_id 가 자기 자신을 가리킨다")
    return errs


def seed_asset_errors(a: dict, data_mode: str) -> list[str]:
    """시드 물건 한 건이 정본 assets 로 들어갈 수 있는지. 스키마 검증은 호출자가 먼저 한다."""
    errs: list[str] = []
    if data_mode not in DATA_MODES:
        errs.append("정본의 data_mode 가 잘못됨")
    if a.get("data_mode") != data_mode:
        errs.append(f"물건 {a.get('asset_id')} 의 data_mode({a.get('data_mode')}) 가 정본({data_mode})과 다르다")
    if parse_date(a.get("created_at")) is None:
        errs.append(f"물건 {a.get('asset_id')} 의 created_at 이 존재하는 날짜가 아니다")
    return errs
