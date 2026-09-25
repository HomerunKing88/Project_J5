"""가격 기준 정규화와 매입 전체 필요자기자본 (데이터 사전 §10.1, ADR-08).

P = 보증금 차감 전 전체 계약금액. 총액 입력이면 P = 입력값, 보증금 차감 후 입력이면 P = 입력값 + 확인된 차감액. 잔금(settlement_residual)·기준 미확인이면 P 를 확정하지 않는다.
매입 전체 필요자기자본 = P − L − D + T + B + V + R + E.
L 새 대출(실제 또는 명시한 시나리오), D 정산에 반영되는 승계 보증금(반환 의무는 남는다), T 취득세 등, B 거래·실사비, V 별도 선지급 세액, R 초기 수선, E 예비현금.
가격에 이미 든 비용은 다시 더하지 않는다. 보증금 공제가 금융기관 순대출 한도에 이미 들어 있으면 D 를 다시 차감하지 않는다. 미확인 항목이 있으면 알려진 항목 합계만 보인다.
"""

from __future__ import annotations

from j5.calc import CALCULATION_VERSION
from j5.db.validate import ValidationError

FORMULA = "equity = P − L − D + T + B + V + R + E (P = 보증금 차감 전 계약 총액)"
COST_KEYS = (("acquisition_tax", "T", "취득세 등"), ("transaction_costs", "B", "거래·실사비"), ("prepaid_taxes", "V", "별도 선지급 세액"), ("initial_repairs", "R", "초기 수선"),
             ("reserve_cash", "E", "예비현금"))


def normalize_price(price: dict) -> tuple[int | None, list[str], list[str]]:
    """(P, 미확인 사유, 주의). 잔금만으로 총액을 복원하지 않는다."""
    basis, v, ded = price["price_input_basis"], price.get("price_input_krw"), price.get("deposit_already_deducted_krw")
    notes: list[str] = []
    if basis == "gross_contract":
        if ded:
            raise ValidationError(["총액 입력(gross_contract)에는 deposit_already_deducted_krw 를 두지 않는다 (차감 후 입력이면 net_after_deposit)"])
        if v is None:
            return None, ["price_input_krw 미확인: " + (price.get("reason") or "사유 없음")], notes
        return v, [], notes
    if basis == "net_after_deposit":
        if v is None:
            return None, ["price_input_krw 미확인: " + (price.get("reason") or "사유 없음")], notes
        if ded is None:
            return None, ["보증금 차감 후 입력인데 차감액(deposit_already_deducted_krw)이 미확인이라 총액 P 를 복원하지 못한다"], notes
        notes.append(f"P = 차감 후 입력 {v:,} + 이미 차감한 보증금 {ded:,} = {v + ded:,} (부분 차감이면 차감액만 반영)")
        return v + ded, [], notes
    if basis == "settlement_residual":
        return None, ["잔금·정산 잔액만으로는 총액 P 를 복원하지 않는다 (다른 채무·기지급금이 섞일 수 있음)"], notes
    return None, ["가격 기준(price_input_basis)이 미확인이라 완성 결과를 내지 않는다"], notes


def _cost(c: dict, label: str) -> tuple[int | None, str | None, str | None]:
    """(반영 금액, 미확인 사유, 주의)."""
    if c.get("included_in_price"):
        return 0, None, f"{label} 은 가격(P)에 이미 포함돼 0 으로 둔다"
    if c.get("included_in"):
        return 0, None, f"{label} 은 {c['included_in']} 에 이미 포함돼 0 으로 둔다"
    if c.get("amount_krw") is None:
        return None, f"{label} 미확인: " + (c.get("reason") or "사유 없음"), None
    return int(c["amount_krw"]), None, None


