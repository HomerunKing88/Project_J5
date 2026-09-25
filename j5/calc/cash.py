"""사업기간 최대 필요자기자본 (데이터 사전 §10.2).

CF_t 에 매입·임대·운영비·이자·원금·세금·보증금 반환·명도·설계·공사·추가 대출·환급을 시점별 반영한다. 소유자의 자기자본 투입은 유입에서 제외한다.
최대 필요자기자본 = max(0, −min_t Σ CF_0..t) + 별도 예비현금. 예비현금이 CF 에 들어 있으면 마지막에 더하지 않는다.
미확인 금액이 있으면 알려진 항목 합계만 표시하고 전체값을 확정하지 않는다.
"""

from __future__ import annotations

from j5.calc import CALCULATION_VERSION
from j5.db.validate import ValidationError

FORMULA = "max_equity = max(0, −min_t Σ CF_0..t) + reserve (reserve 가 CF 에 없을 때만)"


def _key(t):
    return (0, t) if isinstance(t, int) else (1, str(t))


def max_required_equity(doc: dict) -> dict:
    flows = doc["flows"]
    if any(f["kind"] == "owner_equity" for f in flows):
        raise ValidationError(["소유자 자기자본 투입(owner_equity)은 CF_t 의 유입에 넣지 않는다. 그 항목을 뺀다"])
    kinds = {type(f["t"]) for f in flows}
    if len(kinds) > 1:
        raise ValidationError(["시점 t 는 모두 기간 번호이거나 모두 날짜여야 한다"])
    reserve_in = doc["reserve_in_flows"]
    reserve = doc.get("reserve_cash_krw")
    has_reserve_flow = any(f["kind"] == "reserve" for f in flows)
    if reserve_in and reserve is not None:
        raise ValidationError(["예비현금이 CF 에 들어 있으면(reserve_in_flows) reserve_cash_krw 는 null 로 둔다 (이중 반영 방지)"])
    if reserve_in and not has_reserve_flow:
        raise ValidationError(["reserve_in_flows 가 true 인데 kind=reserve 흐름이 없다"])
    if not reserve_in and has_reserve_flow:
        raise ValidationError(["kind=reserve 흐름이 있으면 reserve_in_flows 를 true 로 둔다"])
    unknown = [f"t={f['t']} {f['kind']}: " + (f.get("reason") or "사유 없음") for f in flows if f["amount_krw"] is None]
    by_t: dict = {}
    for f in flows:
        by_t.setdefault(f["t"], 0)
        if f["amount_krw"] is not None:
            by_t[f["t"]] += f["amount_krw"]
    periods = sorted(by_t, key=_key)
    cum, series = 0, []
    for t in periods:
        cum += by_t[t]
        series.append({"t": t, "net_krw": by_t[t], "cumulative_krw": cum})
    min_cum = min(s["cumulative_krw"] for s in series)
    min_t = next(s["t"] for s in series if s["cumulative_krw"] == min_cum)
    known = max(0, -min_cum)
    notes: list[str] = []
    if not reserve_in:
        if reserve is None:
            unknown.append("별도 예비현금(reserve_cash_krw) 미확인 (없으면 0 과 근거를 적는다)")
        else:
            known += reserve
            notes.append(f"별도 예비현금 {reserve:,} 을 마지막에 더했다")
    else:
        notes.append("예비현금이 CF 에 들어 있어 마지막에 더하지 않았다")
    result = None if unknown else known
    notes.append("기본안은 임대 잉여현금을 사업에 유보하는 조건이다. 인출 계획이 있으면 별도 유출로 넣는다")
    return {"kind": "cash", "calculation_version": CALCULATION_VERSION, "formula": FORMULA, "asset_label": doc["asset_label"], "scenario": doc.get("scenario"),
            "period_unit": doc.get("period_unit", "month"), "periods": len(periods), "series": series, "min_cumulative_krw": min_cum, "min_at": min_t,
            "reserve_cash_krw": None if reserve_in else reserve, "reserve_in_flows": reserve_in, "result_krw": result, "known_sum_krw": known, "unknown": unknown, "notes": notes,
            "caveat": "NOI·Cap rate·DSCR 은 여기서 내지 않는다. 세무 미확인 상태에서 세후수익률을 확정하지 않는다. 미확인 항목이 있으면 전체값이 아니다"}


def cash_text(r: dict) -> str:
    lines = [f"사업기간 최대 필요자기자본 · {r['asset_label']}" + (f" · {r['scenario']}" if r["scenario"] else "") + f" · 계산식 {r['calculation_version']} · 기간 {r['periods']}개({r['period_unit']})"]
    for s in r["series"]:
        lines.append(f"  t={s['t']}: 순 {s['net_krw']:,} · 누적 {s['cumulative_krw']:,}")
    lines.append(f"누적 최저 {r['min_cumulative_krw']:,}원 (t={r['min_at']})" + (f" · 별도 예비현금 {r['reserve_cash_krw']:,}원" if r["reserve_cash_krw"] is not None else ""))
    if r["result_krw"] is not None:
        lines.append(f"결과: {r['result_krw']:,}원")
    else:
        lines.append(f"결과: 미확정 · 알려진 항목 기준 {r['known_sum_krw']:,}원 (전체값이 아니다)")
    for u in r["unknown"]:
        lines.append(f"  미확인: {u}")
    for n in r["notes"]:
        lines.append(f"  주의: {n}")
    lines.append(r["caveat"])
    return "\n".join(lines) + "\n"
