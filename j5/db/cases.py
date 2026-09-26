"""매입 검토 사건 비교자료 내보내기 (J5-018B, R7). 릴리스 계획 §9 "매입 검토 사건마다 구성 범위·가격·전략·금융·임대차·규제·실사·협상안을 묶어 표시한다", "비교자료 내보내기를 제공한다".

`case_bundle(db, asset_id, today)`: 물건 하나의 현재 매입 준비 검토 기록, 진입조건 판정(항목·미확인·변경·재확인), 현재 목표 매수가·투자판단, 규제 검토·개발안·자금안(저장 시점 계산 스냅샷),
결정 이력을 한 묶음으로 모은다. 값은 정본에 있는 그대로이며 계산을 다시 하지 않는다.
`export_cases(db, data_home, asset_ids=None, out_root=None, today=None)`: 대상 물건(생략 시 현재 검토 기록이 있는 물건 전부)의 묶음을 `exports/private/cases/<stamp>-<run8>/` 에
`cases.json`(전체)·`cases.csv`(물건당 한 행: 범위·가격·전략·금융·규제·판정 요약)·`README.txt` 로 쓴다. 새 폴더에 쓰며 덮어쓰지 않는다. 실데이터가 들어가므로 저장소·공개 배포에 넣지 않는다.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from pathlib import Path

from j5.db.plans import plan_overview, regulation_status
from j5.db.readiness import CHECK_LABEL, STAGE_LABEL, current_case, evaluate
from j5.db.store import Db
from j5.db.validate import ValidationError

EXPORT_DIR = Path("exports") / "private" / "cases"
CSV_COLUMNS = ("asset_id", "label", "tracking_status", "case_record_id", "case_reviewed_on", "strategy", "ownership_kind", "share_ratio", "parcel_count", "composition_as_of",
               "gross_contract_krw", "deposit_total_krw", "vat_status", "asking_confirmation", "listing_event", "target_price_krw", "target_price_kind", "target_decided_on",
               "judgment_status", "judgment_decision", "required_equity_krw", "max_required_equity_krw", "far_headroom_m2", "regulation_status", "checks_ok", "checks_total",
               "checks_blocking", "unknown_count", "signals_count", "ready", "release_required", "pre_contract_recheck", "pre_settlement_recheck", "last_decision", "last_decision_on",
               "decisions_count", "generated_at", "dataset_version")


def _latest(db: Db, asset_id: str, rtype: str) -> dict | None:
    r = db.conn.execute("SELECT record_id, observed_at, recorded_at, source_kind, payload_json FROM records r WHERE subject_id = ? AND record_type = ?"
                        " AND NOT EXISTS (SELECT 1 FROM records n WHERE n.supersedes_id = r.record_id) ORDER BY observed_at DESC, recorded_at DESC LIMIT 1", (asset_id, rtype)).fetchone()
    if r is None:
        return None
    return {"record_id": r["record_id"], "observed_at": r["observed_at"], "recorded_at": r["recorded_at"], "source_kind": r["source_kind"], "payload": json.loads(r["payload_json"])}


def _by_id(db: Db, rid: str | None) -> dict | None:
    if rid is None:
        return None
    r = db.conn.execute("SELECT record_id, record_type, observed_at, recorded_at, source_kind, payload_json FROM records WHERE record_id = ?", (rid,)).fetchone()
    if r is None:
        return None
    return {"record_id": r["record_id"], "record_type": r["record_type"], "observed_at": r["observed_at"], "recorded_at": r["recorded_at"], "source_kind": r["source_kind"], "payload": json.loads(r["payload_json"])}


def case_bundle(db: Db, asset_id: str, *, today: str | None = None) -> dict:
    asset = db.conn.execute("SELECT asset_id, label, tracking_status, address, lon, lat FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise ValidationError([f"물건 {asset_id} 이 정본에 없다"])
    e = evaluate(db, asset_id, today=today)
    case = current_case(db, asset_id)
    refs = case["payload"]["references"] if case else {}
    # 검토가 참조한 기록을 우선하고, 없으면 현재 기록
    target = _by_id(db, refs.get("target_price_id")) or _latest(db, asset_id, "target_price")
    judgment = _by_id(db, refs.get("investment_judgment_id")) or _latest(db, asset_id, "investment_judgment")
    financing = _by_id(db, refs.get("financing_plan_id")) or _latest(db, asset_id, "financing_plan")
    development = _by_id(db, refs.get("development_plan_id")) or _latest(db, asset_id, "development_plan")
    regulation = _by_id(db, refs.get("regulation_review_id")) or _latest(db, asset_id, "regulation_review")
    decisions = [dict(r) for r in db.conn.execute("SELECT decision_id, decision, decided_on, previous_status, new_status, reason, recorded_at FROM readiness_decisions WHERE asset_id = ? ORDER BY recorded_at, rowid", (asset_id,))]
    return {"asset": {k: asset[k] for k in ("asset_id", "label", "tracking_status", "address", "lon", "lat")}, "case": case, "evaluation": e,
            "target_price": target, "investment_judgment": judgment, "financing_plan": financing, "development_plan": development, "regulation_review": regulation,
            "plans": plan_overview(db, asset_id), "decisions": decisions}


def _csv_row(b: dict, generated_at: str, dataset_version: int) -> dict:
    a, e, c = b["asset"], b["evaluation"], b["case"]
    p = c["payload"] if c else None
    fin, dev, reg, tp, jd = b["financing_plan"], b["development_plan"], b["regulation_review"], b["target_price"], b["investment_judgment"]
    fr = (dev or {}).get("payload", {}).get("far_result") if dev else None
    checks = e["checks"]
    rechecks = {st["stage"]: st for st in e.get("stage_rechecks", [])}
    last = e["last_decision"]
    row = {
        "asset_id": a["asset_id"], "label": a["label"], "tracking_status": a["tracking_status"],
        "case_record_id": c["record_id"] if c else None, "case_reviewed_on": c["observed_at"] if c else None,
        "strategy": p["strategy"] if p else None, "ownership_kind": p["scope"]["ownership_kind"] if p else None, "share_ratio": p["scope"]["share_ratio"] if p else None,
        "parcel_count": len(p["scope"]["parcel_pnus"]) if p else None, "composition_as_of": p["scope"]["composition_as_of"] if p else None,
        "gross_contract_krw": p["price"]["gross_contract_krw"] if p else None, "deposit_total_krw": p["price"]["deposit_total_krw"] if p else None,
        "vat_status": p["price"]["vat_status"] if p else None, "asking_confirmation": p["price"]["asking_confirmation"] if p else None, "listing_event": p["listing_event"]["kind"] if p else None,
        "target_price_krw": tp["payload"]["price_krw"] if tp else None, "target_price_kind": tp["payload"]["price_kind"] if tp else None, "target_decided_on": tp["payload"]["decided_on"] if tp else None,
        "judgment_status": jd["payload"]["status"] if jd else None, "judgment_decision": jd["payload"]["decision"] if jd else None,
        "required_equity_krw": fin["payload"]["equity_result"]["result_krw"] if fin else None, "max_required_equity_krw": fin["payload"]["cash_result"]["result_krw"] if fin else None,
        "far_headroom_m2": fr["result"]["headroom_m2"] if fr and fr.get("result") else None,
        "regulation_status": regulation_status(reg["payload"], reg["source_kind"]) if reg else None,
        "checks_ok": sum(1 for x in checks if x["verdict"] in ("ok", "not_applicable")), "checks_total": len(checks),
        "checks_blocking": "; ".join(f"{x['label']}:{x['verdict']}" for x in checks if x["verdict"] not in ("ok", "not_applicable")),
        "unknown_count": len(e["unknown"]), "signals_count": len(e["signals"]), "ready": e["ready"], "release_required": e["release_required"],
        "pre_contract_recheck": f"{rechecks['pre_contract']['reviewed_on']} {rechecks['pre_contract']['outcome']}" if "pre_contract" in rechecks else None,
        "pre_settlement_recheck": f"{rechecks['pre_settlement']['reviewed_on']} {rechecks['pre_settlement']['outcome']}" if "pre_settlement" in rechecks else None,
        "last_decision": last["decision"] if last else None, "last_decision_on": last["decided_on"] if last else None, "decisions_count": e["decisions"],
        "generated_at": generated_at, "dataset_version": dataset_version,
    }
    return {k: ("" if v is None else v) for k, v in row.items()}


def cases_csv(bundles: list[dict], generated_at: str, dataset_version: int) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    w.writeheader()
    for b in bundles:
        w.writerow(_csv_row(b, generated_at, dataset_version))
    return buf.getvalue()


def _readme(n: int, generated_at: str, dataset_version: int) -> str:
    return "\n".join([
        "매입 검토 사건 비교자료 (j5 db case-export)",
        f"물건 {n}건 · 생성 {generated_at} · 정본 dataset_version {dataset_version}",
        "",
        "- cases.csv: 물건당 한 행. 범위·가격·전략·목표 매수가·투자판단·필요자기자본·최대 필요자기자본·여유면적·규제 상태·체크리스트 판정·재확인·결정 이력 요약.",
        "- cases.json: 각 물건의 검토 기록 전체, 진입조건 판정(항목별 verdict·미확인·변경 신호·재확인), 참조 기록(목표 매수가·투자판단·자금안·개발안·규제 검토)의 저장 payload, 규제·개발안·자금안 현황, 결정 이력.",
        "- 값은 정본에 저장된 그대로다(계산 스냅샷은 저장 시점의 계산식 버전). 비어 있는 칸은 정본에 값이 없거나 미확인이다. 0 으로 읽지 않는다.",
        "- purchase_ready 는 계약 협상 준비 상태이며 대출 확약·허가·계약 적법성 보증이 아니다(ADR-07). 은행 사전 한도는 실행 확약이 아니다.",
        "- 실데이터·개인 판단이 들어 있다. 저장소·공개 배포에 넣지 않는다.",
        "",
    ])


def export_cases(db: Db, data_home: Path, *, asset_ids: list[str] | None = None, out_root: Path | None = None, today: str | None = None) -> dict:
    """모든 읽기를 한 스냅샷(읽기 트랜잭션) 안에서 해 정본 버전과 묶음이 같은 상태를 본다. 파일은 스냅샷을 닫은 뒤 쓴다."""
    data_home = Path(data_home)
    generated_at = db.now()
    with db.snapshot():
        version = int(db.meta("dataset_version") or 0)
        study_id, data_mode = db.meta("study_id"), db.data_mode
        if asset_ids:
            ids = list(asset_ids)
        else:
            ids = [r[0] for r in db.conn.execute("SELECT DISTINCT subject_id FROM records r WHERE record_type = 'acquisition_review'"
                                                 " AND NOT EXISTS (SELECT 1 FROM records n WHERE n.supersedes_id = r.record_id) ORDER BY subject_id")]
        if not ids:
            raise ValidationError(["내보낼 매입 검토 사건이 없다 (현재 검토 기록이 있는 물건이 없음). --asset 으로 지정하거나 `j5 db case-add` 로 검토를 남긴다"])
        bundles = [case_bundle(db, a, today=today) for a in ids]
        if int(db.meta("dataset_version") or 0) != version:
            raise ValidationError(["내보내는 중에 정본 dataset_version 이 바뀌었다. 다시 실행한다"])
    run_id = str(uuid.uuid4())
    root = Path(out_root) if out_root is not None else data_home / EXPORT_DIR
    stamp = generated_at.replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")
    dest = root / f"{stamp}-{run_id[:8]}"
    dest.mkdir(parents=True, exist_ok=False)
    (dest / "cases.json").write_text(json.dumps({"format": "j5cases", "cases_schema_version": "1.0.0", "generated_at": generated_at, "run_id": run_id, "dataset_version": version,
                                                 "study_id": study_id, "data_mode": data_mode, "today": bundles[0]["evaluation"]["today"], "count": len(bundles), "cases": bundles},
                                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (dest / "cases.csv").write_text(cases_csv(bundles, generated_at, version), encoding="utf-8")
    (dest / "README.txt").write_text(_readme(len(bundles), generated_at, version), encoding="utf-8")
    try:
        shown = dest.resolve().relative_to(data_home.resolve()).as_posix()
    except ValueError:
        shown = str(dest.resolve())
    return {"dest": shown, "count": len(bundles), "generated_at": generated_at, "dataset_version": version, "run_id": run_id,
            "summary": [{"asset_id": b["asset"]["asset_id"], "label": b["asset"]["label"], "tracking_status": b["asset"]["tracking_status"], "ready": b["evaluation"]["ready"],
                         "release_required": b["evaluation"]["release_required"], "blockers": len(b["evaluation"]["blockers"])} for b in bundles]}


def export_text(r: dict) -> str:
    lines = [f"비교자료 {r['count']}건: {r['dest']} (cases.csv · cases.json · README.txt) · 정본 v{r['dataset_version']} · {r['generated_at']}"]
    for s in r["summary"]:
        state = "준비 완료" if s["ready"] else f"준비 아님 {s['blockers']}건"
        lines.append(f"  {s['label']} ({s['asset_id'][:8]}) · {s['tracking_status']} · {state}" + (" · 해제 필요" if s["release_required"] else ""))
    lines.append("값은 정본 그대로이며 비교자료는 판단·계약을 대신하지 않는다. 실데이터가 들어 있으니 저장소·공개 배포에 넣지 않는다.")
    return "\n".join(lines) + "\n"
