"""여러 필지의 연도별 표, PC 판 (J5-045). 필지마다 연도마다 한 줄: 공시지가(그 연도 기준)·거래 수집 개월·이 필지에 닿는 거래 수·소유 변동일.
`db parcels-years` 가 CSV(덮어쓰지 않음)·글·JSON 으로 낸다. 정본에 쓰지 않는다.

- 대상: `--pnu`, `parcels-find --csv` 의 파일, `--watchlist`(관찰목록 물건에 오늘 연결된 필지). 한 번에 MAX_PARCELS 필지.
- 공시지가: 토지특성 스냅샷의 공시지가 추이(J5-030 price_series)에서 기준연도가 그 연도인 점. 같은 기준연도에 값이 둘 이상이면 나중 확인 값과 `price_corrected`.
  그 연도 기준의 스냅샷이 없으면 null + `price_missing_reason = "no_snapshot"`(0 이 아니다). 전년 대비는 두 해 값이 모두 있을 때만.
- 거래: 필지 이력(J5-043·044)과 같은 규칙으로 모은 거래(같은 필지·번지대·연결 물건, 취소 확정 제외)를 계약연도별로 센다.
  그 필지 시군구의 그 연도에 완전 수집(complete/empty)한 달이 없으면 거래 수는 null + 사유(`not_collected` 실행 없음, `collection_incomplete` 실패·부분만)
  로 둔다(0 건과 구분한다, 데이터 사전 §7.1 "API 실패·빈 결과·일부 페이지 누락 구분"). 그해 거래가 정본에 있으면 센 값을 적는다.
  수집한 달 수(`rt_months_complete`: complete/empty, `rt_months_any`: 결과와 무관)를 함께 적는다. 12개월이 아니면 일부 연도다.
- 소유 변동일: 토지소유 스냅샷에 적힌 소유 변동일 중 그 연도의 날짜. 토지소유 스냅샷이 없는 필지는 `ownership_snapshot` 이 false 이고 빈 칸은 모름이다.
값은 자료 표기 그대로이며 시세·가치 판단이 아니다. 번지대 거래를 이 필지의 거래로 확정하지 않는다.
"""

from __future__ import annotations

import csv
import io
import re

from j5.db.parcel_timeline import fmt_krw, parcel_transactions
from j5.db.parcels import active_links, attribute_history
from j5.db.schema import WATCHLIST_STATUSES
from j5.db.store import Db, DbError

MAX_PARCELS = 500
MAX_YEARS = 40
PNU_RE = re.compile(r"^\d{19}$")
CSV_FIELDS = ("pnu", "parcel", "sgg_code", "year", "price_krw_m2", "price_delta_pct", "price_as_of", "price_corrected", "price_missing_reason",
              "rt_months_complete", "rt_months_any", "tx_exact", "tx_prefix", "tx_linked", "tx_missing_reason", "tx_exact_last_date", "tx_exact_last_amount_krw",
              "ownership_snapshot", "ownership_changed_on")


def watchlist_pnus(db: Db, on_date: str) -> list[str]:
    """관찰목록(watch·detailed_review·purchase_ready) 물건에 기준일에 연결된 필지 (PNU 순)."""
    ph = ",".join("?" * len(WATCHLIST_STATUSES))
    watch = {r[0] for r in db.conn.execute(f"SELECT asset_id FROM assets WHERE tracking_status IN ({ph})", WATCHLIST_STATUSES)}
    return sorted({l["pnu"] for l in active_links(db, on_date) if l["asset_id"] in watch})


def _coverage_by_year(db: Db, sgg: str) -> dict[str, dict]:
    """시군구의 연도별 수집 개월 수 {year: {complete, any}} (collection_runs 가 없으면 빈 dict)."""
    if not db._has_table("collection_runs"):
        return {}
    months: dict[str, set] = {}
    for r in db.conn.execute("SELECT deal_ymd, outcome FROM collection_runs WHERE lawd_cd = ?", (sgg,)):
        months.setdefault(r["deal_ymd"], set()).add(r["outcome"])
    out: dict[str, dict] = {}
    for ym, outcomes in months.items():
        y = out.setdefault(ym[:4], {"complete": 0, "any": 0})
        y["any"] += 1
        y["complete"] += int(bool(outcomes & {"complete", "empty"}))
    return out