def required_equity(doc: dict) -> dict:
    unknown: list[str] = []
    notes: list[str] = []
    P, u, n = normalize_price(doc["price"])
    unknown += u
    notes += n
    loan = doc["loan"]
    L = loan.get("amount_krw")
    if L is None:
        unknown.append("대출 L 미확인: " + (loan.get("reason") or "사유 없음"))
    elif loan["kind"] == "scenario":
        notes.append(f"L 은 시나리오 가정이다 (LTV 가정 {loan.get('ltv_assumption_pct')}%, 담보 기준 {loan.get('collateral_basis') or '미표시'}). 실제 승인과 분리한다")
    elif loan["kind"] == "unknown":
        notes.append("L 의 종류(승인/시나리오)가 미확인이다")
    D_in, du, dn = _cost(doc["assumed_tenant_deposits"], "승계 보증금 D")
    if du:
        unknown.append(du)
    if dn:
        notes.append(dn)
    D = D_in
    if D is not None and loan.get("deposit_netted_in_limit"):
        notes.append(f"보증금 공제 {D:,} 이 금융기관 순대출 한도에 이미 들어 있어 D 를 다시 차감하지 않는다")
        D = 0
    comps = {"P": P, "L": L, "D": D}
    for key, sym, label in COST_KEYS:
        v, cu, cn = _cost(doc["costs"][key], f"{label} {sym}")
        comps[sym] = v
        if cu:
            unknown.append(cu)
        if cn:
            notes.append(cn)
    signs = {"P": 1, "L": -1, "D": -1, "T": 1, "B": 1, "V": 1, "R": 1, "E": 1}
    known_sum = sum(signs[k] * v for k, v in comps.items() if v is not None)
    result = None if unknown else known_sum
    deposit_obligation = D_in if D_in is not None else doc["assumed_tenant_deposits"].get("amount_krw")
    prepaid = doc["price"].get("prepaid_krw")
    if prepaid:
        notes.append(f"기지급 계약금·중도금 {prepaid:,} 은 지급 시점별 현금흐름에 반영한다. 전체 필요자기자본에서 빼지 않는다 (현재 시점 추가 잔금 현금 = 전체 − 기지급)")
    return {"kind": "equity", "calculation_version": CALCULATION_VERSION, "formula": FORMULA, "asset_label": doc["asset_label"], "scenario": doc.get("scenario"),
            "price_basis": doc["price"]["price_input_basis"], "components_krw": comps, "result_krw": result, "known_sum_krw": known_sum,
            "remaining_cash_now_krw": (result - prepaid) if (result is not None and prepaid) else None,
            "deposit_return_obligation_krw": deposit_obligation, "unknown": unknown, "notes": notes,
            "caveat": "LTV·대출은 가정이며 실제 승인과 분리한다. 세무 미확인 항목을 0 으로 두지 않는다. 승계 보증금의 반환 의무는 정산과 무관하게 남는다"}


def equity_text(r: dict) -> str:
    c = r["components_krw"]
    fmt = lambda v: f"{v:,}원" if v is not None else "미확인"  # noqa: E731
    lines = [f"매입 전체 필요자기자본 · {r['asset_label']}" + (f" · {r['scenario']}" if r["scenario"] else "") + f" · 계산식 {r['calculation_version']}",
             f"P {fmt(c['P'])} ({r['price_basis']}) − L {fmt(c['L'])} − D {fmt(c['D'])} + T {fmt(c['T'])} + B {fmt(c['B'])} + V {fmt(c['V'])} + R {fmt(c['R'])} + E {fmt(c['E'])}"]
    if r["result_krw"] is not None:
        lines.append(f"결과: {r['result_krw']:,}원" + (f" · 현재 시점 추가 잔금 현금 {r['remaining_cash_now_krw']:,}원" if r["remaining_cash_now_krw"] is not None else ""))
    else:
        lines.append(f"결과: 미확정 · 알려진 항목 합계 {r['known_sum_krw']:,}원 (전체값이 아니다)")
    if r["deposit_return_obligation_krw"] is not None:
        lines.append(f"승계 보증금 반환 의무: {r['deposit_return_obligation_krw']:,}원 (후속 현금유출로 남는다)")
    for u in r["unknown"]:
        lines.append(f"  미확인: {u}")
    for n in r["notes"]:
        lines.append(f"  주의: {n}")
    lines.append(r["caveat"])
    return "\n".join(lines) + "\n"
