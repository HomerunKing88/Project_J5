"""목표 매수가·투자판단 기록과 당시 기록 기준 조회 (J5-015A, R4). 데이터 사전 §8, §1 시간 규칙, ADR-07.

- `apply_record_input`: 입력 파일(`schemas/judgment_records.schema.json`)의 목표 매수가·투자판단을 불변 기록(records)으로 넣는다. 수정은 새 기록 + supersedes_id.
  판단일은 observed_at(날짜 정밀도), 정본 반영 시각은 저장소가 부여한다. 승인·철회도 새 기록(decision)이다.
- `asof`: 물건의 시점 T 상태를 재구성한다. 관측 기준(K 없음)은 현재 보유한 근거로, 당시 기록 기준(known_by=K)은 정본 기록일이 K 이하인 근거·검토결정만으로 만든다.
  기본 당시 보기는 K=T. 나중에 입력한 과거 거래, 나중에 철회한 연결, 나중의 정정 기록은 당시 보유정보에 넣지 않는다. 관측하지 않은 기간은 보간하지 않고 마지막 확인일을 보인다.
  당시 경계(도형 기준일)가 T 뒤면 "현재 경계 위의 과거 속성" 을 표시한다.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from j5.db.store import Db
from j5.db.validate import RECORD_PAYLOAD_SCHEMAS, ValidationError, parse_date
from j5.schemas_loader import schema_errors

INPUT_SCHEMA = "judgment_records.schema.json"
CANCEL_MARKS = ("O", "o", "Y", "y")


@dataclass
class RecordAddResult:
    record_id: str = ""
    record_type: str = ""
    asset_id: str = ""
    supersedes_id: str | None = None
    dataset_version: int = 0

    def to_dict(self) -> dict:
        return {"record_id": self.record_id, "record_type": self.record_type, "asset_id": self.asset_id, "supersedes_id": self.supersedes_id, "dataset_version": self.dataset_version}

    def to_text(self) -> str:
        sup = f" (이전 기록 {self.supersedes_id} 를 수정)" if self.supersedes_id else ""
        return f"기록 추가: {self.record_type} {self.record_id} · 물건 {self.asset_id}{sup} · dataset_version {self.dataset_version}\n"


def load_record_input(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValidationError([f"입력 파일을 읽을 수 없다: {e}"]) from e
    errs = schema_errors(INPUT_SCHEMA, doc)
    if errs:
        raise ValidationError([f"입력이 {INPUT_SCHEMA} 에 맞지 않는다"] + errs[:10])
    if parse_date(doc["observed_at"]) is None:
        raise ValidationError([f"판단일이 달력상 존재하지 않는다: {doc['observed_at']}"])
    p = doc["payload"]
    if doc["record_type"] == "target_price":
        if parse_date(p["decided_on"]) is None or parse_date(p["composition_as_of"]) is None:
            raise ValidationError(["decided_on·composition_as_of 는 달력상 존재하는 날짜"])
        if p["decided_on"] != doc["observed_at"]:
            raise ValidationError([f"payload.decided_on({p['decided_on']}) 과 observed_at({doc['observed_at']}) 은 같은 판단일이어야 한다"])
    return doc


def apply_record_input(db: Db, doc: dict) -> RecordAddResult:
    """한 트랜잭션으로 기록·근거를 넣고 dataset_version 을 올린다. 물건·이전 기록·근거 문서는 정본에 있어야 한다."""
    r = RecordAddResult(record_type=doc["record_type"], asset_id=doc["asset_id"], supersedes_id=doc.get("supersedes_id"))
    with db.transaction():
        if db.conn.execute("SELECT 1 FROM assets WHERE asset_id = ?", (doc["asset_id"],)).fetchone() is None:
            raise ValidationError([f"물건 {doc['asset_id']} 이 정본에 없다"])
        sup = doc.get("supersedes_id")
        if sup is not None:
            prev = db.conn.execute("SELECT subject_id, record_type FROM records WHERE record_id = ?", (sup,)).fetchone()
            if prev is None:
                raise ValidationError([f"이전 기록 {sup} 이 정본에 없다"])
            if prev["subject_id"] != doc["asset_id"] or prev["record_type"] != doc["record_type"]:
                raise ValidationError([f"이전 기록 {sup} 은 같은 물건·같은 종류의 기록이어야 한다 ({prev['subject_id']}, {prev['record_type']})"])
            if db.conn.execute("SELECT 1 FROM records WHERE supersedes_id = ?", (sup,)).fetchone() is not None:
                raise ValidationError([f"이전 기록 {sup} 은 이미 다른 기록으로 수정됐다. 최신 기록을 가리킨다"])
            if doc["record_type"] == "target_price" and not doc["payload"].get("revision_reason"):
                raise ValidationError(["목표 매수가를 수정할 때는 payload.revision_reason 이 필요하다"])
        elif doc["record_type"] == "investment_judgment" and doc["payload"]["decision"] == "withdrawn":
            raise ValidationError(["승인 철회(decision=withdrawn)는 철회하는 이전 기록(supersedes_id)이 필요하다"])
        for ev in doc.get("evidence", []):
            if db.conn.execute("SELECT 1 FROM source_documents WHERE document_id = ?", (ev["document_id"],)).fetchone() is None:
                raise ValidationError([f"근거 문서 {ev['document_id']} 가 정본에 없다 (source_documents 에 먼저 둔다)"])
        rid = str(uuid.uuid4())
        rec = {"record_id": rid, "subject_id": doc["asset_id"], "subject_type": "asset", "record_type": doc["record_type"], "source_kind": doc["source_kind"],
               "schema_version": RECORD_PAYLOAD_SCHEMAS[doc["record_type"]][2], "payload": doc["payload"], "observed_at": doc["observed_at"],
               "observed_at_precision": "date", "supersedes_id": sup}
        evidence = [{"document_id": ev["document_id"], "field_path": ev.get("field_path", "$"), "locator": ev.get("locator"),
                     "verification_status": ev.get("verification_status", "unverified")} for ev in doc.get("evidence", [])]
        db.insert_record(rec, evidence=evidence)
        r.record_id = rid
        r.dataset_version = db.bump_dataset_version()
    return r


# ---------------------------------------------------------------- 당시 기록 기준 조회

def _day_end(d: str) -> str:
    return f"{d}T23:59:59Z"


def asof(db: Db, asset_id: str, at: str, *, known_by: str | None = None) -> dict:
    """물건의 시점 `at`(YYYY-MM-DD) 상태. known_by 가 None 이면 관측 기준(현재 보유 근거 전부), 있으면 그 날짜까지 정본에 기록된 근거만 쓴다."""
    if parse_date(at) is None or (known_by is not None and parse_date(known_by) is None):
        raise ValidationError(["at·known_by 는 달력상 존재하는 YYYY-MM-DD"])
    asset = db.conn.execute("SELECT asset_id, label FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise ValidationError([f"물건 {asset_id} 이 정본에 없다"])
    kts = _day_end(known_by) if known_by is not None else None
    notes: list[str] = []

    def known(sql_col: str) -> str:
        return f" AND {sql_col} <= '{kts}'" if kts else ""

    # 기록: K 이하로 기록된 것만 보고, 그중 K 이하로 기록된 정정에 대체된 기록은 뺀다
    rows = [dict(r) for r in db.conn.execute(f"SELECT * FROM records WHERE subject_id = ?{known('recorded_at')} ORDER BY observed_at, recorded_at", (asset_id,))]
    # 정정·수정 기록이 T 에 유효할 때(관측일·판단일 ≤ T)만 원 기록을 대체한다. T 뒤에 판단한 수정은 T 의 상태를 바꾸지 않는다
    superseded = {r["supersedes_id"] for r in rows if r["supersedes_id"] and r["observed_at"][:10] <= at}
    live = [r for r in rows if r["record_id"] not in superseded and r["observed_at"][:10] <= at]
    later_records = sum(1 for r in rows if r["observed_at"][:10] > at)

    def latest(rtype: str) -> dict | None:
        cands = [r for r in live if r["record_type"] == rtype]
        if not cands:
            return None
        r = max(cands, key=lambda x: (x["observed_at"][:10], x["recorded_at"]))
        d = {k: r[k] for k in ("record_id", "observed_at", "observed_at_precision", "recorded_at", "source_kind", "supersedes_id")}
        d["payload"] = json.loads(r["payload_json"])
        return d

    observation = latest("field_observation")
    target = latest("target_price")
    judgment = latest("investment_judgment")
    if observation is None:
        notes.append("시점 T 까지 현장 관측 기록이 없다")
    else:
        notes.append(f"마지막 확인일 {observation['observed_at'][:10]} (그 뒤 T 까지의 상태는 보간하지 않는다)")
    n_target_chain = sum(1 for r in rows if r["record_type"] == "target_price")
    # 물건 구성: T 에 유효하고 K 이하에 기록된 연결
    comps = []
    for c in db.conn.execute(
            f"SELECT c.component_id, c.effective_from, c.effective_to, c.basis, c.recorded_at, p.pnu, p.label, p.emd_name, p.geometry_version"
            f" FROM asset_components c JOIN parcels p ON p.parcel_id = c.component_subject_id"
            f" WHERE c.asset_id = ? AND c.component_type = 'parcel' AND c.effective_from <= ? AND (c.effective_to IS NULL OR c.effective_to > ?){known('c.recorded_at')} ORDER BY p.pnu",
            (asset_id, at, at)):
        d = dict(c)
        d["boundary_note"] = "현재 경계 위의 과거 속성 (당시 경계 없음)" if c["geometry_version"] > at else None
        comps.append(d)
    if any(c["boundary_note"] for c in comps):
        notes.append("필지 경계의 도형 기준일이 T 보다 뒤라 당시 경계가 아니라 현재 경계 위의 과거 속성이다")
    # 거래 연결: 검토 결정(K 이하로 기록된 것)으로 연결의 당시 상태를 재구성한다
    links = []
    if db._has_table("transaction_links"):
        state: dict[str, dict] = {}
        for d in db.conn.execute(f"SELECT d.link_id, d.decision, d.asset_id, d.scope, d.decided_on, d.recorded_at, l.transaction_id FROM review_decisions d"
                                 f" JOIN transaction_links l ON l.link_id = d.link_id WHERE 1=1{known('d.recorded_at')} ORDER BY d.recorded_at, d.rowid"):
            state[d["link_id"]] = dict(d)
        for lid, d in state.items():
            if d["decision"] != "confirmed" or d["asset_id"] != asset_id:
                continue
            t = db.conn.execute(f"SELECT * FROM transactions WHERE transaction_id = ?{known('recorded_at')}", (d["transaction_id"],)).fetchone()
            if t is None:
                continue
            deal = t["deal_date"] or f"{t['deal_ymd'][:4]}-{t['deal_ymd'][4:]}-01"
            if deal > at:
                continue
            cancelled_known = db.conn.execute(
                f"SELECT 1 FROM transaction_observations o WHERE o.lawd_cd = ? AND o.deal_ymd = ? AND o.identity_hash = ? AND o.ordinal = ?"
                f" AND json_extract(o.content_json, '$.cdealType') IN ('O', 'o', 'Y', 'y'){known('o.recorded_at')} LIMIT 1",
                (t["lawd_cd"], t["deal_ymd"], t["identity_hash"], t["ordinal"])).fetchone() is not None
            links.append({"transaction_id": t["transaction_id"], "link_id": lid, "deal_date": t["deal_date"], "deal_ymd": t["deal_ymd"], "amount_krw": t["amount_krw"],
                          "emd_name": t["emd_name"], "jibun": t["jibun_raw"], "scope": d["scope"], "decided_on": d["decided_on"], "link_recorded_at": d["recorded_at"],
                          "cancelled_known": cancelled_known})
    return {"asset_id": asset_id, "label": asset["label"], "at": at, "known_by": known_by, "basis": "당시 기록 기준 (정본 기록일 ≤ K)" if known_by else "관측 기준 (현재 보유 근거 전부)",
            "observation": observation, "target_price": target, "target_price_chain": n_target_chain, "judgment": judgment, "parcels": comps, "transactions": links,
            "records_after_at": later_records, "notes": notes}


def asof_text(a: dict) -> str:
    lines = [f"물건 {a['label']} ({a['asset_id']}) · 시점 T={a['at']} · {a['basis']}" + (f" K={a['known_by']}" if a["known_by"] else "")]
    o = a["observation"]
    lines.append("현장 관측: " + (f"{o['observed_at']} {o['payload'].get('change_status')} {o['payload'].get('note') or ''} (기록 {o['recorded_at']})" if o else "없음"))
    t = a["target_price"]
    if t:
        p = t["payload"]
        lines.append(f"목표 매수가: {p['price_kind']} {p['price_krw']:,}원 · 판단일 {p['decided_on']} · 전략 {p['strategy']} · 구성 기준일 {p['composition_as_of']} (기록 {t['recorded_at']}, 이력 {a['target_price_chain']}건)")
    else:
        lines.append("목표 매수가: 없음")
    j = a["judgment"]
    if j:
        p = j["payload"]
        filled = sum(1 for k in ("strategy", "alternatives", "current_price_gap", "price_gap_change_conditions", "demand", "usability_and_constraints", "financing_cash_flow",
                                 "counter_evidence", "judgment_change_conditions", "purchase_conditions_next_checks") if p.get(k))
        lines.append(f"투자판단: {p['status']} · 결정 {p['decision']} · 항목 {filled}/10 · 판단일 {j['observed_at']} (기록 {j['recorded_at']})")
    else:
        lines.append("투자판단: 없음")
    lines.append(f"구성 필지 (T 에 유효): {len(a['parcels'])}건" + ("".join(f"\n  {c['emd_name'] or ''} {c['label']} ({c['pnu']}) {c['effective_from']}~{c['effective_to'] or '진행 중'} {c['basis']}" + (f" · {c['boundary_note']}" if c['boundary_note'] else "") for c in a["parcels"])))
    tx_lines = []
    for l in a["transactions"]:
        amount = f"{l['amount_krw']:,}원" if l["amount_krw"] is not None else "금액 미확인"
        tx_lines.append(f"\n  {l['deal_date'] or l['deal_ymd']} {l['emd_name'] or ''} {l['jibun'] or ''} {amount} · {l['scope']} · 연결 확정 {l['decided_on']}" + (" · 취소 표시 있음" if l["cancelled_known"] else ""))
    lines.append(f"확정 연결 거래 (계약일 ≤ T): {len(a['transactions'])}건" + "".join(tx_lines))
    if a["records_after_at"]:
        lines.append(f"T 이후 기록 {a['records_after_at']}건은 이 보기에 넣지 않았다")
    for n in a["notes"]:
        lines.append(f"주의: {n}")
    return "\n".join(lines) + "\n"