def _ownership_dates(snapshots: list[dict]) -> list[str]:
    dates = set()
    for s in snapshots:
        if s["kind"] == "land_ownership":
            d = s["values"].get("ownership_changed_on")
            if isinstance(d, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                dates.add(d)
    return sorted(dates)


def _prices_by_year(series: list[dict]) -> dict[int, dict]:
    """기준연도별 공시지가 (같은 기준연도에 여러 값이면 나중 확인 값 + corrected)."""
    out: dict[int, dict] = {}
    for pt in series:
        y = pt.get("base_year")
        if not isinstance(y, int):
            continue
        prev = out.get(y)
        out[y] = {"price": pt["price_krw_m2"], "as_of": pt["as_of"], "corrected": bool(prev) and prev["price"] != pt["price_krw_m2"] or bool(prev and prev["corrected"])}
    return out


def parcel_years(db: Db, pnus: list[str], *, year_from: int | None = None, year_to: int | None = None, on_date: str | None = None) -> dict:
    pnus = list(dict.fromkeys(pnus))
    bad = [p for p in pnus if not PNU_RE.match(p)]
    if bad:
        raise DbError("bad_pnu", f"PNU 는 19자리 숫자: {', '.join(bad[:5])}")
    if not pnus:
        raise DbError("no_pnu", "대상 필지가 없다 (--pnu, --csv 또는 --watchlist)")
    if len(pnus) > MAX_PARCELS:
        raise DbError("too_many", f"한 번에 {MAX_PARCELS}필지까지 ({len(pnus)}필지). 대상을 좁히거나 나눠 만든다")
    rows = {r["pnu"]: r for r in db.conn.execute(f"SELECT pnu, emd_code FROM parcels WHERE pnu IN ({','.join('?' * len(pnus))})", pnus)}
    missing = [p for p in pnus if p not in rows]
    if missing:
        raise DbError("parcel_missing", f"정본에 없는 필지: {', '.join(missing[:5])}" + (f" 외 {len(missing) - 5}개" if len(missing) > 5 else ""))
    on_date = on_date or db.now()[:10]
    this_year = int(on_date[:4])
    per = []
    data_years: set[int] = set()
    cov_cache: dict[str, dict] = {}
    for pnu in pnus:
        h = attribute_history(db, pnu)
        tx = parcel_transactions(db, pnu, on_date=on_date)
        sgg = rows[pnu]["emd_code"][:5]
        if sgg not in cov_cache:
            cov_cache[sgg] = _coverage_by_year(db, sgg)
        prices = _prices_by_year(h["price_series"])
        own = _ownership_dates(h["snapshots"])
        per.append((pnu, h, tx, sgg, prices, own))
        # 기본 범위는 공시지가·거래·수집이 있는 연도로 정한다. 소유 변동일은 오래된 날짜가 많아 범위를 정하는 데 쓰지 않는다(범위 안이면 적는다)
        data_years |= set(prices) | {int(t["year"]) for t in tx["transactions"]} | {int(y) for y in cov_cache[sgg]}
    y0 = year_from if year_from is not None else (min(data_years) if data_years else this_year)
    y1 = year_to if year_to is not None else max([this_year, *data_years])
    if y0 > y1:
        raise DbError("bad_years", f"시작 연도가 끝 연도보다 늦다: {y0} > {y1}")
    if y1 - y0 + 1 > MAX_YEARS:
        raise DbError("too_many_years", f"한 번에 {MAX_YEARS}개 연도까지 ({y0}~{y1}). --from/--to 로 좁힌다")
    out_rows = []
    for pnu, h, tx, sgg, prices, own in per:
        by_year: dict[str, list] = {}
        for t in tx["transactions"]:
            by_year.setdefault(t["year"], []).append(t)
        prev_price = None
        for y in range(y0, y1 + 1):
            p = prices.get(y)
            cov = cov_cache[sgg].get(str(y), {"complete": 0, "any": 0})
            txs = by_year.get(str(y), [])
            collected = cov["complete"] > 0   # 완전 수집(complete/empty)한 달이 하나라도 있어야 거래 수를 적는다
            exact = [t for t in txs if t["match"] == "exact"]
            # 계약월(YYYYMM)로 먼저 비교하고 같은 달 안에서 계약일(YYYY-MM-DD, 없으면 가장 앞)로 비교한다: 두 형식을 섞어 비교하지 않는다 (리뷰 반영 PR #92)
            last = max(exact, key=lambda t: (t["deal_ymd"], t["deal_date"] or "", t["transaction_id"])) if exact else None
            price = p["price"] if p else None
            out_rows.append({
                "pnu": pnu, "parcel": h["label"], "sgg_code": sgg, "year": y,
                "price_krw_m2": price, "price_delta_pct": round((price - prev_price) / prev_price * 100, 2) if price is not None and prev_price else None,
                "price_as_of": p["as_of"] if p else None, "price_corrected": bool(p and p["corrected"]), "price_missing_reason": None if p else "no_snapshot",
                "rt_months_complete": cov["complete"], "rt_months_any": cov["any"],
                # 완전 수집한 달이 없는 연도의 거래 수는 모름이다. 다만 그해 거래가 정본에 있으면(부분 수집·연결 거래 등) 센 값을 그대로 둔다
                "tx_exact": len(exact) if collected or txs else None, "tx_prefix": sum(t["match"] == "prefix" for t in txs) if collected or txs else None,
                "tx_linked": sum(t["match"] == "linked" for t in txs) if collected or txs else None,
                "tx_missing_reason": None if collected or txs else ("collection_incomplete" if cov["any"] else "not_collected"),
                "tx_exact_last_date": (last["deal_date"] or last["deal_ymd"]) if last else None, "tx_exact_last_amount_krw": last["amount_krw"] if last else None,
                "ownership_snapshot": any(s["kind"] == "land_ownership" for s in h["snapshots"]),
                "ownership_changed_on": [d for d in own if d.startswith(str(y))]})
            prev_price = price
    return {"parcels": len(pnus), "year_from": y0, "year_to": y1, "on_date": on_date, "rows": out_rows}


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, list):
        return ";".join(v)
    return str(v)


