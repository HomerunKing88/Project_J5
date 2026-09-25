"""용적률 기준 여유면적 (데이터 사전 §9).

A = 법정 산정 대지면적, F = 적용 용적률(%), C = 동일 구성 범위의 현재 용적률 산정용 연면적.
검토면적 = A × F / 100, 여유면적 = A × F / 100 − C, 소진율 = C / (A × F / 100).
어느 입력이든 미확인이면 결과는 null. 분모 0 은 오류. 음수 여유면적은 그대로 표시한다. 배치 검토영역을 A 대신 자동 대입하지 않는다.
법정 제외 영역이 겹쳐도 이중 차감하지 않는다(면적 합은 검토 보조값이며 A 를 바꾸지 않는다). 여유면적으로 신축 가능 여부·저평가를 판정하지 않는다.
"""

from __future__ import annotations

from j5.calc import CALCULATION_VERSION

FORMULA = "review = A × F / 100; headroom = A × F / 100 − C; utilization = C / (A × F / 100)"


def _area(a: dict | None) -> float | None:
    return None if a is None else a.get("value_m2")


def far_headroom(doc: dict) -> dict:
    site, reg, gfa = doc["site"], doc["regulation"], doc["current_gfa"]
    notes: list[str] = []
    unknown: list[str] = []
    errors: list[str] = []
    # A: 직접 입력 또는 공부면적 − 확인된 법정 제외면적
    basis = site.get("statutory_basis", "direct")
    A = _area(site.get("statutory"))
    if basis == "registered_minus_excluded":
        reg_area, exc = _area(site.get("registered_parcel")), _area(site.get("statutory_excluded"))
        if reg_area is None or exc is None:
            A = None
            unknown.append("statutory_site_area_m2 (공부면적 또는 법정 제외면적 미확인)")
        else:
            derived = reg_area - exc
            if A is not None and abs(A - derived) > 1e-6:
                errors.append(f"site.statutory.value_m2({A}) 가 공부면적 − 제외면적({derived}) 과 다르다. 한쪽을 고친다")
            A = derived
            notes.append(f"A = 공부면적 {reg_area} − 확인된 법정 제외면적 {exc} = {derived}")
    elif A is None:
        unknown.append("statutory_site_area_m2: " + (site["statutory"].get("reason") or "사유 없음"))
    F = reg.get("applied_far_pct")
    if F is None:
        unknown.append("applied_far_pct: " + (reg.get("reason") or "사유 없음"))
    if reg.get("regulation_version") is None:
        notes.append("규제 버전(적용 법령·고시 시점)이 비어 있다. 결과에 규제 버전 없이 남긴다")
    if reg.get("end_date_confirmed") is False:
        notes.append("규제 종료일이 미확인이라 재확인 필요")
    C = _area(gfa.get("far_gfa"))
    if C is None:
        unknown.append("current_far_gfa_m2: " + (gfa["far_gfa"].get("reason") or "사유 없음"))
    total = _area(gfa.get("total_gfa"))
    if total is not None and C is not None and total < C:
        errors.append(f"총연면적({total}) 이 용적률 산정용 연면적({C}) 보다 작다")
    env = _area(site.get("placement_envelope"))
    if env is not None:
        notes.append(f"배치 검토영역 {env}㎡ 는 설계 검토용이며 분모 A 를 대신하지 않는다")
    unknown_constraints = [c["kind"] for c in site.get("area_constraints", []) if c["excluded_from_denominator"] == "unknown"]
    if unknown_constraints:
        notes.append("분모 제외 여부 미확인 제약: " + ", ".join(unknown_constraints) + " (확인 전에는 제외하지 않는다)")
    if A is not None and A <= 0:
        errors.append("법정 산정 대지면적이 0 이하다 (분모 0)")
    if F is not None and F <= 0:
        errors.append("적용 용적률이 0 이하다 (분모 0)")
    result = None
    if not unknown and not errors:
        review = A * F / 100
        result = {"review_area_m2": round(review, 3), "headroom_m2": round(review - C, 3), "utilization": round(C / review, 6)}
        if review - C < 0:
            notes.append("여유면적이 음수다. 현황이 적용 용적률을 넘거나 입력 구성 범위가 다를 수 있다. 원인을 검토한다")
    return {"kind": "far", "calculation_version": CALCULATION_VERSION, "formula": FORMULA, "asset_label": doc["asset_label"], "composition_as_of": doc.get("composition_as_of"),
            "inputs": {"A_m2": A, "F_pct": F, "C_m2": C, "total_gfa_m2": total, "placement_envelope_m2": env, "regulation_version": reg.get("regulation_version"),
                       "site_as_of": (site.get("statutory") or {}).get("as_of"), "gfa_as_of": gfa["far_gfa"].get("as_of")},
            "result": result, "unknown": unknown, "errors": errors, "notes": notes,
            "caveat": "여유면적은 개발 검토 보조값이다. 신축 가능 여부·저평가를 뜻하지 않으며 개별 필지의 규제 적용은 법령·지구단위계획·전문가 검토로 확인한다"}


def far_text(r: dict) -> str:
    i = r["inputs"]
    lines = [f"용적률 기준 여유면적 · {r['asset_label']}" + (f" · 구성 기준일 {r['composition_as_of']}" if r["composition_as_of"] else "") + f" · 계산식 {r['calculation_version']}",
             f"입력: A {i['A_m2']}㎡ (기준일 {i['site_as_of'] or '-'}) · F {i['F_pct']}% ({i['regulation_version'] or '규제 버전 없음'}) · C {i['C_m2']}㎡ (기준일 {i['gfa_as_of'] or '-'})"
             + (f" · 총연면적 {i['total_gfa_m2']}㎡" if i["total_gfa_m2"] is not None else "")]
    if r["result"]:
        x = r["result"]
        lines.append(f"결과: 검토면적 {x['review_area_m2']:g}㎡ · 여유면적 {x['headroom_m2']:g}㎡ · 소진율 {x['utilization'] * 100:.1f}%")
    else:
        lines.append("결과: 미확정 (미확인 입력 또는 오류)")
    for u in r["unknown"]:
        lines.append(f"  미확인: {u}")
    for e in r["errors"]:
        lines.append(f"  오류: {e}")
    for n in r["notes"]:
        lines.append(f"  주의: {n}")
    lines.append(r["caveat"])
    return "\n".join(lines) + "\n"
