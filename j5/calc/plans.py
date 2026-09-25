"""개발안 비교: 후보별 필요자기자본·최대 현금·CSV (데이터 사전 §9 후단·§10, 릴리스 계획 §8 R5).

현상 유지·리모델링·철거신축·공동매입 후보마다 equity(§10.1)·cash(§10.2) 계산을 돌리고 한 표로 나란히 놓는다. 계산기는 후보를 고르지 않는다(자동 판정 없음).
후보마다 점검 경고를 붙인다: 공사기간 공실 반영 여부, 부가세 포함 여부 미확인, 예비현금·비용의 이중 반영, 승계 보증금 반환 의무의 현금흐름 반영, 감정가 기준 대출 차이,
공동매입의 추가 가격·취득 순서·기간·한쪽만 확보했을 때의 운영안, 건축사 검토 유무. 경고는 결과를 막지 않고 미확인 입력만 결과를 막는다.
"""

from __future__ import annotations

import csv
import io

from j5.calc import CALCULATION_VERSION
from j5.calc.cash import max_required_equity
from j5.calc.equity import required_equity
from j5.db.validate import ValidationError

PLAN_KIND_LABEL = {"keep": "현상 유지", "remodel": "리모델링", "rebuild": "철거신축", "joint_purchase": "공동매입"}
CSV_COLUMNS = ("plan_id", "plan_kind", "label", "P_krw", "L_krw", "D_krw", "T_krw", "B_krw", "V_krw", "R_krw", "E_krw", "required_equity_krw", "remaining_cash_now_krw",
               "deposit_return_obligation_krw", "max_required_equity_krw", "min_cumulative_krw", "min_at", "periods", "appraisal_krw", "appraisal_loan_gap_krw",
               "appraisal_based_equity_krw", "construction_months", "unknown_count", "warnings")


def _flows_sorted(cash_doc: dict) -> list[dict]:
    key = (lambda t: (0, t)) if all(isinstance(f["t"], int) for f in cash_doc["flows"]) else (lambda t: (1, str(t)))
    return sorted(cash_doc["flows"], key=lambda f: key(f["t"]))


def _check_plan(plan: dict, eq: dict, ca: dict) -> list[str]:
    warnings: list[str] = []
    kind = plan["plan_kind"]
    price = plan["equity"]["price"]
    costs = plan["equity"]["costs"]
    flows = plan["cash"]["flows"]
    kinds = {f["kind"] for f in flows}
    # 공사기간 공실 (리모델링·철거신축)
    con = plan.get("construction")
    if kind in ("remodel", "rebuild"):
        if con is None or con.get("months") is None:
            warnings.append("공사기간 미확인: " + ((con or {}).get("reason") or "사유 없음"))
        else:
            months = con["months"]
            periods = sorted({f["t"] for f in flows}, key=lambda t: (0, t) if isinstance(t, int) else (1, str(t)))
            # 공사 창: 첫 설계·공사(design/construction) 항목이 있는 시점부터 월 단위면 months 개 시점, 그 밖의 단위는 그 시점 하나.
            # 임대·공실 항목 모두 이 창 안에서만 본다 (공사 뒤의 공실 항목은 공사 공실이 아니다)
            start = next((i for i, t in enumerate(periods) if any(f["t"] == t and f["kind"] in ("design", "construction") for f in flows)), 0)
            early = set(periods[start: start + max(1, months)]) if plan["cash"].get("period_unit", "month") == "month" else set(periods[start:start + 1])
            rent_early = any(f["kind"] == "rent" and (f["amount_krw"] or 0) > 0 and f["t"] in early for f in flows)
            vacancy_periods = {f["t"] for f in flows if f["kind"] == "vacancy" and f["t"] in early}
            if months > 0 and not vacancy_periods and (rent_early or "rent" not in kinds):
                warnings.append(f"공사기간 {months}개월(창 t={sorted(early, key=str)}) 안에 공실(vacancy) 항목이 없다. 공사 중 임대수입 중단·기존 임차인 명도를 반영했는지 확인")
            elif months > 0 and rent_early:
                warnings.append("공사기간 안에 임대수입(rent)이 들어온다. 공사 중에도 임대가 이어지는지 확인")
            if con.get("vacancy_months") is None:
                warnings.append("공사 중 공실 개월 수(vacancy_months) 미확인")
            elif plan["cash"].get("period_unit", "month") == "month" and len(vacancy_periods) < min(con["vacancy_months"], months):
                warnings.append(f"공실 개월 수 {con['vacancy_months']} 보다 공사 창 안의 공실 항목이 적다 ({len(vacancy_periods)}개 시점)")
            if "construction" not in kinds:
                warnings.append("공사비(construction) 항목이 현금흐름에 없다")
    # 부가세
    if price.get("vat_included") is None:
        warnings.append("가격의 부가세 포함 여부(vat_included) 미확인")
    for key in ("transaction_costs", "initial_repairs"):
        c = costs[key]
        if c.get("amount_krw") and c.get("vat_included") is None:
            warnings.append(f"{key} 의 부가세 포함 여부 미확인")
    # 비용 중복: included_in 은 실제 항목을 가리켜야 하고, 예비현금은 매입(E)과 사업기간(reserve) 중 한 곳에만
    for key, c in costs.items():
        ref = c.get("included_in")
        if ref and ref not in costs and ref != "price":
            warnings.append(f"{key}.included_in 이 가리키는 항목 {ref!r} 이 없다")
    E = costs["reserve_cash"].get("amount_krw") or 0
    has_cash_reserve = plan["cash"]["reserve_in_flows"] or (plan["cash"].get("reserve_cash_krw") or 0) > 0
    if E > 0 and has_cash_reserve:
        warnings.append(f"예비현금이 매입 필요자기자본(E {E:,})과 사업기간 계산(별도 예비현금) 양쪽에 있다. 이중 반영인지 확인")
    # 승계 보증금 반환 의무
    dep = eq.get("deposit_return_obligation_krw") or 0
    if dep > 0 and "deposit_return" not in kinds:
        warnings.append(f"승계 보증금 반환 의무 {dep:,} 이 현금흐름(deposit_return)에 없다")
    # 공동매입: 추가 가격이 가격(P)·현금흐름에 들어 있어야 후보가 성립한다. 계산기가 임의로 더하지 않는다
    if kind == "joint_purchase":
        j = plan.get("joint") or {}
        extra = j.get("extra_price", {}).get("amount_krw")
        if extra is None:
            warnings.append("공동매입 추가 가격 미확인: " + (j.get("extra_price", {}).get("reason") or "사유 없음"))
        elif eq["components_krw"]["P"] is not None and eq["components_krw"]["P"] < extra:
            warnings.append(f"가격 P {eq['components_krw']['P']:,} 이 공동매입 추가 가격 {extra:,} 보다 작다. 가격에 추가 필지가 들어 있는지 확인")
        purchase_periods = {f["t"] for f in flows if f["kind"] == "purchase"}
        if len(j.get("acquisition_order", [])) > 1 and len(purchase_periods) < 2:
            warnings.append("취득 순서가 2단계 이상인데 매입(purchase) 흐름이 한 시점뿐이다. 단계별 매입 현금을 시점별로 넣었는지 확인")
        if j.get("period_months") is None:
            warnings.append("공동매입 취득 기간 미확인")
    # 건축사 검토
    ar = plan.get("architect_review")
    if kind in ("remodel", "rebuild") and not (ar and ar.get("reviewed")):
        warnings.append("건축사 검토안이 없다. 설계 가능 연면적·주차·높이·구조는 건축사의 안으로 확인한다")
    return warnings


