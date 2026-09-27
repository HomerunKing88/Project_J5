"""관심 단계(관찰목록) 관리 (J5-029, ADR-22).

- `set_status`: 사람이 PC 에서 관심 단계를 바꾼다. 같은 트랜잭션에서 assets.tracking_status 갱신 + tracking_changes 불변 행 + dataset_version 증가.
  purchase_ready 로의 전환·철회는 준비 조건 판정과 결정 이력이 있는 readiness 명령(readiness-approve/withdraw)으로만 하며 여기서는 거절한다.
  hold·excluded·archived 로 옮길 때는 사유가 필요하다(왜 관찰에서 뺐는지 남긴다). 같은 단계면 변경 없음(이력·버전 그대로).
- `history`: 한 물건의 관심 단계 변경 이력(readiness 결정이 남긴 변경 포함). `overview`: 단계별 수와 관찰목록(watch·detailed_review·purchase_ready).
- 폰은 파생본 시드의 tracking_status 를 표시·필터만 하고 바꾸지 못한다(데이터 사전 §3.1 "관심도 갱신은 허용하지 않는다").
"""

from __future__ import annotations

import uuid
from datetime import date

from j5.db import schema as S
from j5.db.store import Db
from j5.db.validate import ValidationError, parse_date

STATUS_LABELS = {"unreviewed": "미검토", "background": "배경", "watch": "관찰", "detailed_review": "상세 검토", "purchase_ready": "매입 준비", "hold": "보류", "excluded": "제외", "archived": "보관"}
REASON_REQUIRED = ("hold", "excluded", "archived")
SOURCE_LABELS = {"asset_track": "asset-track", "readiness": "매입 준비 결정"}


