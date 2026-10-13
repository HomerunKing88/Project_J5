"""필지 하나의 주변 사례와 같은 동 연도별 시세, PC 판 (J5-072). 폰 필지 카드의 주변 사례(J5-064·066·071, web/app/view.js 의
`nearbyComps`·`compsByYear`)와 같은 규칙으로 정본에서 계산한다. `db parcels-comps` 가 글·JSON 으로 낸다. 정본에 쓰지 않는다.

- 사례: 이 필지와 시군구(PNU 앞 5자리)·법정동 이름이 같은 거래(취소 확정 제외), 기간(최근 n년, 월만 아는 거래는 그달 1일) 안, 고른 종류.
  폰은 파생본의 핵심·비교 범위 거래만 보므로, 범위 밖 동의 필지는 PC 에만 사례가 있다.
- 단가: 대지·연면적 평당(지분 거래·면적 없음은 null). 공시지가 배율: 지번이 온전히 닿는 필지(같은 시군구·법정동·산·본번·부번)의 그 거래 연도 이하
  가장 늦은 기준연도 공시지가로 "대지 ㎡당 거래가 ÷ 공시지가". 배율 사례가 COMP_MIN_RATIOS 건 이상이고 이 필지 공시지가가 있을 때만 참고 추정.
- 연도별 시세: 기간이 시작하는 해의 1월부터 오늘의 해까지 달력 연도마다 거래 수·대지 평당 중간값·수집 개월. 받지 않은 해의 거래 수는 0 이 아니다 (미수집).
값은 실거래 신고 자료에서 계산한 단가·배율이며 감정평가·시세 판단이 아니다 (ADR-25). 번지대 거래를 어느 필지로도 확정하지 않는다.
"""

from __future__ import annotations

import re

from j5.collect.rt import KINDS as RT_KINDS, kind_of_provider
from j5.db.parcel_timeline import fmt_krw
from j5.db.parcel_years import KIND_SHORT, PNU_RE, kinds_note
from j5.db.parcels import attribute_history
from j5.db.store import Db, DbError
from j5.db.transactions import coverage_by_year
from j5.db.txlinks import parse_jibun

PYEONG_M2 = 3.305785          # 폰 PYEONG_M2 와 같다
COMP_MIN_RATIOS = 3           # 참고 추정에 필요한 배율 사례 수 (폰 COMP_MIN_RATIOS)
TREND_MIN_CASES = COMP_MIN_RATIOS   # 연도별 변동에 필요한 그해 대지 평당 사례 수 (폰 TREND_MIN_CASES)
COMP_YEARS = (1, 3, 5, 0)     # 폰 DEAL_YEARS, 0 = 전체
KIND_CHOICES = ("all", *RT_KINDS)


def median(values: list[float]) -> float | None:
    v = sorted(values)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def _deal_date(t) -> str:
    return t["deal_date"] or f"{t['deal_ymd'][:4]}-{t['deal_ymd'][4:6]}"


def _since(years: int, on_date: str) -> str | None:
    return f"{int(on_date[:4]) - years}{on_date[4:]}" if years else None


def _price_for_year(series: list[dict], year: int) -> dict | None:
    """기준연도가 year 이하인 공시지가 가운데 가장 늦은 것 {price, year}. 더 늦은 해의 값으로 거슬러 쓰지 않는다 (폰 priceForYear)."""
    best = None
    for pt in series:
        y = pt.get("base_year")
        if not isinstance(y, int) or y > year:
            continue
        if best is None or (y, pt.get("base_month") or 0) >= (best["base_year"], best.get("base_month") or 0):
            best = pt
    return {"price": best["price_krw_m2"], "year": best["base_year"]} if best else None


def _stats(values: list[float]) -> dict:
    return {"n": len(values), "median": median(values), "min": min(values) if values else None, "max": max(values) if values else None}


