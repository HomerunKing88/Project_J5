"""핵심·비교 범위 분류와 변경 점검 (J5-014B-3). 데이터 사전 §7.1, ADR-06.

- 범위 규칙(zone_rules): 법정동 이름 목록으로 핵심(core)·비교(comparison)를 정한 버전. `apply_rules` 는 새 버전을 넣고 정본의 모든 거래를 다시 분류한다.
  규칙 밖 법정동은 outside, 규칙이 없으면 unclassified. 마스킹 지번 때문에 법정동보다 작은 경계는 두지 않는다("미확인 주소를 임의 경계 안에 넣지 않는다").
- 변경 점검(`changes_since`): 어떤 실행 이후 새로 보인 거래, 취소로 바뀐 거래, 응답에서 사라진 거래를 센다. 취소·정정 점검(월 1회 최근 6개월 `rt-recheck`)의 결과를 읽는 용도다.
- 파생본용 거래 목록(`transactions_for_projection`): 핵심·비교 범위의 취소 확정이 아닌 거래. 범위 밖 행은 파생본에 넣지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from j5.db.store import Db
from j5.db.validate import ValidationError
from j5.schemas_loader import schema_errors

RULES_SCHEMA = "zone_rules.schema.json"
TRANSACTIONS_FILE_VERSION = "1.0.0"


@dataclass
class ZoneApplyResult:
    outcome: str = "applied"
    version: int = 0
    name: str = ""
    core: int = 0
    comparison: int = 0
    outside: int = 0
    dataset_version: int = 0
    counts_by_zone: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "version": self.version, "name": self.name, "core_emd": self.core, "comparison_emd": self.comparison,
                "dataset_version": self.dataset_version, "counts_by_zone": self.counts_by_zone}

    def to_text(self) -> str:
        z = self.counts_by_zone
        return (f"범위 규칙 v{self.version} '{self.name}' 반영: 핵심 법정동 {self.core}개, 비교 법정동 {self.comparison}개 · 거래 분류 핵심 {z.get('core', 0)}, 비교 {z.get('comparison', 0)},"
                f" 범위 밖 {z.get('outside', 0)} · dataset_version {self.dataset_version}\n")


def load_rules(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValidationError([f"규칙 파일을 읽을 수 없다: {e}"]) from e
    errs = schema_errors(RULES_SCHEMA, doc)
    if errs:
        raise ValidationError([f"입력이 {RULES_SCHEMA} 에 맞지 않는다"] + errs[:10])
    core = [c.strip() for c in doc["core"]]
    comp = [c.strip() for c in doc["comparison"]]
    if any(not c for c in core + comp):
        raise ValidationError(["법정동 이름이 비어 있다"])
    overlap = sorted(set(core) & set(comp))
    if overlap:
        raise ValidationError([f"같은 법정동이 핵심과 비교 둘 다에 있다: {overlap}"])
    if len(set(core)) != len(core) or len(set(comp)) != len(comp):
        raise ValidationError(["같은 법정동이 한 목록에 두 번 있다 (앞뒤 공백 차이 포함)"])
    return {"kind": "zone_rules", "name": doc["name"].strip(), "core": core, "comparison": comp, "note": doc.get("note")}


def current_rules(db: Db) -> dict | None:
    row = db.conn.execute("SELECT version, name, rules_json, note, recorded_at FROM zone_rules ORDER BY version DESC LIMIT 1").fetchone()
    if row is None:
        return None
    rules = json.loads(row["rules_json"])
    return {"version": row["version"], "name": row["name"], "core": rules["core"], "comparison": rules["comparison"], "note": row["note"], "recorded_at": row["recorded_at"]}


def classify(rules: dict | None, emd_name: str | None) -> str:
    if rules is None:
        return "unclassified"
    if emd_name in rules["core"]:
        return "core"
    if emd_name in rules["comparison"]:
        return "comparison"
    return "outside"


def apply_rules(db: Db, rules: dict) -> ZoneApplyResult:
    """새 규칙 버전을 넣고 모든 거래를 다시 분류한다. 한 트랜잭션. 같은 내용을 다시 넣으면 변화 없음."""
    r = ZoneApplyResult(name=rules["name"], core=len(rules["core"]), comparison=len(rules["comparison"]))
    now = db.now()
    with db.transaction():
        cur = current_rules(db)
        if cur is not None and cur["core"] == rules["core"] and cur["comparison"] == rules["comparison"] and cur["name"] == rules["name"]:
            r.outcome = "unchanged"
            r.version = cur["version"]
            r.dataset_version = int(db.meta("dataset_version") or 0)
            r.counts_by_zone = zone_counts(db)
            return r
        version = (cur["version"] + 1) if cur else 1
        db.conn.execute("INSERT INTO zone_rules (version, name, rules_json, note, recorded_at) VALUES (?, ?, ?, ?, ?)",
                        (version, rules["name"], json.dumps({"core": rules["core"], "comparison": rules["comparison"]}, ensure_ascii=False, sort_keys=True), rules.get("note"), now))
        for row in db.conn.execute("SELECT transaction_id, emd_name FROM transactions").fetchall():
            db.conn.execute("UPDATE transactions SET zone = ?, zone_rule_version = ?, updated_at = ? WHERE transaction_id = ?",
                            (classify(rules, row["emd_name"]), version, now, row["transaction_id"]))
        r.version = version
        r.dataset_version = db.bump_dataset_version()
        r.counts_by_zone = zone_counts(db)
    return r


def zone_counts(db: Db) -> dict:
    return {row[0]: row[1] for row in db.conn.execute("SELECT zone, COUNT(*) FROM transactions WHERE cancel_status <> 'cancelled' GROUP BY zone")}


def rules_text(rules: dict | None, counts: dict) -> str:
    if rules is None:
        return "범위 규칙 없음: 모든 거래가 unclassified 다. `j5 db rt-zones apply <규칙.json>` 으로 정한다 (schemas/zone_rules.schema.json)\n"
    return (f"범위 규칙 v{rules['version']} '{rules['name']}' ({rules['recorded_at']})\n  핵심: {', '.join(rules['core'])}\n  비교: {', '.join(rules['comparison']) or '(없음)'}\n"
            f"  거래(취소 제외): 핵심 {counts.get('core', 0)}, 비교 {counts.get('comparison', 0)}, 범위 밖 {counts.get('outside', 0)}, 미분류 {counts.get('unclassified', 0)}\n")


# ---------------------------------------------------------------- 변경 점검

def changes_since(db: Db, lawd_cd: str, since_run_id: str, *, zones: tuple[str, ...] | None = None) -> dict:
    """since_run_id 뒤의 실행에서 처음 보인 거래, 취소로 바뀐 거래(최신 관측이 뒤 실행), 사라진 거래를 센다. 취소 확정은 제공자 표시(cdealType)만 근거다."""
    zsql, zargs = "", []
    if zones:
        zsql = " AND zone IN (%s)" % ",".join("?" * len(zones))
        zargs = list(zones)
    new_rows = db.conn.execute("SELECT transaction_id, deal_ymd, emd_name, jibun_raw, amount_krw, zone FROM transactions WHERE lawd_cd = ? AND first_seen_run_id > ?" + zsql + " ORDER BY deal_ymd, emd_name",
                               [lawd_cd, since_run_id, *zargs]).fetchall()
    cancelled = db.conn.execute(
        "SELECT t.transaction_id, t.deal_ymd, t.emd_name, t.jibun_raw, t.amount_krw, t.zone, t.cancel_date FROM transactions t"
        " JOIN transaction_observations o ON o.observation_id = t.latest_observation_id"
        " WHERE t.lawd_cd = ? AND t.cancel_status = 'cancelled' AND o.run_id > ? AND t.first_seen_run_id <= ?" + zsql.replace("zone", "t.zone") + " ORDER BY t.deal_ymd, t.emd_name",
        [lawd_cd, since_run_id, since_run_id, *zargs]).fetchall()
    missing = db.conn.execute("SELECT transaction_id, deal_ymd, emd_name, jibun_raw, amount_krw, zone, missing_since_run_id FROM transactions WHERE lawd_cd = ? AND missing_since_run_id > ?" + zsql + " ORDER BY deal_ymd, emd_name",
                              [lawd_cd, since_run_id, *zargs]).fetchall()
    runs = db.conn.execute("SELECT DISTINCT run_id FROM collection_runs WHERE lawd_cd = ? AND run_id > ? ORDER BY run_id", (lawd_cd, since_run_id)).fetchall()
    return {"lawd_cd": lawd_cd, "since_run_id": since_run_id, "runs_after": [r[0] for r in runs], "zones": list(zones) if zones else None,
            "new": [dict(r) for r in new_rows], "cancelled": [dict(r) for r in cancelled], "missing": [dict(r) for r in missing]}


def changes_text(c: dict) -> str:
    lines = [f"변경 점검 시군구 {c['lawd_cd']}: 실행 {c['since_run_id']} 이후 실행 {len(c['runs_after'])}회 · 새 거래 {len(c['new'])}, 취소로 바뀜 {len(c['cancelled'])}, 응답에서 사라짐 {len(c['missing'])}"
             + (f" (범위 {', '.join(c['zones'])})" if c["zones"] else "")]
    for label, key in (("새 거래", "new"), ("취소", "cancelled"), ("사라짐", "missing")):
        for t in c[key][:50]:
            amt = f"{t['amount_krw']:,}원" if t.get("amount_krw") is not None else "금액 미확인"
            extra = f" 취소일 {t['cancel_date']}" if key == "cancelled" and t.get("cancel_date") else (f" 실행 {t['missing_since_run_id']}" if key == "missing" else "")
            lines.append(f"  [{label}] {t['deal_ymd'][:4]}-{t['deal_ymd'][4:]} {t['emd_name'] or ''} {t['jibun_raw'] or ''} {amt} ({t['zone']}){extra} · {t['transaction_id']}")
        if len(c[key]) > 50:
            lines.append(f"  … {label} {len(c[key]) - 50}건 더 (--json)")
    lines.append("사라짐은 취소 확정이 아니다. 취소는 제공자의 cdealType 표시만 근거로 한다.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 파생본용

def transactions_for_projection(db: Db, *, generated_at: str, study_id: str, source_dataset_version: int, data_mode: str) -> dict | None:
    """핵심·비교 범위의 취소 확정 아닌 거래 목록. 범위 규칙이 없거나 해당 거래가 없으면 None (파일을 만들지 않는다)."""
    if not db._has_table("zone_rules") or current_rules(db) is None:
        return None
    rows = db.conn.execute(
        "SELECT t.*, l.asset_id AS linked_asset_id, l.status AS link_state, l.basis_kind FROM transactions t"
        " LEFT JOIN transaction_links l ON l.transaction_id = t.transaction_id AND l.status <> 'withdrawn'"
        " WHERE t.zone IN ('core', 'comparison') AND t.cancel_status <> 'cancelled' ORDER BY t.deal_ymd, t.emd_name, t.jibun_raw, t.ordinal").fetchall()
    items = []
    for t in rows:
        items.append({"transaction_id": t["transaction_id"], "zone": t["zone"], "lawd_cd": t["lawd_cd"], "deal_ymd": t["deal_ymd"], "deal_date": t["deal_date"],
                      "emd_name": t["emd_name"], "jibun": t["jibun_raw"], "jibun_masked": bool(t["jibun_masked"]), "building_kind": t["building_kind"],
                      "building_use": t["building_use_raw"], "land_use": t["land_use_raw"], "building_area_m2": t["building_area_m2"], "plottage_area_m2": t["plottage_area_m2"],
                      "build_year": t["build_year"], "amount_krw": t["amount_krw"], "missing_reasons": json.loads(t["missing_reasons_json"]), "share_deal": bool(t["share_deal"]),
                      "scope": t["scope"], "link_status": t["link_status"], "asset_id": t["linked_asset_id"] if t["link_state"] == "confirmed" else None,
                      "link_basis": t["basis_kind"] if t["link_state"] == "confirmed" else None, "missing_from_provider": t["missing_since_run_id"] is not None,
                      "first_seen_run_id": t["first_seen_run_id"], "last_seen_run_id": t["last_seen_run_id"]})
    if not items:
        return None
    return {"j5transactions": TRANSACTIONS_FILE_VERSION, "study_id": study_id, "data_mode": data_mode, "source_dataset_version": source_dataset_version,
            "generated_at": generated_at, "count": len(items), "note": "핵심·비교 범위의 거래(취소 확정 제외). asset_id 는 확정 연결만. 마스킹 지번은 제공자 표기 그대로", "transactions": items}