def _asset(db: Db, asset_id: str) -> dict:
    row = db.conn.execute("SELECT asset_id, label, tracking_status, resolution_status FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if row is None:
        raise ValidationError([f"물건이 정본에 없다: {asset_id}"])
    return dict(row)


def set_status(db: Db, asset_id: str, status: str, *, reason: str | None = None, changed_on: str | None = None) -> dict:
    """관심 단계를 바꾼다. 돌려주는 dict 의 changed 가 False 면 같은 단계라 아무것도 쓰지 않았다."""
    if status not in S.TRACKING_STATUSES:
        raise ValidationError([f"관심 단계는 {'/'.join(S.TRACKING_STATUSES)} 중 하나다: {status}"])
    if status == "purchase_ready":
        raise ValidationError(["purchase_ready 전환은 준비 조건 판정 뒤 `db readiness-approve` 로만 한다"])
    changed_on = changed_on or date.today().isoformat()
    if parse_date(changed_on) is None:
        raise ValidationError([f"변경일이 달력상 존재하지 않는다: {changed_on}"])
    reason = reason.strip() if reason and reason.strip() else None
    if status in REASON_REQUIRED and reason is None:
        raise ValidationError([f"{status} 로 옮길 때는 --reason 이 필요하다"])
    with db.transaction():
        a = _asset(db, asset_id)
        prev = a["tracking_status"]
        if prev == "purchase_ready":
            raise ValidationError(["purchase_ready 인 물건은 `db readiness-withdraw` 로 철회한 뒤 단계를 바꾼다"])
        if prev == status:
            return {"asset_id": asset_id, "label": a["label"], "changed": False, "previous_status": prev, "new_status": status, "dataset_version": int(db.meta("dataset_version") or 0)}
        now = db.now()
        cid = str(uuid.uuid4())
        db.conn.execute("INSERT INTO tracking_changes (change_id, asset_id, previous_status, new_status, changed_on, reason, source, decision_id, recorded_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, 'asset_track', NULL, ?)", (cid, asset_id, prev, status, changed_on, reason, now))
        db.conn.execute("UPDATE assets SET tracking_status = ?, updated_at = ? WHERE asset_id = ?", (status, now, asset_id))
        version = db.bump_dataset_version()
    return {"asset_id": asset_id, "label": a["label"], "changed": True, "previous_status": prev, "new_status": status, "changed_on": changed_on, "reason": reason, "change_id": cid,
            "dataset_version": version, "resolution_status": a["resolution_status"]}


def history(db: Db, asset_id: str) -> dict:
    a = _asset(db, asset_id)
    # 정렬은 기록 시각 뒤 삽입 순서(rowid, 삭제 없는 불변 표라 단조 증가)다. change_id(UUID4) 는 무작위라 같은 초의 순서를 정하지 못한다 (리뷰 반영 PR #75)
    rows = [dict(r) for r in db.conn.execute("SELECT change_id, previous_status, new_status, changed_on, reason, source, decision_id, recorded_at FROM tracking_changes"
                                             " WHERE asset_id = ? ORDER BY recorded_at, rowid", (asset_id,))]
    return {**a, "changes": rows, "watchlist": a["tracking_status"] in S.WATCHLIST_STATUSES}


def overview(db: Db) -> dict:
    counts = {s: 0 for s in S.TRACKING_STATUSES}
    for r in db.conn.execute("SELECT tracking_status, COUNT(*) AS n FROM assets GROUP BY tracking_status"):
        counts[r["tracking_status"]] = r["n"]
    ph = ", ".join("?" for _ in S.WATCHLIST_STATUSES)
    items = [dict(r) for r in db.conn.execute(
        f"SELECT a.asset_id, a.label, a.tracking_status, a.resolution_status,"
        f" (SELECT changed_on FROM tracking_changes c WHERE c.asset_id = a.asset_id ORDER BY recorded_at DESC, rowid DESC LIMIT 1) AS last_changed_on"
        f" FROM assets a WHERE a.tracking_status IN ({ph}) ORDER BY a.tracking_status, a.label, a.asset_id", S.WATCHLIST_STATUSES)]
    return {"counts": counts, "total": sum(counts.values()), "watchlist": items, "watchlist_statuses": list(S.WATCHLIST_STATUSES), "pending": counts_pending(db)}


def counts_pending(db: Db) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM assets WHERE resolution_status = 'pending'").fetchone()[0]


def set_status_text(r: dict) -> str:
    if not r["changed"]:
        return f"변경 없음: {r['label']} ({r['asset_id']}) 는 이미 {STATUS_LABELS[r['new_status']]}({r['new_status']}) 다"
    tail = f" · 사유: {r['reason']}" if r["reason"] else ""
    pend = " · 확인 전(pending) 물건이다. 확정은 db asset-confirm" if r.get("resolution_status") == "pending" else ""
    return (f"관심 단계 변경: {r['label']} ({r['asset_id']}) {r['previous_status']} → {r['new_status']} ({STATUS_LABELS[r['new_status']]}) · 변경일 {r['changed_on']}{tail}"
            f" · dataset_version {r['dataset_version']}{pend}. 폰에는 project → project-copy 뒤 보인다")


def history_text(h: dict) -> str:
    lines = [f"관심 단계 {h['label']} ({h['asset_id']}): 현재 {STATUS_LABELS[h['tracking_status']]}({h['tracking_status']})"
             + (" · 관찰목록" if h["watchlist"] else "") + (" · 확인 전(pending)" if h["resolution_status"] == "pending" else "")]
    if not h["changes"]:
        lines.append("변경 이력 없음 (정본에 들어온 뒤 바뀐 적 없음)")
    for c in h["changes"]:
        lines.append(f"  {c['changed_on']} {c['previous_status']} → {c['new_status']} [{SOURCE_LABELS[c['source']]}]" + (f": {c['reason']}" if c["reason"] else ""))
    return "\n".join(lines)


def overview_text(o: dict) -> str:
    lines = ["관심 단계별 물건 수: " + " · ".join(f"{STATUS_LABELS[s]} {o['counts'][s]}" for s in S.TRACKING_STATUSES if o["counts"][s]) + f" (전체 {o['total']})"]
    if o["pending"]:
        lines.append(f"확인 전(pending) 물건 {o['pending']}개: db asset-confirm 으로 확정한다")
    lines.append(f"관찰목록 ({'·'.join(o['watchlist_statuses'])}) {len(o['watchlist'])}개" + (" — 운영 목표 30~50개를 넘는다" if len(o["watchlist"]) > 50 else ""))
    for it in o["watchlist"]:
        lines.append(f"  {STATUS_LABELS[it['tracking_status']]} · {it['label']} ({it['asset_id']})" + (f" · 마지막 변경 {it['last_changed_on']}" if it["last_changed_on"] else "")
                     + (" · 확인 전" if it["resolution_status"] == "pending" else ""))
    lines.append("관심 단계는 PC 에서만 바꾼다 (db asset-track). purchase_ready 는 readiness-approve/withdraw 로만.")
    return "\n".join(lines)