def _appraisal(plan: dict, eq: dict) -> dict:
    """감정가 기준 대출과 매입가 가정 대출의 차이. 감정가 × LTV 가정이 L 보다 작으면 그 차이만큼 자기자본이 더 든다."""
    ap = plan.get("appraisal")
    loan = plan["equity"]["loan"]
    out = {"appraisal_krw": None if ap is None else ap.get("amount_krw"), "loan_gap_krw": None, "appraisal_based_equity_krw": None, "note": None}
    if ap is None:
        return out
    if ap.get("amount_krw") is None:
        out["note"] = "감정가 미확인: " + (ap.get("reason") or "사유 없음")
        return out
    L, ltv = loan.get("amount_krw"), loan.get("ltv_assumption_pct")
    if L is None or ltv is None or loan.get("collateral_basis") != "purchase_price_assumption":
        out["note"] = "대출이 매입가 가정 LTV 로 잡힌 경우에만 감정가 차이를 본다"
        return out
    # L 이 보증금 공제 뒤의 순대출 한도(deposit_netted_in_limit)면 감정가 기준 한도도 같은 방식으로 D 를 뺀 값과 비교한다
    netted = 0
    if loan.get("deposit_netted_in_limit"):
        D = plan["equity"]["assumed_tenant_deposits"].get("amount_krw")
        if D is None:
            out["note"] = "순대출 한도 비교에 필요한 승계 보증금 D 가 미확인이라 감정가 차이를 내지 않는다"
            return out
        netted = D
    by_appraisal = int(ap["amount_krw"] * ltv / 100) - netted
    gap = L - by_appraisal
    out["loan_gap_krw"] = gap
    if eq.get("result_krw") is not None:
        out["appraisal_based_equity_krw"] = eq["result_krw"] + max(0, gap)
    basis = f"감정가 {ap['amount_krw']:,} × LTV {ltv}%" + (f" − 보증금 공제 {netted:,}" if netted else "")
    out["note"] = (f"{basis} = {by_appraisal:,} 이 매입가 가정 대출 {L:,} 보다 {gap:,} 적다. 그만큼 자기자본이 더 든다" if gap > 0
                   else f"감정가 기준 대출 {by_appraisal:,} 이 매입가 가정 대출 {L:,} 이상이라 자기자본이 늘지 않는다 (실제 한도는 금융기관 확인)")
    return out