def years_csv(r: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_FIELDS)
    for row in r["rows"]:
        w.writerow([_cell(row[k]) for k in CSV_FIELDS])
    return buf.getvalue()


def years_text(r: dict) -> str:
    lines = [f"필지 {r['parcels']}개 · {r['year_from']}~{r['year_to']}년 (최근 먼저). 값은 자료 그대로이며 시세·가치 판단이 아니다."]
    cur = None
    order = {p: i for i, p in enumerate(dict.fromkeys(x["pnu"] for x in r["rows"]))}   # 입력 순서를 지키고 필지 안에서만 연도를 최근 먼저 (리뷰 반영 PR #92)
    for row in sorted(r["rows"], key=lambda x: (order[x["pnu"]], -x["year"])):
        if row["pnu"] != cur:
            cur = row["pnu"]
            lines.append(f"{row['parcel']} (PNU {row['pnu']})")
        if row["price_krw_m2"] is None:
            price = "공시지가 자료 없음"
        else:
            price = f"공시지가 {int(row['price_krw_m2']):,}원/㎡" + (f" ({row['price_delta_pct']:+.1f}%)" if row["price_delta_pct"] is not None else "") + (" · 같은 기준연도 값 바뀜" if row["price_corrected"] else "")
        if row["tx_missing_reason"]:
            tx = "거래 미수집" if row["tx_missing_reason"] == "not_collected" else f"거래 수집 실패·부분만 ({row['rt_months_any']}개월 시도)"
        else:
            tx = f"거래 수집 {row['rt_months_complete']}/12개월 · 같은 필지 {row['tx_exact']}" + (f" ({fmt_krw(row['tx_exact_last_amount_krw'])}, {row['tx_exact_last_date']})" if row["tx_exact_last_date"] else "") + f" · 번지대 {row['tx_prefix']} · 연결 {row['tx_linked']}"
        own = (" · 소유 변동 " + ", ".join(row["ownership_changed_on"])) if row["ownership_changed_on"] else ("" if row["ownership_snapshot"] else " · 소유 자료 없음")
        lines.append(f"  {row['year']}: {price} · {tx}{own}")
    lines.append("거래 미수집 연도의 빈 칸은 0 건이 아니다. 번지대 거래는 이 필지의 거래로 확정하지 않는다.")
    return "\n".join(lines) + "\n"