def parcel_comps(db: Db, pnu: str, *, years: int = 5, kind: str = "all", on_date: str | None = None) -> dict:
    if not PNU_RE.match(pnu or ""):
        raise DbError("bad_pnu", f"PNU 는 19자리 숫자: {pnu}")
    if years not in COMP_YEARS:
        raise DbError("bad_years", f"기간은 {', '.join(map(str, COMP_YEARS))} 가운데 하나 (0 = 전체): {years}")
    if kind not in KIND_CHOICES:
        raise DbError("bad_kind", f"종류는 {', '.join(KIND_CHOICES)} 가운데 하나: {kind}")
    on_date = on_date or db.now()[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", on_date):
        raise DbError("bad_date", f"기준일은 YYYY-MM-DD: {on_date}")
    h = attribute_history(db, pnu)   # 정본에 없으면 parcel_missing
    me = db.conn.execute("SELECT emd_code, emd_name FROM parcels WHERE pnu = ?", (pnu,)).fetchone()
    sgg, emd = me["emd_code"][:5], me["emd_name"]
    since = _since(years, on_date)
    from_year = int(since[:4]) if since else None
    rows = []
    if db._has_table("transactions") and emd is not None:
        rows = [t for t in db.conn.execute("SELECT * FROM transactions WHERE emd_name = ? AND cancel_status <> 'cancelled'", (emd,))
                if (t["lawd_cd"] is None or t["lawd_cd"] == sgg) and (kind == "all" or _kind(t) == kind)]
    # 지번이 온전한 거래가 닿는 필지 (같은 시군구·법정동·산·본번·부번). 공시지가 이력은 필지마다 한 번 읽는다
    parcels = {}
    for p in db.conn.execute("SELECT pnu, mountain, bon, bu FROM parcels WHERE emd_name = ? AND substr(emd_code, 1, 5) = ?", (emd, sgg)):
        parcels.setdefault((bool(p["mountain"]), p["bon"], p["bu"] or 0), p["pnu"])
    series: dict[str, list] = {}

    def comp(t) -> dict:
        j = parse_jibun(t["jibun_raw"])
        exact = parcels.get((j["mountain"], j["bon"], j["bu"])) if j and not j["masked"] else None
        land = unit(t, t["plottage_area_m2"])
        pr = None
        if exact is not None:
            if exact not in series:
                series[exact] = attribute_history(db, exact)["price_series"] if exact != pnu else h["price_series"]
            pr = _price_for_year(series[exact], int(t["deal_ymd"][:4]))
        ok = land is not None and pr is not None and pr["price"] > 0
        return {"transaction_id": t["transaction_id"], "deal_date": _deal_date(t), "deal_kind": _kind(t), "jibun": t["jibun_raw"], "jibun_masked": bool(t["jibun_masked"]),
                "amount_krw": t["amount_krw"], "share_deal": bool(t["share_deal"]), "building_use": t["building_use_raw"],
                "land_unit_krw_py": land, "floor_unit_krw_py": unit(t, t["building_area_m2"]),
                "ratio": (t["amount_krw"] / t["plottage_area_m2"]) / pr["price"] if ok else None, "price_year": pr["year"] if ok else None, "mine": exact == pnu}

    def in_period(t, start: str | None) -> bool:
        return not start or f"{_deal_date(t)}-01"[:10] >= start

    order = lambda c: (c["deal_date"], c["transaction_id"])   # noqa: E731 (폰 byDealDesc 의 역순 기준)
    comps = sorted((comp(t) for t in rows if in_period(t, since)), key=order, reverse=True)
    lands = [c["land_unit_krw_py"] for c in comps if c["land_unit_krw_py"] is not None]
    floors = [c["floor_unit_krw_py"] for c in comps if c["floor_unit_krw_py"] is not None]
    ratios = [c["ratio"] for c in comps if c["ratio"] is not None]
    r = median(ratios)
    own = _price_for_year(h["price_series"], 9999)
    if len(ratios) < COMP_MIN_RATIOS:
        reference = {"missing": f"공시지가와 견줄 수 있는 사례가 {len(ratios)}건이라 참고값을 내지 않는다 ({COMP_MIN_RATIOS}건부터)"}
    elif own is None:
        reference = {"missing": "이 필지의 공시지가 자료가 없어 참고값을 내지 않는다"}
    else:
        reference = {"per_m2": own["price"] * r, "unit_krw_py": own["price"] * r * PYEONG_M2, "price_krw_m2": own["price"], "price_year": own["year"], "ratio": r, "n": len(ratios)}
    # 연도별 시세 (J5-071): 기간이 시작하는 해의 1월부터
    ycomps = [comp(t) for t in rows if in_period(t, f"{from_year}-01-01" if from_year else None)]
    by_year = _by_year(db, ycomps, sgg, kind, from_year, int(on_date[:4]))
    return {"pnu": pnu, "parcel": h["label"], "sgg_code": sgg, "emd_name": emd, "on_date": on_date, "years": years, "since": since, "kind": kind,
            "count": len(comps), "land": _stats(lands), "floor": _stats(floors), "ratio": {"n": len(ratios), "median": r}, "reference": reference,
            "comps": comps, "by_year": by_year}


def _kind(t) -> str:
    k = kind_of_provider(t["provider"])
    return k.name if k else "nrg"   # 폰 dealKindOf: 모르는 값은 상업·업무용


def unit(t, area) -> float | None:
    """평당 원 (지분 거래·면적 없음은 null, 폰 unitPrice)."""
    if t["amount_krw"] is None or t["share_deal"] or area is None or area <= 0:
        return None
    return t["amount_krw"] / (area / PYEONG_M2)


def _by_year(db: Db, comps: list[dict], sgg: str, kind: str, from_year: int | None, this_year: int) -> dict | None:
    """폰 compsByYear 와 같은 규칙. 거래가 없으면 None."""
    if not comps:
        return None
    ys = [int(c["deal_date"][:4]) for c in comps]
    y0 = min([from_year, *ys]) if from_year is not None else min(ys)
    y1 = max([*ys, this_year])
    cov_all = coverage_by_year(db, sgg)
    cov_kind = {k: coverage_by_year(db, sgg, k) for k in RT_KINDS}
    out = []
    for y in range(y0, y1 + 1):
        inyear = [c for c in comps if int(c["deal_date"][:4]) == y]
        lands = [c["land_unit_krw_py"] for c in inyear if c["land_unit_krw_py"] is not None]
        kind_months = {k: c[str(y)]["complete"] for k, c in cov_kind.items() if c.get(str(y), {}).get("complete")}
        complete = cov_all.get(str(y), {}).get("complete", 0)
        # 정본은 자기 수집 기록을 안다: 기록이 없는 해는 받지 않은 해다 (폰은 수집 현황이 없는 이전 파일에서만 모름)
        months = complete if kind == "all" else kind_months.get(kind, 0)
        counted = "counted" if inyear or months > 0 else "not_collected"
        out.append({"year": y, "count": len(inyear), "counted": counted, **_stats(lands), "months": months,
                    "kinds": kinds_note(kind_months, complete) if kind == "all" else "", "partial": y == this_year})
    q = [r for r in out if r["n"] >= TREND_MIN_CASES]
    if len(q) < 2:
        change = {"missing": f"대지 평당 사례가 {TREND_MIN_CASES}건 이상인 해가 {len(q)}개라 시작·최근 변동을 적지 않는다 (2개 해부터)"}
    else:
        change = {"from": q[0]["year"], "to": q[-1]["year"], "pct": (q[-1]["median"] - q[0]["median"]) / q[0]["median"] * 100}
    return {"years": out, "change": change}


def _py(v) -> str:
    return f"{fmt_krw(round(v))}/평" if v is not None else "모름"


def comps_text(r: dict) -> str:
    scope = f"최근 {r['years']}년 ({r['since']}부터)" if r["years"] else "전체 기간"
    lines = [f"{r['parcel']} (PNU {r['pnu']}) 주변 사례: {r['emd_name']}, {scope}" + ("" if r["kind"] == "all" else f", {KIND_SHORT[r['kind']]}") + f", 기준일 {r['on_date']}",
             f"  거래 {r['count']}건"]
    if r["count"]:
        for name, s in (("대지 평당", r["land"]), ("연면적 평당", r["floor"])):
            lines.append(f"  {name}: " + (f"중간 {_py(s['median'])} ({_py(s['min'])}~{_py(s['max'])}, {s['n']}건)" if s["n"] else "면적이 있는 거래 없음"))
        lines.append("  공시지가 대비: " + (f"중간 {r['ratio']['median']:.2f}배 ({r['ratio']['n']}건, 그 필지의 거래 연도 이하 가장 최근 공시지가 기준)" if r["ratio"]["n"] else "견줄 수 있는 사례 없음"))
        ref = r["reference"]
        lines.append("  " + (ref["missing"] if "missing" in ref else
                             f"참고 추정: 이 필지 공시지가 {int(ref['price_krw_m2']):,}원/㎡ ({ref['price_year']}년) × {ref['ratio']:.2f}배 ≈ {_py(ref['unit_krw_py'])} (사례 {ref['n']}건, 추정값이며 시세 판단이 아니다)"))
        lines.append("  사례 (최근 먼저):")
        for c in r["comps"]:
            where = (c["jibun"] or "지번 미공개") + (" 번지대" if c["jibun_masked"] else "") + (" (이 필지)" if c["mine"] else "")
            extra = ", 지분 거래" if c["share_deal"] else ""
            ratio = f", {c['price_year']}년 공시지가의 {c['ratio']:.2f}배" if c["ratio"] is not None else ""
            lines.append(f"    {c['deal_date']} {KIND_SHORT[c['deal_kind']]} {fmt_krw(c['amount_krw'])} · {where} · 대지 {_py(c['land_unit_krw_py'])}{extra}{ratio}")
    by = r["by_year"]
    if by:
        ch = by["change"]
        lines.append("  연도별 시세 (기간이 시작하는 해의 1월부터, 최근 먼저): " + (ch["missing"] if "missing" in ch else f"대지 평당 중간값 {ch['from']}→{ch['to']}년 {ch['pct']:+.1f}%"))
        for y in reversed(by["years"]):
            if y["counted"] == "not_collected":
                lines.append(f"    {y['year']}{' (오늘까지)' if y['partial'] else ''}: 거래 미수집 (0 건이 아니다)")
                continue
            cnt = f"{y['count']}건"
            col = "미수집" if y["months"] == 0 else f"수집 {y['months']}/12개월" + (f", {y['kinds']}" if y["kinds"] else "")
            lines.append(f"    {y['year']}{' (오늘까지)' if y['partial'] else ''}: 거래 {cnt} · 대지 평당 " + (f"중간 {_py(y['median'])} ({y['n']}건)" if y["n"] else "사례 없음") + f" · {col}")
    lines.append("실거래 신고 자료에서 계산한 단가와 배율이며 감정평가가 아니다. 지분 거래는 단가에서 뺀다. 번지대 거래는 어느 필지로도 확정하지 않는다.")
    return "\n".join(lines) + "\n"