def compare_plans(doc: dict) -> dict:
    ids = [p["plan_id"] for p in doc["plans"]]
    if len(set(ids)) != len(ids):
        raise ValidationError(["plan_id 가 겹친다: " + ", ".join(sorted({i for i in ids if ids.count(i) > 1}))])
    rows = []
    for plan in doc["plans"]:
        eq = required_equity(plan["equity"])
        ca = max_required_equity(plan["cash"])
        warnings = _check_plan(plan, eq, ca)
        ap = _appraisal(plan, eq)
        if ap["loan_gap_krw"] and ap["loan_gap_krw"] > 0:
            warnings.append(ap["note"])
        unknown = eq["unknown"] + ca["unknown"]
        j = plan.get("joint")
        if j is not None and j["included_in_price"] is False:
            unknown.append("공동매입 추가 가격이 이 후보의 가격(P)·현금흐름에 들어 있지 않다(included_in_price=false). 가격과 시점별 매입 흐름에 넣은 뒤 다시 계산한다")
        rows.append({"plan_id": plan["plan_id"], "plan_kind": plan["plan_kind"], "plan_kind_label": PLAN_KIND_LABEL[plan["plan_kind"]], "label": plan["label"],
                     "equity": eq, "cash": ca, "appraisal": ap, "construction_months": (plan.get("construction") or {}).get("months"),
                     "joint": plan.get("joint"), "architect_review": plan.get("architect_review"), "assumptions": plan.get("assumptions", []),
                     "resolved": not unknown and eq["result_krw"] is not None and ca["result_krw"] is not None, "unknown": unknown, "warnings": warnings})
    return {"kind": "plans", "calculation_version": CALCULATION_VERSION, "asset_label": doc["asset_label"], "composition_as_of": doc.get("composition_as_of"), "plans": rows,
            "resolved": all(r["resolved"] for r in rows), "csv_columns": list(CSV_COLUMNS),
            "note": "후보를 나란히 놓을 뿐 고르지 않는다. 미확인 입력이 있는 후보는 값이 없고, 경고는 사람이 확인할 항목이다. 여유면적·필요자기자본으로 신축 가능 여부·저평가를 판정하지 않는다"}


def plans_csv_rows(r: dict) -> list[dict]:
    out = []
    for p in r["plans"]:
        c = p["equity"]["components_krw"]
        row = {"plan_id": p["plan_id"], "plan_kind": p["plan_kind"], "label": p["label"], **{f"{k}_krw": c[k] for k in ("P", "L", "D", "T", "B", "V", "R", "E")},
               "required_equity_krw": p["equity"]["result_krw"], "remaining_cash_now_krw": p["equity"]["remaining_cash_now_krw"],
               "deposit_return_obligation_krw": p["equity"]["deposit_return_obligation_krw"], "max_required_equity_krw": p["cash"]["result_krw"],
               "min_cumulative_krw": p["cash"]["min_cumulative_krw"], "min_at": p["cash"]["min_at"], "periods": p["cash"]["periods"],
               "appraisal_krw": p["appraisal"]["appraisal_krw"], "appraisal_loan_gap_krw": p["appraisal"]["loan_gap_krw"], "appraisal_based_equity_krw": p["appraisal"]["appraisal_based_equity_krw"],
               "construction_months": p["construction_months"], "unknown_count": len(p["unknown"]), "warnings": "; ".join(p["warnings"])}
        out.append({k: ("" if v is None else v) for k, v in row.items()})
    return out


def plans_csv(r: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    w.writeheader()
    for row in plans_csv_rows(r):
        w.writerow(row)
    return buf.getvalue()


def plans_text(r: dict) -> str:
    fmt = lambda v: f"{v:,}원" if v is not None else "미확정"  # noqa: E731
    lines = [f"개발안 비교 · {r['asset_label']}" + (f" · 구성 기준일 {r['composition_as_of']}" if r["composition_as_of"] else "") + f" · 계산식 {r['calculation_version']} · 후보 {len(r['plans'])}개"]
    for p in r["plans"]:
        e, c, a = p["equity"], p["cash"], p["appraisal"]
        lines.append(f"[{p['plan_kind_label']}] {p['plan_id']} {p['label']}: 필요자기자본 {fmt(e['result_krw'])} · 최대 필요자기자본 {fmt(c['result_krw'])} (누적 최저 {c['min_cumulative_krw']:,} @ t={c['min_at']})"
                     + (f" · 감정가 기준 {fmt(a['appraisal_based_equity_krw'])}" if a["loan_gap_krw"] and a["loan_gap_krw"] > 0 else "")
                     + (f" · 공사 {p['construction_months']}개월" if p["construction_months"] is not None else ""))
        if p["joint"]:
            j = p["joint"]
            lines.append(f"  공동매입: 추가 가격 {fmt(j['extra_price'].get('amount_krw'))} · 취득 순서 {' → '.join(j['acquisition_order'])} · 기간 {j['period_months'] if j['period_months'] is not None else '미확인'}개월 · 한쪽만 확보 시 {j['partial_fallback']}")
        for u in p["unknown"]:
            lines.append(f"  미확인: {u}")
        for w in p["warnings"]:
            lines.append(f"  확인: {w}")
    lines.append(r["note"])
    return "\n".join(lines) + "\n"
