"""규제 검토·개발안·자금안 기록 (J5-016C, R5). 데이터 사전 §2·§9·§10, 릴리스 계획 §8.

- `apply_plan_input`: 입력 파일(`schemas/plan_records.schema.json`, kind: plan_record)을 불변 기록(records)으로 넣는다. 개발안은 여유면적(calc far), 자금안은 필요자기자본·최대 현금(calc equity/cash)을
  저장 시점에 계산해 결과 스냅샷과 계산식 버전을 payload 에 붙인다. 미확인 입력이 있으면 결과는 null 과 미확인 목록이다(거절하지 않는다). 수정은 새 기록 + supersedes_id.
- `plan_overview`: 물건의 현재(수정되지 않은) 규제 검토·개발안·자금안을 나란히 보인다. 후보를 고르지 않는다.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from j5.calc import CALCULATION_VERSION
from j5.calc.cash import max_required_equity
from j5.calc.equity import required_equity
from j5.calc.far import far_headroom
from j5.calc.plans import PLAN_KIND_LABEL
from j5.db.store import Db
from j5.db.validate import RECORD_PAYLOAD_SCHEMAS, ValidationError, parse_date
from j5.schemas_loader import schema_errors

INPUT_SCHEMA = "plan_records.schema.json"
PLAN_TYPES = ("regulation_review", "development_plan", "financing_plan")
TYPE_LABEL = {"regulation_review": "규제 검토", "development_plan": "개발안", "financing_plan": "자금안"}
STAGE_LABEL = {"decision_notice": "결정고시", "draft_notice": "입안·열람공고", "review_result": "심의결과", "press_release": "보도자료", "other": "기타", None: "단계 미확인"}
SOURCE_LABEL = {"official_fact": "공식 자료", "broker_report": "중개사 전달", "personal_estimate": "개인 추정", "scenario_assumption": "시나리오 가정", "calculated_result": "계산 결과"}


def regulation_in_force(p: dict) -> bool:
    """현재 효력이 확인된 규제인지: 결정고시이고 효력일이 있을 때만. 보도자료·심의결과·입안공고는 효력으로 보지 않는다(데이터 사전 §6)."""
    return p.get("document_stage") == "decision_notice" and p.get("effective_on") is not None


def regulation_status(p: dict, source_kind: str) -> str:
    if source_kind != "official_fact":
        return f"가정 ({SOURCE_LABEL.get(source_kind, source_kind)})"
    return "효력 확인" if regulation_in_force(p) else f"효력 미확인 ({STAGE_LABEL.get(p.get('document_stage'))}{', 효력일 없음' if p.get('effective_on') is None else ''})"


def load_plan_input(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValidationError([f"입력 파일을 읽을 수 없다: {e}"]) from e
    errs = schema_errors(INPUT_SCHEMA, doc)
    if errs:
        raise ValidationError([f"입력이 {INPUT_SCHEMA} 에 맞지 않는다"] + errs[:10])
    if parse_date(doc["observed_at"]) is None:
        raise ValidationError([f"검토일이 달력상 존재하지 않는다: {doc['observed_at']}"])
    return doc


def _compute_payload(doc: dict, regulation: dict | None = None) -> dict:
    """입력 payload 에 저장 시점의 계산 결과와 계산식 버전을 붙인다. 규제 검토는 그대로.
    개발안이 전제한 규제 검토(regulation)가 효력 확인된 결정고시가 아니거나 공식 자료가 아니면 여유면적 결과를 확정하지 않는다."""
    p = dict(doc["payload"])
    rt = doc["record_type"]
    if rt == "development_plan":
        if p["far_input"] is not None:
            r = far_headroom(p["far_input"])
            unknown = list(r["unknown"])
            if regulation is not None:
                rp, rk = regulation["payload"], regulation["source_kind"]
                if rk != "official_fact":
                    unknown.append(f"전제한 규제 검토가 공식 자료가 아니라 {SOURCE_LABEL.get(rk, rk)}이다. 적용 용적률을 확정하지 않는다")
                elif not regulation_in_force(rp):
                    unknown.append(f"전제한 규제 검토가 효력 확인된 결정고시가 아니다 ({STAGE_LABEL.get(rp.get('document_stage'))}{', 효력일 없음' if rp.get('effective_on') is None else ''}). 적용 용적률을 확정하지 않는다")
            p["far_result"] = {"result": r["result"] if not unknown else None, "unknown": unknown, "errors": r["errors"], "inputs": r["inputs"]}
        else:
            p["far_result"] = None
        p["calculation_version"] = CALCULATION_VERSION
    elif rt == "financing_plan":
        eq = required_equity(p["equity_input"])
        ca = max_required_equity(p["cash_input"])
        p["equity_result"] = {"result_krw": eq["result_krw"], "known_sum_krw": eq["known_sum_krw"], "components_krw": eq["components_krw"],
                              "deposit_return_obligation_krw": eq["deposit_return_obligation_krw"], "remaining_cash_now_krw": eq["remaining_cash_now_krw"], "unknown": eq["unknown"]}
        p["cash_result"] = {"result_krw": ca["result_krw"], "known_sum_krw": ca["known_sum_krw"], "min_cumulative_krw": ca["min_cumulative_krw"], "min_at": ca["min_at"],
                            "periods": ca["periods"], "unknown": ca["unknown"]}
        p["calculation_version"] = CALCULATION_VERSION
    return p


def apply_plan_input(db: Db, doc: dict) -> dict:
    """한 트랜잭션으로 기록·근거를 넣고 dataset_version 을 올린다. 물건·이전 기록·근거 문서·참조 기록은 정본에 있어야 한다."""
    rt = doc["record_type"]
    with db.transaction():
        if db.conn.execute("SELECT 1 FROM assets WHERE asset_id = ?", (doc["asset_id"],)).fetchone() is None:
            raise ValidationError([f"물건 {doc['asset_id']} 이 정본에 없다"])
        sup = doc.get("supersedes_id")
        if sup is not None:
            prev = db.conn.execute("SELECT subject_id, record_type FROM records WHERE record_id = ?", (sup,)).fetchone()
            if prev is None:
                raise ValidationError([f"이전 기록 {sup} 이 정본에 없다"])
            if prev["subject_id"] != doc["asset_id"] or prev["record_type"] != rt:
                raise ValidationError([f"이전 기록 {sup} 은 같은 물건·같은 종류의 기록이어야 한다 ({prev['subject_id']}, {prev['record_type']})"])
            if db.conn.execute("SELECT 1 FROM records WHERE supersedes_id = ?", (sup,)).fetchone() is not None:
                raise ValidationError([f"이전 기록 {sup} 은 이미 다른 기록으로 수정됐다. 최신 기록을 가리킨다"])
        for ev in doc.get("evidence", []):
            if db.conn.execute("SELECT 1 FROM source_documents WHERE document_id = ?", (ev["document_id"],)).fetchone() is None:
                raise ValidationError([f"근거 문서 {ev['document_id']} 가 정본에 없다 (source_documents 에 먼저 둔다)"])
        regulation = None
        for key, want in (("regulation_review_id", "regulation_review"), ("development_plan_id", "development_plan")):
            ref = doc["payload"].get(key)
            if ref is not None:
                row = db.conn.execute("SELECT subject_id, record_type, source_kind, payload_json FROM records WHERE record_id = ?", (ref,)).fetchone()
                if row is None or row["subject_id"] != doc["asset_id"] or row["record_type"] != want:
                    raise ValidationError([f"{key} {ref} 은 이 물건의 {TYPE_LABEL[want]} 기록이어야 한다"])
                if db.conn.execute("SELECT 1 FROM records WHERE supersedes_id = ?", (ref,)).fetchone() is not None:
                    raise ValidationError([f"{key} {ref} 은 이미 수정된 기록이다. 최신 기록을 가리킨다"])
                if want == "regulation_review":
                    regulation = {"source_kind": row["source_kind"], "payload": json.loads(row["payload_json"])}
        payload = _compute_payload(doc, regulation)
        rid = str(uuid.uuid4())
        rec = {"record_id": rid, "subject_id": doc["asset_id"], "subject_type": "asset", "record_type": rt, "source_kind": doc["source_kind"],
               "schema_version": RECORD_PAYLOAD_SCHEMAS[rt][2], "payload": payload, "observed_at": doc["observed_at"], "observed_at_precision": "date", "supersedes_id": sup}
        evidence = [{"document_id": ev["document_id"], "field_path": ev.get("field_path", "$"), "locator": ev.get("locator"),
                     "verification_status": ev.get("verification_status", "unverified")} for ev in doc.get("evidence", [])]
        db.insert_record(rec, evidence=evidence)
        version = db.bump_dataset_version()
    out = {"record_id": rid, "record_type": rt, "asset_id": doc["asset_id"], "supersedes_id": sup, "dataset_version": version, "calculation_version": payload.get("calculation_version")}
    if rt == "development_plan":
        fr = payload["far_result"]
        out["far"] = None if fr is None else fr["result"]
        out["unknown"] = [] if fr is None else fr["unknown"] + fr["errors"]
    elif rt == "financing_plan":
        out["required_equity_krw"] = payload["equity_result"]["result_krw"]
        out["max_required_equity_krw"] = payload["cash_result"]["result_krw"]
        out["unknown"] = payload["equity_result"]["unknown"] + payload["cash_result"]["unknown"]
    else:
        out["unknown"] = [k for k in ("issuing_agency", "notice_number", "document_stage", "effective_on", "zoning", "applied_far_pct", "regulation_version") if payload.get(k) is None]
        out["regulation_status"] = regulation_status(payload, doc["source_kind"])
    return out


def plan_add_text(r: dict) -> str:
    lines = [f"기록 추가: {TYPE_LABEL[r['record_type']]}({r['record_type']}) {r['record_id']} · 물건 {r['asset_id']}" + (f" (이전 기록 {r['supersedes_id']} 를 수정)" if r["supersedes_id"] else "")
             + f" · dataset_version {r['dataset_version']}"]
    if r["record_type"] == "regulation_review":
        lines.append(f"규제 상태: {r['regulation_status']}")
    if r["record_type"] == "development_plan":
        lines.append("여유면적: " + (f"검토 {r['far']['review_area_m2']:g}㎡ · 여유 {r['far']['headroom_m2']:g}㎡" if r["far"] else "미확정 (입력 없음 또는 미확인)"))
    elif r["record_type"] == "financing_plan":
        fmt = lambda v: f"{v:,}원" if v is not None else "미확정"  # noqa: E731
        lines.append(f"필요자기자본 {fmt(r['required_equity_krw'])} · 최대 필요자기자본 {fmt(r['max_required_equity_krw'])}")
    for u in r.get("unknown", []):
        lines.append(f"  미확인: {u}")
    if r.get("calculation_version"):
        lines.append(f"계산식 {r['calculation_version']} 으로 저장 시점에 계산한 스냅샷이다. 입력이 바뀌면 새 기록으로 다시 계산한다")
    return "\n".join(lines) + "\n"


def plan_overview(db: Db, asset_id: str) -> dict:
    asset = db.conn.execute("SELECT asset_id, label FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise ValidationError([f"물건 {asset_id} 이 정본에 없다"])
    rows = db.conn.execute(
        "SELECT r.record_id, r.record_type, r.observed_at, r.recorded_at, r.supersedes_id, r.source_kind, r.payload_json,"
        " (SELECT COUNT(*) FROM records n WHERE n.supersedes_id = r.record_id) AS superseded"
        " FROM records r WHERE r.subject_id = ? AND r.record_type IN ('regulation_review', 'development_plan', 'financing_plan') ORDER BY r.observed_at, r.recorded_at", (asset_id,)).fetchall()
    out = {"asset_id": asset_id, "label": asset["label"], "regulation_reviews": [], "development_plans": [], "financing_plans": [], "superseded": 0}
    for r in rows:
        if r["superseded"]:
            out["superseded"] += 1
            continue
        p = json.loads(r["payload_json"])
        item = {"record_id": r["record_id"], "observed_at": r["observed_at"], "recorded_at": r["recorded_at"], "source_kind": r["source_kind"], "supersedes_id": r["supersedes_id"], "payload": p}
        out[{"regulation_review": "regulation_reviews", "development_plan": "development_plans", "financing_plan": "financing_plans"}[r["record_type"]]].append(item)
    out["note"] = "현재(수정되지 않은) 기록만 보인다. 결과는 저장 시점의 계산 스냅샷이며 후보를 고르지 않는다. 미확인 입력이 있는 기록은 값이 없다"
    return out


def plan_overview_text(o: dict) -> str:
    fmt = lambda v: f"{v:,}원" if v is not None else "미확정"  # noqa: E731
    lines = [f"규제·개발안·자금안 {o['label']} ({o['asset_id']}): 규제 검토 {len(o['regulation_reviews'])}, 개발안 {len(o['development_plans'])}, 자금안 {len(o['financing_plans'])}"
             + (f" · 수정된 옛 기록 {o['superseded']}건 제외" if o["superseded"] else "")]
    for it in o["regulation_reviews"]:
        p = it["payload"]
        official = it["source_kind"] == "official_fact"
        lines.append(f"[규제 검토{'' if official else '·가정'}] {it['record_id'][:8]} · {regulation_status(p, it['source_kind'])} · 자료 {SOURCE_LABEL.get(it['source_kind'], it['source_kind'])} · 검토일 {it['observed_at']}"
                     f" · {p.get('issuing_agency') or '기관 미확인'} {p.get('notice_number') or '고시번호 미확인'} · {STAGE_LABEL.get(p.get('document_stage'))} · 효력일 {p.get('effective_on') or '미확인'} · 종료일 {p.get('end_on') or '-'}"
                     f" · 용도지역 {p['zoning'] or '미확인'} · 적용 용적률 {p['applied_far_pct'] if p['applied_far_pct'] is not None else '미확인'}%"
                     f" · 규제 버전 {p['regulation_version'] or '미확인'} · 종료일 확인 {p['end_date_confirmed']} · 제약 {len(p['constraints'])}건")
        if not official:
            lines.append("    주의: 공식 자료가 아닌 가정값이다. 결정고시 확인 전에는 적용 용적률로 쓰지 않는다")
    for it in o["development_plans"]:
        p = it["payload"]
        fr = p.get("far_result")
        far = "여유면적 미확정" if not fr or not fr["result"] else f"검토 {fr['result']['review_area_m2']:g}㎡ · 여유 {fr['result']['headroom_m2']:g}㎡ · 소진율 {fr['result']['utilization'] * 100:.1f}%"
        lines.append(f"[개발안·{PLAN_KIND_LABEL[p['plan_kind']]}] {it['record_id'][:8]} {p['label']} · 판단일 {it['observed_at']} · {far} · 계산식 {p['calculation_version']}"
                     + (f" · 공사 {p['construction']['months']}개월" if p.get("construction") and p["construction"].get("months") is not None else "")
                     + (" · 건축사 검토" if (p.get("architect_review") or {}).get("reviewed") else ""))
        for u in (fr or {}).get("unknown", []) + (fr or {}).get("errors", []):
            lines.append(f"    미확인: {u}")
    for it in o["financing_plans"]:
        p = it["payload"]
        eq, ca = p["equity_result"], p["cash_result"]
        lines.append(f"[자금안] {it['record_id'][:8]} {p['scenario']} · 판단일 {it['observed_at']} · 필요자기자본 {fmt(eq['result_krw'])} · 최대 필요자기자본 {fmt(ca['result_krw'])} (누적 최저 {ca['min_cumulative_krw']:,} @ t={ca['min_at']})"
                     + (f" · 개발안 {p['development_plan_id'][:8]}" if p.get("development_plan_id") else "") + f" · 계산식 {p['calculation_version']}")
        for u in eq["unknown"] + ca["unknown"]:
            lines.append(f"    미확인: {u}")
    lines.append(o["note"])
    return "\n".join(lines) + "\n"
