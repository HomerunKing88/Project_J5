"""매입 준비 검토·진입조건 (J5-018A, R7). 데이터 사전 §11, 릴리스 계획 §9, ADR-07.

- `apply_case_input`: 매입 준비 검토 기록(`schemas/acquisition_review.schema.json`, kind: acquisition_review)을 불변 기록(records)으로 넣는다. 체크리스트 8항목 각 1회, verified 는 검토일 필수,
  not_applicable 은 사유 필수, conditional·blocked 는 미해결 사항 필수. 참조 기록(목표 매수가·투자판단·자금안·개발안·규제 검토)과 근거 문서는 이 물건의 현재 기록·정본 문서여야 한다.
  물건마다 현재(수정되지 않은) 검토 기록은 하나이며 새 검토는 이전 검토를 supersedes 로 가리킨다.
- `evaluate`: 현재 검토 기록의 진입조건을 판정한다. 항목마다 `ok / not_applicable / expired(유효기한 경과) / incomplete / conditional / blocked / recheck(변경 감지)`.
  중요 미확인(계약 총액·보증금 명세·필요자기자본·여유면적·규제 효력·참조 기록 수정)을 모으고, 검토 기록이 정본에 들어온 뒤의 변경(가격·대출·전략·규제·구성·실사)을 신호로 잡아
  그 변경을 재검토 조건으로 둔 항목을 `recheck` 로 바꾼다. ready 는 모든 항목이 ok 또는 사유 있는 not_applicable 이고 미확인·변경 신호가 없을 때다.
  이미 purchase_ready 인 물건에 준비가 아닌 상태나 승인 뒤 변경이 있으면 `release_required`(해제 필요)다.
- `approve` / `withdraw`: purchase_ready 전환은 사용자 승인이며 ready 가 아니면 거절한다. 철회는 사유와 함께 남긴다. 둘 다 assets.tracking_status 를 바꾸고 readiness_decisions 에
  불변 행(이전·새 상태, 판정 스냅샷)을 남겨 과거 승인 이력을 보존한다. 은행 사전 한도는 실행 확약이 아니며 이 도구는 계약·송금을 자동화하지 않는다.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path

from j5.db.plans import regulation_in_force
from j5.db.store import Db
from j5.db.validate import RECORD_PAYLOAD_SCHEMAS, ValidationError, now_utc, parse_date
from j5.schemas_loader import schema_errors

INPUT_SCHEMA = "acquisition_review.schema.json"
RECORD_TYPE = "acquisition_review"
CHECK_KEYS = ("scope_price", "financing", "tax_legal", "rights_tenancy", "building_land", "business_funding", "negotiation", "final_decision")
CHECK_LABEL = {"scope_price": "매입 범위·가격", "financing": "금융", "tax_legal": "세무·법적 검토", "rights_tenancy": "권리·임대차", "building_land": "건축·토지",
               "business_funding": "사업·자금", "negotiation": "계약 협상", "final_decision": "최종 의사결정"}
STATUS_LABEL = {"not_started": "미착수", "in_progress": "진행 중", "verified": "확인됨", "conditional": "조건부", "blocked": "차단", "not_applicable": "해당 없음"}
VERDICT_LABEL = {"ok": "확인 유효", "not_applicable": "해당 없음(사유 있음)", "expired": "유효기한 경과", "incomplete": "미완료", "conditional": "조건부(미완료)", "blocked": "차단", "recheck": "변경 감지·재검토 필요"}
TRIGGER_LABEL = {"price": "가격", "loan": "대출", "deposit": "임대보증금", "strategy": "전략", "composition": "구성 토지", "regulation": "규제", "due_diligence": "중요 실사 결과", "tenancy": "임대차"}
# 참조 기록 종류 → (검토가 전제한 기록이 바뀌었을 때의 변경 종류)
REF_TYPES = {"target_price_id": ("target_price", ("price",)), "investment_judgment_id": ("investment_judgment", ("strategy",)),
             "financing_plan_id": ("financing_plan", ("loan", "deposit")), "development_plan_id": ("development_plan", ("strategy",)), "regulation_review_id": ("regulation_review", ("regulation",))}
# 검토 뒤 정본에 들어온 새 기록 종류 → 변경 종류
NEW_RECORD_TRIGGERS = {"target_price": "price", "financing_plan": "loan", "development_plan": "strategy", "regulation_review": "regulation"}
WITHDRAWN_STATUS = "detailed_review"


def load_case_input(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValidationError([f"입력 파일을 읽을 수 없다: {e}"]) from e
    errs = schema_errors(INPUT_SCHEMA, doc)
    if errs:
        raise ValidationError([f"입력이 {INPUT_SCHEMA} 에 맞지 않는다"] + errs[:10])
    _validate_dates(doc)
    return doc


def _validate_dates(doc: dict) -> None:
    if parse_date(doc["observed_at"]) is None:
        raise ValidationError([f"검토일이 달력상 존재하지 않는다: {doc['observed_at']}"])
    keys = [c["key"] for c in doc["payload"]["checks"]]
    if sorted(keys) != sorted(CHECK_KEYS):
        raise ValidationError([f"체크리스트는 8항목 각 1회여야 한다: {keys}"])
    for c in doc["payload"]["checks"]:
        for f in ("checked_at", "valid_until"):
            if c[f] is not None and parse_date(c[f]) is None:
                raise ValidationError([f"{c['key']}.{f} 가 달력상 존재하지 않는다: {c[f]}"])
        if c["checked_at"] and c["valid_until"] and c["valid_until"] < c["checked_at"]:
            raise ValidationError([f"{c['key']}: 유효기한이 검토일보다 앞선다"])
        if c["checked_at"] and c["checked_at"] > doc["observed_at"]:
            raise ValidationError([f"{c['key']}: 항목 검토일이 기록의 검토일보다 뒤다"])


def current_case(db: Db, asset_id: str) -> dict | None:
    """수정되지 않은 현재 매입 준비 검토 기록. 없으면 None."""
    r = db.conn.execute("SELECT rowid, record_id, observed_at, recorded_at, supersedes_id, payload_json FROM records r WHERE subject_id = ? AND record_type = ?"
                        " AND NOT EXISTS (SELECT 1 FROM records n WHERE n.supersedes_id = r.record_id) ORDER BY recorded_at DESC, rowid DESC LIMIT 1", (asset_id, RECORD_TYPE)).fetchone()
    if r is None:
        return None
    return {"record_id": r["record_id"], "observed_at": r["observed_at"], "recorded_at": r["recorded_at"], "supersedes_id": r["supersedes_id"], "payload": json.loads(r["payload_json"]), "rowid": r["rowid"]}


def apply_case_input(db: Db, doc: dict) -> dict:
    with db.transaction():
        asset = db.conn.execute("SELECT asset_id, label, tracking_status FROM assets WHERE asset_id = ?", (doc["asset_id"],)).fetchone()
        if asset is None:
            raise ValidationError([f"물건 {doc['asset_id']} 이 정본에 없다"])
        sup = doc.get("supersedes_id")
        cur = current_case(db, doc["asset_id"])
        if sup is not None:
            prev = db.conn.execute("SELECT subject_id, record_type FROM records WHERE record_id = ?", (sup,)).fetchone()
            if prev is None or prev["subject_id"] != doc["asset_id"] or prev["record_type"] != RECORD_TYPE:
                raise ValidationError([f"이전 기록 {sup} 은 같은 물건의 매입 준비 검토 기록이어야 한다"])
            if db.conn.execute("SELECT 1 FROM records WHERE supersedes_id = ?", (sup,)).fetchone() is not None:
                raise ValidationError([f"이전 기록 {sup} 은 이미 다른 기록으로 수정됐다. 최신 기록을 가리킨다"])
        elif cur is not None:
            raise ValidationError([f"이 물건에는 현재 검토 기록 {cur['record_id']} 이 있다. 새 검토는 supersedes_id 로 그 기록을 가리킨다 (물건마다 현재 검토는 하나)"])
        p = doc["payload"]
        doc_ids = {ev["document_id"] for ev in doc.get("evidence", [])} | {d for c in p["checks"] for d in c["evidence_ids"]}
        for did in sorted(doc_ids):
            if db.conn.execute("SELECT 1 FROM source_documents WHERE document_id = ?", (did,)).fetchone() is None:
                raise ValidationError([f"근거 문서 {did} 가 정본에 없다 (source_documents 에 먼저 둔다)"])
        for key, (want, _) in REF_TYPES.items():
            ref = p["references"][key]
            if ref is None:
                continue
            row = db.conn.execute("SELECT subject_id, record_type FROM records WHERE record_id = ?", (ref,)).fetchone()
            if row is None or row["subject_id"] != doc["asset_id"] or row["record_type"] != want:
                raise ValidationError([f"{key} {ref} 은 이 물건의 {want} 기록이어야 한다"])
            if db.conn.execute("SELECT 1 FROM records WHERE supersedes_id = ?", (ref,)).fetchone() is not None:
                raise ValidationError([f"{key} {ref} 은 이미 수정된 기록이다. 최신 기록을 가리킨다"])
        rid = str(uuid.uuid4())
        rec = {"record_id": rid, "subject_id": doc["asset_id"], "subject_type": "asset", "record_type": RECORD_TYPE, "source_kind": doc["source_kind"],
               "schema_version": RECORD_PAYLOAD_SCHEMAS[RECORD_TYPE][2], "payload": p, "observed_at": doc["observed_at"], "observed_at_precision": "date", "supersedes_id": sup}
        evidence = [{"document_id": ev["document_id"], "field_path": ev.get("field_path", "$"), "locator": ev.get("locator"), "verification_status": ev.get("verification_status", "unverified")}
                    for ev in doc.get("evidence", [])]
        db.insert_record(rec, evidence=evidence)
        version = db.bump_dataset_version()
    return {"record_id": rid, "asset_id": doc["asset_id"], "label": asset["label"], "supersedes_id": sup, "dataset_version": version, "tracking_status": asset["tracking_status"],
            "checks": {c["key"]: c["status"] for c in p["checks"]}}


def case_add_text(r: dict) -> str:
    lines = [f"매입 준비 검토 기록 추가: {r['record_id']} · 물건 {r['label']} ({r['asset_id']})" + (f" · 이전 기록 {r['supersedes_id']} 를 수정" if r["supersedes_id"] else "") + f" · dataset_version {r['dataset_version']}",
             "항목: " + ", ".join(f"{CHECK_LABEL[k]} {STATUS_LABEL[v]}" for k, v in r["checks"].items()),
             f"관심 단계는 그대로다 ({r['tracking_status']}). purchase_ready 전환은 `j5 db readiness --asset` 으로 판정한 뒤 `readiness-approve` 로 승인한다"]
    return "\n".join(lines) + "\n"


# ---- 판정 ----

def _ref_record(db: Db, ref: str | None) -> dict | None:
    if ref is None:
        return None
    r = db.conn.execute("SELECT record_id, record_type, source_kind, payload_json, recorded_at, (SELECT n.record_id FROM records n WHERE n.supersedes_id = r.record_id ORDER BY n.recorded_at LIMIT 1) AS superseded_by"
                        " FROM records r WHERE record_id = ?", (ref,)).fetchone()
    if r is None:
        return {"record_id": ref, "missing": True}
    return {"record_id": ref, "record_type": r["record_type"], "source_kind": r["source_kind"], "payload": json.loads(r["payload_json"]), "recorded_at": r["recorded_at"], "superseded_by": r["superseded_by"], "missing": False}


def _unknowns_and_ref_changes(db: Db, p: dict) -> tuple[list[str], list[dict]]:
    """중요 미확인 항목과 참조 기록 변경."""
    unknown: list[str] = []
    ref_changes: list[dict] = []
    if p["price"]["gross_contract_krw"] is None:
        unknown.append(f"계약 총액 미확인 ({p['price']['reason']})")
    if p["price"]["deposit_total_krw"] is None:
        unknown.append(f"보증금 명세 미확인 ({p['price']['deposit_basis']})")
    if p["price"]["vat_status"] == "unknown":
        unknown.append("부가세 포함 여부 미확인")
    if p["price"]["asking_confirmation"] == "unknown":
        unknown.append("호가 확인 수준 미확인")
    refs = p["references"]
    if refs["target_price_id"] is None:
        unknown.append("목표 매수가 기록 없음")
    if refs["financing_plan_id"] is None:
        unknown.append("자금안 기록 없음 (필요자기자본·최대 필요자기자본 미확정)")
    for key, (want, triggers) in REF_TYPES.items():
        r = _ref_record(db, refs[key])
        if r is None:
            continue
        if r["missing"]:
            unknown.append(f"{key} {refs[key]} 이 정본에 없다")
            continue
        if r["superseded_by"]:
            ref_changes.append({"reference": key, "record_id": refs[key], "superseded_by": r["superseded_by"], "triggers": list(triggers), "recorded_at": r["recorded_at"]})
        pl = r["payload"]
        if want == "financing_plan":
            if pl["equity_result"]["result_krw"] is None:
                unknown.append("자금안의 필요자기자본이 미확정이다: " + "; ".join(pl["equity_result"]["unknown"][:3]))
            if pl["cash_result"]["result_krw"] is None:
                unknown.append("자금안의 최대 필요자기자본이 미확정이다: " + "; ".join(pl["cash_result"]["unknown"][:3]))
        elif want == "development_plan":
            fr = pl.get("far_result")
            if pl.get("plan_kind") != "keep" and (fr is None or fr.get("result") is None):
                unknown.append("개발안의 여유면적이 미확정이다" + (": " + "; ".join((fr or {}).get("unknown", [])[:2]) if fr else ""))
        elif want == "regulation_review":
            if r["source_kind"] != "official_fact":
                unknown.append("전제한 규제 검토가 공식 자료가 아니다 (가정값)")
            elif not regulation_in_force(pl):
                unknown.append("전제한 규제 검토가 효력 확인된 결정고시가 아니다")
            elif pl.get("end_date_confirmed") is False:
                unknown.append("전제한 규제의 종료일이 미확인이다 (재확인 필요)")
    return unknown, ref_changes


def _signals_since(db: Db, asset_id: str, since: str, since_rowid: int) -> list[dict]:
    """검토 기록이 정본에 들어온 뒤의 변경. 기록은 정본 반영 시각과 같은 초면 삽입 순서(rowid)로 뒤를 가린다. 변경 종류(trigger)를 붙인다."""
    out: list[dict] = []
    for r in db.conn.execute("SELECT record_id, record_type, observed_at, recorded_at, supersedes_id, json_extract(payload_json, '$.change_status') AS cs FROM records"
                             " WHERE subject_id = ? AND (recorded_at > ? OR (recorded_at = ? AND rowid > ?)) AND record_type <> ? ORDER BY recorded_at, rowid", (asset_id, since, since, since_rowid, RECORD_TYPE)):
        if r["record_type"] in NEW_RECORD_TRIGGERS:
            out.append({"kind": "record", "record_type": r["record_type"], "record_id": r["record_id"], "trigger": NEW_RECORD_TRIGGERS[r["record_type"]], "recorded_at": r["recorded_at"]})
        elif r["record_type"] == "field_observation" and r["cs"] == "change_observed":
            out.append({"kind": "observation", "record_type": r["record_type"], "record_id": r["record_id"], "trigger": "due_diligence", "recorded_at": r["recorded_at"]})
    for c in db.conn.execute("SELECT component_id, updated_at FROM asset_components WHERE asset_id = ? AND updated_at > ? ORDER BY updated_at", (asset_id, since)):
        out.append({"kind": "component", "component_id": c["component_id"], "trigger": "composition", "recorded_at": c["updated_at"]})
    return out


def last_decision(db: Db, asset_id: str) -> dict | None:
    r = db.conn.execute("SELECT * FROM readiness_decisions WHERE asset_id = ? ORDER BY recorded_at DESC, rowid DESC LIMIT 1", (asset_id,)).fetchone()
    return dict(r) if r else None


def evaluate(db: Db, asset_id: str, *, today: str | None = None) -> dict:
    asset = db.conn.execute("SELECT asset_id, label, tracking_status FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise ValidationError([f"물건 {asset_id} 이 정본에 없다"])
    today = today or date.today().isoformat()
    if parse_date(today) is None:
        raise ValidationError([f"기준일이 달력상 존재하지 않는다: {today}"])
    dec = last_decision(db, asset_id)
    out = {"asset_id": asset_id, "label": asset["label"], "today": today, "tracking_status": asset["tracking_status"], "case": None, "checks": [], "unknown": [], "signals": [], "ref_changes": [],
           "ready": False, "blockers": [], "release_required": False, "last_decision": dec, "decisions": db.conn.execute("SELECT COUNT(*) FROM readiness_decisions WHERE asset_id = ?", (asset_id,)).fetchone()[0]}
    case = current_case(db, asset_id)
    if case is None:
        out["blockers"].append("매입 준비 검토 기록이 없다 (`j5 db case-add`)")
        out["release_required"] = asset["tracking_status"] == "purchase_ready"
        return out
    p = case["payload"]
    out["case"] = {k: case[k] for k in ("record_id", "observed_at", "recorded_at", "supersedes_id")}
    out["case"].update({"strategy": p["strategy"], "ownership_kind": p["scope"]["ownership_kind"], "gross_contract_krw": p["price"]["gross_contract_krw"], "deposit_total_krw": p["price"]["deposit_total_krw"],
                        "checklist_version": p["checklist_version"], "references": p["references"]})
    unknown, ref_changes = _unknowns_and_ref_changes(db, p)
    signals = _signals_since(db, asset_id, case["recorded_at"], case["rowid"])
    # 참조 기록 수정도 변경 신호다
    for rc in ref_changes:
        for t in rc["triggers"]:
            signals.append({"kind": "reference", "reference": rc["reference"], "record_id": rc["record_id"], "superseded_by": rc["superseded_by"], "trigger": t, "recorded_at": rc["recorded_at"]})
    triggered = {s["trigger"] for s in signals}
    for c in p["checks"]:
        hit = sorted(set(c["recheck_triggers"]) & triggered)
        if hit:
            verdict = "recheck"
        elif c["status"] == "verified":
            verdict = "expired" if (c["valid_until"] is not None and c["valid_until"] < today) else "ok"
        elif c["status"] == "not_applicable":
            verdict = "not_applicable"
        elif c["status"] in ("conditional", "blocked"):
            verdict = c["status"]
        else:
            verdict = "incomplete"
        out["checks"].append({"key": c["key"], "label": CHECK_LABEL[c["key"]], "status": c["status"], "verdict": verdict, "checked_at": c["checked_at"], "valid_until": c["valid_until"],
                              "reviewer_role": c["reviewer_role"], "evidence_count": len(c["evidence_ids"]), "open_issues": c["open_issues"], "triggered_by": hit, "not_applicable_reason": c["not_applicable_reason"]})
    out["unknown"], out["signals"], out["ref_changes"] = unknown, signals, ref_changes
    for c in out["checks"]:
        if c["verdict"] not in ("ok", "not_applicable"):
            out["blockers"].append(f"{c['label']}: {VERDICT_LABEL[c['verdict']]}" + (f" ({', '.join(TRIGGER_LABEL[t] for t in c['triggered_by'])} 변경)" if c["triggered_by"] else "")
                                   + (f" · 미해결 {len(c['open_issues'])}건" if c["open_issues"] else ""))
    for u in unknown:
        out["blockers"].append(f"중요 미확인: {u}")
    out["ready"] = not out["blockers"]
    if asset["tracking_status"] == "purchase_ready":
        # 승인과 같은 초에 들어온 변경도 승인 뒤로 본다(안전한 쪽). 승인 전에 들어온 변경은 승인 자체를 막았을 것이다
        after_approval = [s for s in signals if dec and dec["decision"] == "approve" and s["recorded_at"] and s["recorded_at"] >= dec["recorded_at"]]
        out["release_required"] = (not out["ready"]) or bool(after_approval) or (dec is not None and dec["decision"] == "approve" and dec["record_id"] != case["record_id"])
    return out


def readiness_text(e: dict) -> str:
    lines = [f"매입 준비 {e['label']} ({e['asset_id']}) · 기준일 {e['today']} · 관심 단계 {e['tracking_status']} · 결정 이력 {e['decisions']}건"]
    if e["case"] is None:
        lines.append("검토 기록 없음")
    else:
        c = e["case"]
        fmt = lambda v: f"{v:,}원" if v is not None else "미확인"  # noqa: E731
        lines.append(f"검토 기록 {c['record_id'][:8]} · 검토일 {c['observed_at']} · 전략 {c['strategy']} · 범위 {c['ownership_kind']} · 계약 총액 {fmt(c['gross_contract_krw'])} · 보증금 총액 {fmt(c['deposit_total_krw'])} · 체크리스트 {c['checklist_version']}")
        for ch in e["checks"]:
            extra = []
            if ch["checked_at"]:
                extra.append(f"검토 {ch['checked_at']}")
            if ch["valid_until"]:
                extra.append(f"유효 {ch['valid_until']}")
            extra.append(f"검토자 {ch['reviewer_role']}")
            extra.append(f"근거 {ch['evidence_count']}건")
            if ch["triggered_by"]:
                extra.append("변경: " + ", ".join(TRIGGER_LABEL[t] for t in ch["triggered_by"]))
            if ch["open_issues"]:
                extra.append(f"미해결 {len(ch['open_issues'])}건")
            if ch["not_applicable_reason"]:
                extra.append(f"사유 {ch['not_applicable_reason']}")
            lines.append(f"  [{VERDICT_LABEL[ch['verdict']]}] {ch['label']} ({STATUS_LABEL[ch['status']]}) · " + " · ".join(extra))
        for u in e["unknown"]:
            lines.append(f"  중요 미확인: {u}")
        if e["signals"]:
            lines.append(f"  검토 뒤 변경 {len(e['signals'])}건: " + ", ".join(f"{TRIGGER_LABEL[s['trigger']]}({s['kind']})" for s in e["signals"]))
    if e["last_decision"]:
        d = e["last_decision"]
        lines.append(f"마지막 결정: {d['decision']} {d['decided_on']} ({d['previous_status']} → {d['new_status']})" + (f" · {d['reason']}" if d["reason"] else ""))
    if e["ready"]:
        lines.append("판정: 준비 완료 조건 충족. `j5 db readiness-approve --asset ... --on <날짜>` 로 본인이 승인하면 purchase_ready 가 된다 (계약 협상 준비 상태이며 대출 확약·적법성 보증이 아니다)")
    else:
        lines.append(f"판정: 준비 아님 ({len(e['blockers'])}건)")
        for b in e["blockers"]:
            lines.append(f"  - {b}")
    if e["release_required"]:
        lines.append("주의: purchase_ready 인데 준비 조건이 깨졌거나 승인 뒤 변경이 있다. `j5 db readiness-withdraw --asset ... --on <날짜> --reason ...` 로 준비 상태를 철회하고 재검토한다")
    return "\n".join(lines) + "\n"


# ---- 결정 ----

def _record_decision(db: Db, asset_id: str, record_id: str, decision: str, decided_on: str, prev: str, new: str, reason: str | None, evaluation: dict) -> str:
    did = str(uuid.uuid4())
    now = db.now()
    db.conn.execute("INSERT INTO readiness_decisions (decision_id, asset_id, record_id, decision, decided_on, previous_status, new_status, reason, evaluation_json, recorded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (did, asset_id, record_id, decision, decided_on, prev, new, reason, json.dumps(evaluation, ensure_ascii=False, sort_keys=True), now))
    db.conn.execute("UPDATE assets SET tracking_status = ?, updated_at = ? WHERE asset_id = ?", (new, now, asset_id))
    return did


def approve(db: Db, asset_id: str, decided_on: str, *, note: str | None = None, today: str | None = None) -> dict:
    if parse_date(decided_on) is None:
        raise ValidationError([f"승인일이 달력상 존재하지 않는다: {decided_on}"])
    with db.transaction():
        e = evaluate(db, asset_id, today=today or decided_on)
        if not e["ready"]:
            raise ValidationError(["준비 조건을 충족하지 않아 승인하지 않는다"] + e["blockers"])
        if e["tracking_status"] == "purchase_ready":
            raise ValidationError(["이미 purchase_ready 다. 재승인하려면 먼저 철회한다"])
        if e["tracking_status"] in ("excluded", "archived"):
            raise ValidationError([f"관심 단계 {e['tracking_status']} 인 물건은 승인하지 않는다"])
        snapshot = {k: e[k] for k in ("today", "checks", "unknown", "signals", "ref_changes", "ready", "blockers")}
        did = _record_decision(db, asset_id, e["case"]["record_id"], "approve", decided_on, e["tracking_status"], "purchase_ready", note, snapshot)
        version = db.bump_dataset_version()
    return {"decision_id": did, "asset_id": asset_id, "decision": "approve", "decided_on": decided_on, "previous_status": e["tracking_status"], "new_status": "purchase_ready", "dataset_version": version,
            "record_id": e["case"]["record_id"]}


def withdraw(db: Db, asset_id: str, decided_on: str, *, reason: str, today: str | None = None) -> dict:
    if parse_date(decided_on) is None:
        raise ValidationError([f"철회일이 달력상 존재하지 않는다: {decided_on}"])
    if not reason or not reason.strip():
        raise ValidationError(["철회 사유가 필요하다"])
    with db.transaction():
        e = evaluate(db, asset_id, today=today or decided_on)
        if e["tracking_status"] != "purchase_ready":
            raise ValidationError([f"purchase_ready 가 아니라 철회할 것이 없다 (현재 {e['tracking_status']})"])
        snapshot = {k: e[k] for k in ("today", "checks", "unknown", "signals", "ref_changes", "ready", "blockers", "release_required")}
        rid = e["case"]["record_id"] if e["case"] else (e["last_decision"] or {}).get("record_id")
        if rid is None:
            raise ValidationError(["철회할 결정의 검토 기록을 찾을 수 없다"])
        did = _record_decision(db, asset_id, rid, "withdraw", decided_on, "purchase_ready", WITHDRAWN_STATUS, reason.strip(), snapshot)
        version = db.bump_dataset_version()
    return {"decision_id": did, "asset_id": asset_id, "decision": "withdraw", "decided_on": decided_on, "previous_status": "purchase_ready", "new_status": WITHDRAWN_STATUS, "dataset_version": version,
            "record_id": rid, "reason": reason.strip()}


def decision_text(r: dict) -> str:
    if r["decision"] == "approve":
        return (f"승인: 물건 {r['asset_id']} 관심 단계 {r['previous_status']} → purchase_ready ({r['decided_on']}) · 결정 {r['decision_id']} · dataset_version {r['dataset_version']}\n"
                "이 상태는 계약 협상 준비 상태다. 대출 확약·허가·계약 적법성 보증이 아니며 계약 직전·잔금 직전에 권리·규제·임대차를 다시 점검한다.\n")
    return (f"철회: 물건 {r['asset_id']} 관심 단계 purchase_ready → {r['new_status']} ({r['decided_on']}) · 사유 {r['reason']} · 결정 {r['decision_id']} · dataset_version {r['dataset_version']}\n"
            "승인 이력은 readiness_decisions 에 그대로 남는다. 재검토 뒤 새 검토 기록과 승인으로 다시 전환한다.\n")
