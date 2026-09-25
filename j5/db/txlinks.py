"""거래↔물건 연결: 후보 CSV, 수동 연결·철회 (J5-014B-2). 데이터 사전 §7.2, §2 review_decisions, §8, 릴리스 계획 §4·§7 (연결 검토 화면 대신 CSV·수동 확인).

흐름: `rt-candidates` 가 범위 안의 현재 거래(취소 확정 제외)를 CSV 로 내고, 물건에 연결된 필지의 법정동·지번과 거래의 법정동·지번을 대조해
후보 물건을 적는다(정본에 쓰지 않는다). 사용자가 CSV 의 결정 열(decision, asset_id, decision_scope, basis_kind, reviewed_on, note)을 채우면
`rt-link` 가 한 트랜잭션으로 반영한다. 거래 하나에 유효한 연결은 하나이며 물건을 바꾸면 이전 연결을 철회(대체)하고 새 연결을 만든다.
상태 변화는 review_decisions 에 불변 이력으로 남는다. 마스킹된 지번은 앞자리 범위로만 후보를 좁히고(jibun_prefix) 자동 확정하지 않는다.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from j5.db import schema as S
from j5.db.parcels import active_links
from j5.db.store import Db
from j5.db.validate import ValidationError, is_uuid, parse_date

INFO_COLUMNS = ("transaction_id", "deal_ymd", "deal_date", "emd_name", "jibun", "building_kind", "building_use", "land_use", "building_area_m2", "plottage_area_m2",
                "amount_krw", "share_deal", "scope", "link_status", "linked_asset_id", "candidate_asset_ids", "candidate_basis", "candidate_labels")
DECISION_COLUMNS = ("decision", "asset_id", "decision_scope", "basis_kind", "reviewed_on", "note")
CSV_COLUMNS = INFO_COLUMNS + DECISION_COLUMNS
MAX_CSV_BYTES = 20 * 1024 * 1024
MAX_CSV_ROWS = 50_000

_JIBUN_RE = re.compile(r"^(산)?\s*([0-9*]+)(?:-([0-9*]+))?$")


@dataclass
class LinkApplyResult:
    outcome: str = "unchanged"  # applied / unchanged
    rows: int = 0
    decided: int = 0
    inserted: int = 0
    updated: int = 0
    withdrawn: int = 0
    superseded: int = 0
    unchanged: int = 0
    dataset_version: int = 0
    link_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "rows": self.rows, "decided": self.decided, "inserted": self.inserted, "updated": self.updated, "withdrawn": self.withdrawn,
                "superseded": self.superseded, "unchanged": self.unchanged, "dataset_version": self.dataset_version, "link_ids": self.link_ids}

    def to_text(self) -> str:
        return (f"거래 연결 반영: {'반영됨' if self.outcome == 'applied' else '변화 없음'} · CSV 행 {self.rows}, 결정 있는 행 {self.decided}"
                f" · 신규 {self.inserted}, 갱신 {self.updated}, 철회 {self.withdrawn} (대체 {self.superseded}), 변화 없음 {self.unchanged} · dataset_version {self.dataset_version}\n")


# ---------------------------------------------------------------- 지번 대조

def parse_jibun(jibun: str | None) -> dict | None:
    """'160' → bon 160 bu 0, '160-3', '산12-1', '1**' → 마스킹(앞자리 '1', 자릿수 3), '*' → 마스킹 자릿수 0(대조 불가). 해석 불가는 None."""
    if not jibun:
        return None
    m = _JIBUN_RE.match(jibun.strip())
    if not m:
        return None
    mountain = m.group(1) == "산"
    bon, bu = m.group(2), m.group(3) or "0"
    masked = "*" in bon or "*" in bu
    if not masked:
        return {"mountain": mountain, "bon": int(bon), "bu": int(bu), "masked": False}
    bon_prefix = bon.split("*", 1)[0]
    bu_prefix = bu.split("*", 1)[0]
    return {"mountain": mountain, "masked": True, "bon_prefix": bon_prefix, "bon_digits": len(bon), "bu_prefix": bu_prefix if "*" in bu else bu,
            "bu_digits": len(bu) if "*" in bu else None}


def jibun_matches(tx: dict, parcel: dict) -> str | None:
    """거래 지번(parse_jibun)과 필지(mountain, bon, bu)의 대조 결과: 'jibun_exact' / 'jibun_prefix' / None."""
    if tx is None or bool(parcel["mountain"]) != tx["mountain"]:
        return None
    if not tx["masked"]:
        return "jibun_exact" if (parcel["bon"], parcel["bu"]) == (tx["bon"], tx["bu"]) else None
    if tx["bon_prefix"] == "":
        return None  # '*' 처럼 앞자리가 하나도 없으면 대조 정보가 없다
    bon_s = str(parcel["bon"])
    if len(bon_s) != tx["bon_digits"] or not bon_s.startswith(tx["bon_prefix"]):
        return None
    bu_s = str(parcel["bu"])
    if tx.get("bu_digits") is None:
        if bu_s != tx["bu_prefix"]:
            return None
    elif len(bu_s) != tx["bu_digits"] or not bu_s.startswith(tx["bu_prefix"]):
        return None
    return "jibun_prefix"


# ---------------------------------------------------------------- 후보 CSV

def linked_parcels(db: Db, on_date: str) -> list[dict]:
    """유효한 물건↔필지 연결의 필지 정보 (법정동·산·본번·부번·물건)."""
    out = []
    labels = {a["asset_id"]: a["label"] for a in db.list_assets()}
    for l in active_links(db, on_date):
        p = db.conn.execute("SELECT emd_name, mountain, bon, bu, label FROM parcels WHERE pnu = ?", (l["pnu"],)).fetchone()
        if p is None:
            continue
        out.append({"asset_id": l["asset_id"], "asset_label": labels.get(l["asset_id"], ""), "pnu": l["pnu"], "emd_name": p["emd_name"], "mountain": p["mountain"],
                    "bon": p["bon"], "bu": p["bu"], "parcel_label": p["label"]})
    return out


def candidates(db: Db, lawd_cd: str, months: list[str], *, emd_names: list[str] | None = None, unlinked_only: bool = False, on_date: str | None = None) -> list[dict]:
    """범위 안의 현재 거래(취소 확정 제외)마다 후보 물건을 붙인 행 목록 (CSV 열 순서). 정본에 쓰지 않는다."""
    on_date = on_date or db.now()[:10]
    parcels = linked_parcels(db, on_date)
    q = "SELECT * FROM transactions WHERE lawd_cd = ? AND deal_ymd IN (%s) AND cancel_status <> 'cancelled'" % ",".join("?" * len(months))
    args: list = [lawd_cd, *months]
    if emd_names:
        q += " AND emd_name IN (%s)" % ",".join("?" * len(emd_names))
        args += emd_names
    if unlinked_only:
        q += " AND link_status = 'unlinked'"
    q += " ORDER BY deal_ymd, emd_name, jibun_raw, ordinal"
    rows = []
    for t in db.conn.execute(q, args):
        tx = parse_jibun(t["jibun_raw"])
        hits: dict[str, tuple[str, str]] = {}
        for p in parcels:
            if p["emd_name"] != t["emd_name"]:
                continue
            m = jibun_matches(tx, p)
            if m is None:
                continue
            prev = hits.get(p["asset_id"])
            if prev is None or (prev[0] == "jibun_prefix" and m == "jibun_exact"):
                hits[p["asset_id"]] = (m, f"{p['asset_label']} ({p['emd_name']} {p['parcel_label']})")
        linked = db.conn.execute("SELECT asset_id FROM transaction_links WHERE transaction_id = ? AND status <> 'withdrawn'", (t["transaction_id"],)).fetchone()
        ordered = sorted(hits.items(), key=lambda kv: (kv[1][0] != "jibun_exact", kv[0]))
        rows.append({"transaction_id": t["transaction_id"], "deal_ymd": t["deal_ymd"], "deal_date": t["deal_date"] or "", "emd_name": t["emd_name"] or "",
                     "jibun": t["jibun_raw"] or "", "building_kind": t["building_kind"], "building_use": t["building_use_raw"] or "", "land_use": t["land_use_raw"] or "",
                     "building_area_m2": "" if t["building_area_m2"] is None else t["building_area_m2"], "plottage_area_m2": "" if t["plottage_area_m2"] is None else t["plottage_area_m2"],
                     "amount_krw": "" if t["amount_krw"] is None else t["amount_krw"], "share_deal": t["share_deal"], "scope": t["scope"], "link_status": t["link_status"],
                     "linked_asset_id": linked["asset_id"] if linked else "", "candidate_asset_ids": ";".join(a for a, _ in ordered),
                     "candidate_basis": ";".join(m for _, (m, _) in ordered), "candidate_labels": ";".join(lab for _, (_, lab) in ordered),
                     "decision": "", "asset_id": "", "decision_scope": "", "basis_kind": "", "reviewed_on": "", "note": ""})
    return rows


def candidates_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in CSV_COLUMNS})
    return buf.getvalue()


def candidates_text(rows: list[dict]) -> str:
    n_cand = sum(1 for r in rows if r["candidate_asset_ids"])
    n_exact = sum(1 for r in rows if "jibun_exact" in r["candidate_basis"])
    n_linked = sum(1 for r in rows if r["linked_asset_id"])
    return (f"거래 후보 {len(rows)}행 (취소 확정 제외) · 후보 물건 있는 거래 {n_cand} (지번 일치 {n_exact}, 나머지는 앞자리 범위) · 이미 연결된 거래 {n_linked}\n"
            "CSV 의 decision(candidate/pending_evidence/confirmed/withdrawn)·asset_id·decision_scope·basis_kind·reviewed_on·note 를 채운 뒤 `j5 db rt-link <csv>` 로 반영한다.\n"
            "마스킹 지번(앞자리 범위)은 근거 없이 confirmed 로 두지 않는다.\n")


# ---------------------------------------------------------------- 결정 CSV 반영

def read_decisions_csv(path: Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        raise ValidationError([f"CSV 파일이 없다: {p}"])
    if p.stat().st_size > MAX_CSV_BYTES:
        raise ValidationError([f"CSV 가 {p.stat().st_size} 바이트로 상한을 넘는다"])
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ValidationError([f"CSV 를 읽을 수 없다 (UTF-8): {e}"]) from e
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "transaction_id" not in reader.fieldnames or "decision" not in reader.fieldnames:
        raise ValidationError(["CSV 머리글에 transaction_id 와 decision 열이 있어야 한다 (rt-candidates 출력 형식)"])
    rows = []
    for i, row in enumerate(reader, start=2):
        if i - 1 > MAX_CSV_ROWS:
            raise ValidationError([f"CSV 행이 {MAX_CSV_ROWS} 을 넘는다"])
        if None in row:
            raise ValidationError([f"{i}행: 열 수가 머리글보다 많다"])
        rows.append({"_line": i, **{k: (v or "").strip() for k, v in row.items() if k is not None}})
    return rows


def apply_decisions(db: Db, rows: list[dict]) -> LinkApplyResult:
    """결정 열이 있는 행만 반영한다. 한 행이라도 오류면 전체 거절. 거래마다 유효한 연결은 하나."""
    r = LinkApplyResult(rows=len(rows))
    decided = [row for row in rows if row.get("decision")]
    r.decided = len(decided)
    errs: list[str] = []
    today = db.now()[:10]
    plan = []
    seen_tx: set[str] = set()
    for row in decided:
        ln = row["_line"]
        tid, decision = row.get("transaction_id", ""), row["decision"]
        if decision not in S.LINK_DECISIONS:
            errs.append(f"{ln}행: decision 은 {'/'.join(S.LINK_DECISIONS)} 중 하나 ({decision!r})")
            continue
        if not is_uuid(tid):
            errs.append(f"{ln}행: transaction_id 형식")
            continue
        if tid in seen_tx:
            errs.append(f"{ln}행: 같은 거래가 두 번 있다 ({tid})")
            continue
        seen_tx.add(tid)
        tx = db.conn.execute("SELECT transaction_id, scope, cancel_status FROM transactions WHERE transaction_id = ?", (tid,)).fetchone()
        if tx is None:
            errs.append(f"{ln}행: 거래 {tid} 가 정본에 없다")
            continue
        asset_id = row.get("asset_id", "")
        scope = row.get("decision_scope", "") or None
        basis_kind = row.get("basis_kind", "") or "manual"
        reviewed_on = row.get("reviewed_on", "") or today
        note = row.get("note", "") or None
        if scope is not None and scope not in S.TRANSACTION_SCOPES:
            errs.append(f"{ln}행: decision_scope 는 {'/'.join(S.TRANSACTION_SCOPES)} 중 하나 ({scope!r})")
        if basis_kind not in S.LINK_BASIS_KINDS:
            errs.append(f"{ln}행: basis_kind 는 {'/'.join(S.LINK_BASIS_KINDS)} 중 하나 ({basis_kind!r})")
        if parse_date(reviewed_on) is None:
            errs.append(f"{ln}행: reviewed_on 은 YYYY-MM-DD ({reviewed_on!r})")
        active = db.conn.execute("SELECT link_id, asset_id, status, scope, basis_kind, basis_note FROM transaction_links WHERE transaction_id = ? AND status <> 'withdrawn'", (tid,)).fetchone()
        if decision == "withdrawn":
            if active is None:
                errs.append(f"{ln}행: 철회할 유효한 연결이 없다")
            elif asset_id and asset_id != active["asset_id"]:
                errs.append(f"{ln}행: 철회 대상 물건({active['asset_id']})과 asset_id 가 다르다")
        else:
            if not asset_id:
                errs.append(f"{ln}행: {decision} 에는 asset_id 가 필요하다")
            elif db.conn.execute("SELECT 1 FROM assets WHERE asset_id = ?", (asset_id,)).fetchone() is None:
                errs.append(f"{ln}행: 물건 {asset_id} 이 정본에 없다")
            if decision == "confirmed" and basis_kind == "jibun_prefix":
                errs.append(f"{ln}행: 앞자리 범위(jibun_prefix)만으로 confirmed 로 두지 않는다. 근거를 확보해 manual/document 로 하거나 pending_evidence 로 둔다")
            if decision == "confirmed" and tx["cancel_status"] == "cancelled":
                errs.append(f"{ln}행: 취소 확정 거래는 연결을 확정하지 않는다")
        plan.append({"line": ln, "tid": tid, "decision": decision, "asset_id": asset_id, "scope": scope, "basis_kind": basis_kind, "reviewed_on": reviewed_on, "note": note,
                     "active": dict(active) if active else None, "tx_scope": tx["scope"]})
    if errs:
        raise ValidationError(errs[:20])
    now = db.now()
    with db.transaction():
        for p in plan:
            active = p["active"]
            scope = p["scope"] or (active["scope"] if active else p["tx_scope"])
            if p["decision"] == "withdrawn":
                _set_status(db, active["link_id"], "withdrawn", scope, p, now, withdrawn_on=p["reviewed_on"], reason=p["note"] or "철회")
                r.withdrawn += 1
                r.link_ids.append(active["link_id"])
                _sync_transaction(db, p["tid"], now, scope=None)
                continue
            if active is not None and active["asset_id"] == p["asset_id"]:
                same = active["status"] == p["decision"] and active["scope"] == scope and active["basis_kind"] == p["basis_kind"] and (active["basis_note"] or None) == p["note"]
                if same:
                    r.unchanged += 1
                    r.link_ids.append(active["link_id"])
                    continue
                _set_status(db, active["link_id"], p["decision"], scope, p, now)
                r.updated += 1
                r.link_ids.append(active["link_id"])
                _sync_transaction(db, p["tid"], now, scope=p["scope"])
                continue
            new_id = str(uuid.uuid4())
            if active is not None:
                # 거래당 유효 연결은 하나(부분 UNIQUE)이므로 먼저 철회하고 새 연결을 넣은 뒤 대체 관계를 적는다
                _set_status(db, active["link_id"], "withdrawn", active["scope"], p, now, withdrawn_on=p["reviewed_on"], reason=f"다른 물건으로 대체 ({p['asset_id']})")
                r.superseded += 1
            db.conn.execute(
                "INSERT INTO transaction_links (link_id, transaction_id, asset_id, scope, status, basis_kind, basis_note, evidence_document_id, reviewed_on, withdrawn_on,"
                " withdrawn_reason, superseded_by, recorded_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL, ?, ?)",
                (new_id, p["tid"], p["asset_id"], scope, p["decision"], p["basis_kind"], p["note"], p["reviewed_on"], now, now))
            if active is not None:
                db.conn.execute("UPDATE transaction_links SET superseded_by = ? WHERE link_id = ?", (new_id, active["link_id"]))
            _record_decision(db, new_id, p["decision"], scope, p["asset_id"], p["reviewed_on"], p["note"], now)
            r.inserted += 1
            r.link_ids.append(new_id)
            _sync_transaction(db, p["tid"], now, scope=p["scope"])
        if r.inserted or r.updated or r.withdrawn:
            r.outcome = "applied"
            r.dataset_version = db.bump_dataset_version()
        else:
            r.dataset_version = int(db.meta("dataset_version") or 0)
    return r


def _set_status(db: Db, link_id: str, status: str, scope: str, p: dict, now: str, *, withdrawn_on: str | None = None, reason: str | None = None) -> None:
    cur = db.conn.execute("SELECT asset_id, basis_kind FROM transaction_links WHERE link_id = ?", (link_id,)).fetchone()
    if status == "withdrawn":
        basis_kind, basis_note, rationale = cur["basis_kind"], None, reason
    else:
        basis_kind, basis_note, rationale = p["basis_kind"], p["note"], p["note"]
    db.conn.execute("UPDATE transaction_links SET status = ?, scope = ?, basis_kind = ?, basis_note = ?, reviewed_on = ?, withdrawn_on = ?, withdrawn_reason = ?, updated_at = ? WHERE link_id = ?",
                    (status, scope, basis_kind, basis_note, p["reviewed_on"], withdrawn_on, reason if status == "withdrawn" else None, now, link_id))
    _record_decision(db, link_id, status, scope, cur["asset_id"], p["reviewed_on"], rationale, now)


def _record_decision(db: Db, link_id: str, decision: str, scope: str, asset_id: str, decided_on: str, rationale: str | None, now: str) -> None:
    prev = db.conn.execute("SELECT decision_id FROM review_decisions WHERE link_id = ? ORDER BY recorded_at DESC, rowid DESC LIMIT 1", (link_id,)).fetchone()
    db.conn.execute("INSERT INTO review_decisions (decision_id, link_id, decision, scope, asset_id, decided_on, rationale, previous_decision_id, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), link_id, decision, scope, asset_id, decided_on, rationale, prev["decision_id"] if prev else None, now))


def _sync_transaction(db: Db, tid: str, now: str, *, scope: str | None) -> None:
    """transactions.link_status 를 유효한 연결의 상태(없으면 unlinked)로, 범위는 결정에 있을 때만 manual_review 로 바꾼다."""
    active = db.conn.execute("SELECT status FROM transaction_links WHERE transaction_id = ? AND status <> 'withdrawn'", (tid,)).fetchone()
    status = active["status"] if active else "unlinked"
    if scope:
        db.conn.execute("UPDATE transactions SET link_status = ?, scope = ?, scope_basis = 'manual_review', updated_at = ? WHERE transaction_id = ?", (status, scope, now, tid))
    else:
        db.conn.execute("UPDATE transactions SET link_status = ?, updated_at = ? WHERE transaction_id = ?", (status, now, tid))


def link_history(db: Db, transaction_id: str) -> list[dict]:
    rows = db.conn.execute(
        "SELECT d.decision_id, d.link_id, d.decision, d.scope, d.asset_id, d.decided_on, d.rationale, d.previous_decision_id, d.recorded_at"
        " FROM review_decisions d JOIN transaction_links l ON l.link_id = d.link_id WHERE l.transaction_id = ? ORDER BY d.recorded_at, d.rowid", (transaction_id,))
    return [dict(r) for r in rows]
