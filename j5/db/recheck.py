"""반대 증거·변경 조건 재확인 (J5-015C, R4). 데이터 사전 §8, 릴리스 계획 §8·§9.

목표 매수가의 유효 조건, 투자판단의 가격차 변화 조건·판단 변경 조건·반대 근거는 자유 서술이라 기계가 판정하지 않는다. 대신 판단 기록이 정본에 들어온 뒤(recorded_at 기준)
정본에 새로 들어온 정보(현장 관측, 거래 연결의 검토 결정, 연결 거래의 취소 표시, 비교 근거 거래의 변동, 범위 규칙 버전, 물건 구성 변경, 반박(disputed) 근거)를 "신호" 로 모아
"재확인 필요" 를 표시한다. 재확인은 불변 기록(judgment_rechecks)이며 그 시점에 본 신호 요약을 함께 남긴다. 그 뒤 새 신호가 생기면 다시 재확인 필요가 된다.
재확인은 판단을 바꾸지 않는다. 판단을 바꾸려면 새 기록(supersedes_id)을 넣는다(judgment.apply_record_input).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from j5.db import schema as S
from j5.db.judgment import CANCEL_MARKS
from j5.db.store import Db
from j5.db.validate import ValidationError, parse_date
from j5.schemas_loader import schema_errors

INPUT_SCHEMA = "judgment_recheck.schema.json"
JUDGMENT_TYPES = ("target_price", "investment_judgment")
STATUS_LABEL = {"no_signal": "새 신호 없음", "recheck_needed": "재확인 필요", "reconfirmed": "재확인됨", "revision_needed": "수정 필요"}

# 기록 종류별로 사람이 대조할 조건 항목 (payload 키 → 표시 이름)
CONDITION_FIELDS = {
    "target_price": (("valid_conditions", "유효 조건"), ("assumptions", "비용·규제 전제")),
    "investment_judgment": (("price_gap_change_conditions", "가격차 변화 조건"), ("judgment_change_conditions", "판단 변경 조건"),
                            ("counter_evidence", "반대 근거"), ("purchase_conditions_next_checks", "매입 조건·다음 확인사항")),
}


def _current_judgments(db: Db, asset_id: str) -> list[dict]:
    """수정되지 않은(supersedes 로 대체되지 않은) 목표 매수가·투자판단 기록. 판단일·기록 순."""
    rows = db.conn.execute(
        "SELECT r.record_id, r.record_type, r.observed_at, r.recorded_at, r.supersedes_id, r.payload_json FROM records r"
        " WHERE r.subject_id = ? AND r.record_type IN ('target_price', 'investment_judgment')"
        " AND NOT EXISTS (SELECT 1 FROM records n WHERE n.supersedes_id = r.record_id) ORDER BY r.record_type, r.observed_at, r.recorded_at", (asset_id,)).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in ("record_id", "record_type", "observed_at", "recorded_at", "supersedes_id")}
        d["payload"] = json.loads(r["payload_json"])
        out.append(d)
    return out


def signals_since(db: Db, asset_id: str, rec: dict) -> dict:
    """판단 기록 rec 가 정본에 들어온 뒤(recorded_at 기준) 들어온 정보. 각 항목에 recorded_at 을 남기고 latest_at 은 그 최댓값이다."""
    since = rec["recorded_at"]
    latest: list[str] = []

    def seen(ts: str | None) -> None:
        if ts:
            latest.append(ts)

    obs = []
    for r in db.conn.execute("SELECT record_id, observed_at, recorded_at, supersedes_id, json_extract(payload_json, '$.change_status') AS cs FROM records"
                             " WHERE subject_id = ? AND record_type = 'field_observation' AND recorded_at > ? ORDER BY recorded_at, rowid", (asset_id, since)):
        obs.append({"record_id": r["record_id"], "observed_at": r["observed_at"], "change_status": r["cs"], "corrects": r["supersedes_id"], "recorded_at": r["recorded_at"]})
        seen(r["recorded_at"])
    decisions, cancellations, zones, components, basis = [], [], [], [], []
    if db._has_table("transaction_links"):
        for d in db.conn.execute("SELECT d.decision, d.decided_on, d.recorded_at, d.rationale, l.transaction_id, t.deal_date, t.deal_ymd, t.amount_krw, t.emd_name, t.jibun_raw"
                                 " FROM review_decisions d JOIN transaction_links l ON l.link_id = d.link_id JOIN transactions t ON t.transaction_id = l.transaction_id"
                                 " WHERE d.asset_id = ? AND d.recorded_at > ? ORDER BY d.recorded_at, d.rowid", (asset_id, since)):
            decisions.append(dict(d))
            seen(d["recorded_at"])
        # 확정 연결된 거래에 판단 뒤 들어온 취소 표시
        for c in db.conn.execute("SELECT t.transaction_id, t.deal_date, t.deal_ymd, t.cancel_status, t.cancel_date, MIN(o.recorded_at) AS recorded_at"
                                 " FROM transaction_links l JOIN transactions t ON t.transaction_id = l.transaction_id"
                                 " JOIN transaction_observations o ON o.lawd_cd = t.lawd_cd AND o.deal_ymd = t.deal_ymd AND o.identity_hash = t.identity_hash AND o.ordinal = t.ordinal"
                                 " WHERE l.asset_id = ? AND l.status = 'confirmed' AND o.recorded_at > ?"
                                 f" AND json_extract(o.content_json, '$.cdealType') IN ({', '.join('?' * len(CANCEL_MARKS))}) GROUP BY t.transaction_id ORDER BY recorded_at",
                                 (asset_id, since, *CANCEL_MARKS)):
            cancellations.append(dict(c))
            seen(c["recorded_at"])
        # 목표 매수가의 비교 근거 거래: 취소·응답에서 사라짐·연결 철회
        if rec["record_type"] == "target_price":
            for b in rec["payload"].get("comparison_basis", []):
                if b.get("kind") != "transaction" or not b.get("ref_id"):
                    continue
                t = db.conn.execute("SELECT transaction_id, deal_date, deal_ymd, cancel_status, cancel_date, missing_since_run_id, updated_at FROM transactions WHERE transaction_id = ?", (b["ref_id"],)).fetchone()
                if t is None:
                    basis.append({"transaction_id": b["ref_id"], "note": b["note"], "problem": "정본에 없는 거래", "recorded_at": None})
                    continue
                withdrawn = db.conn.execute("SELECT withdrawn_on, updated_at FROM transaction_links WHERE transaction_id = ? AND asset_id = ? AND status = 'withdrawn' AND updated_at > ?"
                                            " ORDER BY updated_at DESC LIMIT 1", (b["ref_id"], asset_id, since)).fetchone()
                problems = []
                if t["cancel_status"] != "none":
                    problems.append(f"취소 ({t['cancel_date'] or '날짜 미확인'})")
                if t["missing_since_run_id"]:
                    problems.append(f"응답에서 사라짐 ({t['missing_since_run_id']})")
                if withdrawn:
                    problems.append(f"연결 철회 ({withdrawn['withdrawn_on']})")
                if problems and t["updated_at"] > since:
                    basis.append({"transaction_id": b["ref_id"], "note": b["note"], "problem": ", ".join(problems), "recorded_at": t["updated_at"]})
                    seen(t["updated_at"])
                elif problems:
                    basis.append({"transaction_id": b["ref_id"], "note": b["note"], "problem": ", ".join(problems) + " (판단 전부터)", "recorded_at": None})
    if db._has_table("zone_rules"):
        for z in db.conn.execute("SELECT version, name, recorded_at FROM zone_rules WHERE recorded_at > ? ORDER BY version", (since,)):
            zones.append(dict(z))
            seen(z["recorded_at"])
    for c in db.conn.execute("SELECT c.component_id, c.effective_from, c.effective_to, c.basis, c.recorded_at, c.updated_at, c.component_type, c.component_subject_id"
                             " FROM asset_components c WHERE c.asset_id = ? AND c.updated_at > ? ORDER BY c.updated_at", (asset_id, since)):
        d = dict(c)
        d["kind"] = "new" if c["recorded_at"] > since else "changed"
        components.append(d)
        seen(c["updated_at"])
    disputed = [dict(r) for r in db.conn.execute("SELECT field_path, document_id, locator, recorded_at FROM record_evidence WHERE record_id = ? AND verification_status = 'disputed'", (rec["record_id"],))]
    for r in disputed:
        seen(r["recorded_at"])
    counts = {"observations": len(obs), "observations_changed": sum(1 for o in obs if o["change_status"] == "change_observed"), "link_decisions": len(decisions),
              "cancellations": len(cancellations), "comparison_basis_problems": len(basis), "zone_rule_versions": len(zones), "component_changes": len(components), "disputed_evidence": len(disputed)}
    return {"since": since, "latest_at": max(latest) if latest else None, "counts": counts, "total": sum(counts.values()),
            "observations": obs, "link_decisions": decisions, "cancellations": cancellations, "comparison_basis": basis, "zone_rules": zones, "components": components, "disputed_evidence": disputed}


def _last_recheck(db: Db, record_id: str) -> dict | None:
    r = db.conn.execute("SELECT * FROM judgment_rechecks WHERE record_id = ? ORDER BY recorded_at DESC, rowid DESC LIMIT 1", (record_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    for k in ("conditions_checked_json", "counter_evidence_json", "signals_json"):
        d[k[:-5]] = json.loads(d.pop(k))
    return d


def _status(sig: dict, last: dict | None) -> tuple[str, str]:
    if last is None:
        return ("recheck_needed", "판단 뒤 새 정보가 들어왔고 재확인 기록이 없다") if sig["total"] else ("no_signal", "판단 뒤 새 정보가 없고 재확인 기록도 없다")
    if sig["latest_at"] and sig["latest_at"] > last["recorded_at"]:
        return "recheck_needed", f"마지막 재확인({last['reviewed_on']}) 뒤에 새 정보가 들어왔다"
    return last["outcome"], f"마지막 재확인 {last['reviewed_on']} ({STATUS_LABEL[last['outcome']]}) 뒤 새 정보 없음"


def recheck_status(db: Db, asset_id: str) -> dict:
    asset = db.conn.execute("SELECT asset_id, label FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise ValidationError([f"물건 {asset_id} 이 정본에 없다"])
    items = []
    for rec in _current_judgments(db, asset_id):
        sig = signals_since(db, asset_id, rec)
        last = _last_recheck(db, rec["record_id"])
        status, reason = _status(sig, last)
        conditions = []
        for key, name in CONDITION_FIELDS[rec["record_type"]]:
            v = rec["payload"].get(key)
            if isinstance(v, list):
                conditions.extend({"field": key, "name": name, "text": x} for x in v)
            elif v:
                conditions.append({"field": key, "name": name, "text": v})
        rechecks = db.conn.execute("SELECT COUNT(*) FROM judgment_rechecks WHERE record_id = ?", (rec["record_id"],)).fetchone()[0]
        items.append({"record_id": rec["record_id"], "record_type": rec["record_type"], "decided_on": rec["observed_at"][:10], "recorded_at": rec["recorded_at"],
                      "summary": _summary(rec), "conditions": conditions, "signals": sig, "last_recheck": last, "rechecks": rechecks, "status": status, "status_label": STATUS_LABEL[status], "reason": reason})
    return {"asset_id": asset_id, "label": asset["label"], "records": items,
            "note": "조건·반대 근거는 사람이 대조한다. 신호는 판단 기록이 정본에 들어온 뒤 들어온 정보이며 조건이 깨졌다는 판정이 아니다. 재확인은 판단을 바꾸지 않으며 바꾸려면 새 기록(supersedes_id)을 넣는다"}


def _summary(rec: dict) -> str:
    p = rec["payload"]
    if rec["record_type"] == "target_price":
        return f"{p['price_kind']} {p['price_krw']:,}원 · 전략 {p['strategy']} · 구성 기준일 {p['composition_as_of']}"
    return f"{p['status']} · 결정 {p['decision']} · 전략 {p.get('strategy') or '-'}"


def recheck_text(s: dict) -> str:
    lines = [f"재확인 현황 {s['label']} ({s['asset_id']}): 현재 판단 기록 {len(s['records'])}건"]
    for it in s["records"]:
        lines.append(f"[{it['status_label']}] {it['record_type']} {it['record_id'][:8]} · 판단일 {it['decided_on']} · {it['summary']} (기록 {it['recorded_at']}, 재확인 {it['rechecks']}회)")
        lines.append(f"  이유: {it['reason']}")
        for c in it["conditions"]:
            lines.append(f"  조건 [{c['name']}] {c['text']}")
        sig = it["signals"]
        if sig["total"]:
            c = sig["counts"]
            lines.append(f"  판단 뒤 새 정보 {sig['total']}건 (마지막 {sig['latest_at']}): 관측 {c['observations']}(변화 {c['observations_changed']}), 연결 결정 {c['link_decisions']}, 취소 표시 {c['cancellations']},"
                         f" 비교 근거 변동 {c['comparison_basis_problems']}, 범위 규칙 {c['zone_rule_versions']}, 구성 변경 {c['component_changes']}, 반박 근거 {c['disputed_evidence']}")
            for o in sig["observations"]:
                lines.append(f"    관측 {o['observed_at']} {o['change_status']}" + (" (정정)" if o["corrects"] else ""))
            for d in sig["link_decisions"]:
                lines.append(f"    연결 {d['decision']} {d['deal_date'] or d['deal_ymd']} {d['emd_name'] or ''} {d['jibun_raw'] or ''} · {d['decided_on']}")
            for x in sig["cancellations"]:
                lines.append(f"    취소 표시 {x['deal_date'] or x['deal_ymd']} ({x['cancel_status']}, {x['cancel_date'] or '날짜 미확인'})")
            for b in sig["comparison_basis"]:
                lines.append(f"    비교 근거 {b['transaction_id'][:8]} {b['problem']} · {b['note']}")
            for z in sig["zone_rules"]:
                lines.append(f"    범위 규칙 v{z['version']} {z['name']}")
            for cpt in sig["components"]:
                lines.append(f"    구성 {cpt['kind']} {cpt['component_type']} {cpt['component_subject_id'][:12]} {cpt['effective_from']}~{cpt['effective_to'] or '진행 중'}")
        else:
            lines.append("  판단 뒤 새 정보 없음")
        last = it["last_recheck"]
        if last:
            lines.append(f"  마지막 재확인 {last['reviewed_on']} {STATUS_LABEL[last['outcome']]}: 조건 대조 {len(last['conditions_checked'])}건"
                         f"(깨짐 {sum(1 for c in last['conditions_checked'] if c.get('holds') is False)}), 반대 증거 {len(last['counter_evidence'])}건" + (f" · {last['note']}" if last["note"] else ""))
            for ce in last["counter_evidence"]:
                lines.append(f"    반대 증거: {ce['note']}" + (f" (문서 {ce['document_id'][:8]})" if ce.get("document_id") else ""))
    lines.append(s["note"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 재확인 기록

def load_recheck_input(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValidationError([f"입력 파일을 읽을 수 없다: {e}"]) from e
    errs = schema_errors(INPUT_SCHEMA, doc)
    if errs:
        raise ValidationError([f"입력이 {INPUT_SCHEMA} 에 맞지 않는다"] + errs[:10])
    if parse_date(doc["reviewed_on"]) is None:
        raise ValidationError([f"재확인일이 달력상 존재하지 않는다: {doc['reviewed_on']}"])
    for ce in doc["counter_evidence"]:
        if ce.get("observed_on") is not None and parse_date(ce["observed_on"]) is None:
            raise ValidationError([f"반대 증거 확인일이 달력상 존재하지 않는다: {ce['observed_on']}"])
    return doc


def apply_recheck_input(db: Db, doc: dict) -> dict:
    """한 트랜잭션으로 재확인 기록을 넣고 dataset_version 을 올린다. 현재(수정되지 않은) 목표 매수가·투자판단 기록만 재확인한다. 신호 요약은 저장소가 그 시점에 계산해 남긴다."""
    with db.transaction():
        if db.conn.execute("SELECT 1 FROM assets WHERE asset_id = ?", (doc["asset_id"],)).fetchone() is None:
            raise ValidationError([f"물건 {doc['asset_id']} 이 정본에 없다"])
        r = db.conn.execute("SELECT record_id, subject_id, record_type, observed_at, recorded_at, supersedes_id, payload_json FROM records WHERE record_id = ?", (doc["record_id"],)).fetchone()
        if r is None:
            raise ValidationError([f"기록 {doc['record_id']} 이 정본에 없다"])
        if r["subject_id"] != doc["asset_id"] or r["record_type"] not in JUDGMENT_TYPES:
            raise ValidationError([f"기록 {doc['record_id']} 은 이 물건의 목표 매수가·투자판단 기록이어야 한다 ({r['subject_id']}, {r['record_type']})"])
        newer = db.conn.execute("SELECT record_id FROM records WHERE supersedes_id = ?", (doc["record_id"],)).fetchone()
        if newer is not None:
            raise ValidationError([f"기록 {doc['record_id']} 은 이미 {newer['record_id']} 로 수정됐다. 최신 기록을 재확인한다"])
        if doc["reviewed_on"] < r["observed_at"][:10]:
            raise ValidationError([f"재확인일({doc['reviewed_on']})이 판단일({r['observed_at'][:10]})보다 앞선다"])
        for ce in doc["counter_evidence"]:
            if ce.get("document_id") and db.conn.execute("SELECT 1 FROM source_documents WHERE document_id = ?", (ce["document_id"],)).fetchone() is None:
                raise ValidationError([f"반대 증거 문서 {ce['document_id']} 가 정본에 없다 (source_documents 에 먼저 둔다)"])
        rec = {"record_id": r["record_id"], "record_type": r["record_type"], "observed_at": r["observed_at"], "recorded_at": r["recorded_at"], "payload": json.loads(r["payload_json"])}
        sig = signals_since(db, doc["asset_id"], rec)
        snapshot = {"since": sig["since"], "latest_at": sig["latest_at"], "counts": sig["counts"], "total": sig["total"]}
        rid = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO judgment_rechecks (recheck_id, record_id, asset_id, outcome, reviewed_on, conditions_checked_json, counter_evidence_json, signals_json, note, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, doc["record_id"], doc["asset_id"], doc["outcome"], doc["reviewed_on"], json.dumps(doc["conditions_checked"], ensure_ascii=False),
             json.dumps(doc["counter_evidence"], ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False), doc.get("note"), db.now()))
        version = db.bump_dataset_version()
    return {"recheck_id": rid, "record_id": doc["record_id"], "record_type": r["record_type"], "asset_id": doc["asset_id"], "outcome": doc["outcome"], "reviewed_on": doc["reviewed_on"],
            "signals_seen": snapshot["total"], "conditions_checked": len(doc["conditions_checked"]), "counter_evidence": len(doc["counter_evidence"]), "dataset_version": version}


def recheck_add_text(r: dict) -> str:
    return (f"재확인 기록: {STATUS_LABEL[r['outcome']]} · {r['record_type']} {r['record_id']} · 물건 {r['asset_id']} · 재확인일 {r['reviewed_on']}"
            f" · 본 신호 {r['signals_seen']}건, 조건 대조 {r['conditions_checked']}건, 반대 증거 {r['counter_evidence']}건 · dataset_version {r['dataset_version']}\n"
            + ("수정 필요로 판정했다. 판단을 바꾸려면 새 기록(supersedes_id)을 넣는다\n" if r["outcome"] == "revision_needed" else ""))
