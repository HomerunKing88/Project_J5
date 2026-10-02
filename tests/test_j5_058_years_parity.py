"""J5-058: 폰 필지 연도별 표(J5-047·053·055·057)가 PC `db parcels-years` 와 같은 값을 낸다.

가상 파생본 생성기(tests/fixtures/make_view.py)의 정본으로 PC(parcel_years)가 만든 필지·연도별 줄과, 같은 정본에서 만든 파생본 파일로
폰(web/app 의 parcelAssets·parcelHistory·parcelSummaryItems·priceTrend·ownershipChanges·parcelYearSummaryInfo, Node 실행)이 만든 줄을
필지·연도마다 대조한다: 공시지가, 거래 수(같은 필지·번지대·연결)와 수집 개월, 모름의 사유(미수집·일부만), 소유 변동일·자료 여부.
폰이 판정할 수 없는 칸(거래 범위 밖 동: 파생본에 거래가 없음, 거래 파일 없음)은 PC 와 대조하지 않는다. 가상자료만 쓴다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "fixtures"))

from j5.db.parcel_years import parcel_years  # noqa: E402

P1 = "9999900100100010000"
A0 = "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50"   # 가상 물건 1: PC 에서 필지 1 에 연결


def _extra(db, home):
    """대조할 갈래를 모두 만든다 (리뷰 반영 PR #105): 전년 대비(2027 공시지가), 연결 거래(지번이 다른 거래를 물건 1 에 확정 연결),
    완전 수집 0 건 해(2023), 일부만 받은 해(2024, 응답 오류), 받지 않은 해(2027 은 공시지가만 있고 수집 없음)."""
    import threading
    from http.server import HTTPServer

    import make_view
    from j5.collect.rt import collect_months
    from j5.db.parcels import load_bundle
    from j5.db.transactions import load_run
    from j5.db.txlinks import apply_decisions, candidates
    from tests.test_j5_014_collect import KEY, _Handler

    vw = json.loads(make_view.PARCELS_VW.read_text(encoding="utf-8"))
    vw["source"]["geometry_version"] = "2027-03-01"
    a1 = next(f for f in vw["features"] if f["id"] == P1)["properties"]["attrs"]
    a1.update({"official_land_price_krw_m2": 14_300_000, "price_base_year": 2027, "price_base_month": 1,
               "ownership_kind_code": "06", "ownership_kind": "법인", "ownership_changed_on": "2026-09-24", "ownership_change_cause_code": "04"})
    load_bundle(db, vw)
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        _Handler.scenarios = {"202301": {"kind": "pages", "items": []}, "202401": {"kind": "http_error"}}
        run = collect_months(home, key=KEY, key_source="fixture", lawd_cd="99999", months=["202301", "202401"],
                             endpoint=f"http://127.0.0.1:{srv.server_address[1]}/api", sleep=lambda s: None)
        load_run(db, home, "99999", run.run_id)
    finally:
        srv.shutdown()
    rows = candidates(db, "99999", ["202508"])
    for row in rows:
        row["_line"] = 2
        if row["jibun"] == "9":   # 비교 동(가상2동)의 지번 9 거래: 지번이 필지 1 과 다르지만 물건 1 의 거래로 확정
            row.update({"decision": "confirmed", "asset_id": A0, "basis_kind": "manual", "reviewed_on": "2026-09-26", "note": "가상 확정 연결"})
    apply_decisions(db, rows)

NODE = """
import { readFileSync } from "node:fs";
import { parcelAssets, priceTrend, ownershipChanges } from "./web/app/parcels.js";
import { parcelHistory, parcelSummaryItems, parcelYearSummaryInfo, parseRecordsJsonl } from "./web/app/view.js";
const d = JSON.parse(readFileSync(process.argv[1], "utf8"));
const records = parseRecordsJsonl(d.records);
const thisYear = Number(d.manifest.generated_at.slice(0, 4));
const out = {};
for (const f of d.parcels.features) {
  const p = f.properties;
  const inside = parcelAssets(f, d.seed, { linksValid: true });
  const items = parcelHistory(f, inside.map((x) => x.asset), { events: [], records, transactions: d.tx?.transactions ?? [] });
  const summary = parcelSummaryItems(items, inside.filter((x) => x.basis !== "inside").map((x) => x.asset.asset_id));
  const prices = priceTrend(p.attrs, p.attrs_history).map((q) => ({ year: q.year, price: q.price }));
  out[f.id] = parcelYearSummaryInfo(f, summary, d.tx, prices, { ownership: ownershipChanges(p.attrs, p.attrs_history), thisYear }).rows;
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_phone_year_rows_match_pc(tmp_path):
    import make_view

    def inspect(db, home, proj):
        pnus = [r["pnu"] for r in db.conn.execute("SELECT pnu FROM parcels ORDER BY pnu")]
        return parcel_years(db, pnus, year_from=2000, year_to=2027)["rows"]

    built = make_view.build(tmp_path, inspect=inspect, extra=_extra)
    pc = {(r["pnu"], r["year"]): r for r in built["inspect"]}
    with zipfile.ZipFile(built["zip"]) as z:
        data = {"seed": json.loads(z.read("assets.seed.json")), "parcels": json.loads(z.read("parcels.geojson")),
                "records": z.read("records.jsonl").decode("utf-8"), "tx": json.loads(z.read("transactions.json")) if "transactions.json" in z.namelist() else None,
                "manifest": json.loads(z.read("manifest.json"))}
    src = tmp_path / "phone-input.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(["node", "--input-type=module", "-e", NODE, str(src)], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    phone = json.loads(res.stdout)

    compared = {"price": 0, "counted": 0, "missing": 0, "owner": 0, "delta": 0, "linked": 0, "not_collected": 0, "incomplete": 0}
    for pnu, rows in phone.items():
        assert rows, f"{pnu}: 폰 연도별 줄이 없다"
        for r in rows:
            key = (pnu, r["year"])
            assert key in pc, f"{key}: 폰에만 있는 연도"
            row = pc[key]
            assert r["price"] == row["price_krw_m2"], (key, r["price"], row["price_krw_m2"])
            compared["price"] += 1
            # 전년 대비: PC 는 소수 둘째 자리로 반올림해 담고 글에서 첫째 자리로 보인다. 폰도 첫째 자리로 보인다
            assert (r["deltaPct"] is None) == (row["price_delta_pct"] is None), (key, r, row)
            if r["deltaPct"] is not None:
                assert f"{r['deltaPct']:+.1f}" == f"{row['price_delta_pct']:+.1f}", (key, r, row)
                compared["delta"] += 1
            if r["tx"] == "counted":
                assert row["tx_missing_reason"] is None, (key, row)
                assert [r["exact"], r["prefix"], r["linked"]] == [row["tx_exact"], row["tx_prefix"], row["tx_linked"]], (key, r, row)
                assert r["monthsComplete"] == row["rt_months_complete"], (key, r, row)
                compared["counted"] += 1
                compared["linked"] += r["linked"] > 0
            elif r["tx"] in ("not_collected", "incomplete"):
                assert row["tx_missing_reason"] == {"not_collected": "not_collected", "incomplete": "collection_incomplete"}[r["tx"]], (key, r, row)
                assert r["monthsAny"] == row["rt_months_any"], (key, r, row)
                compared["missing"] += 1
                compared[r["tx"]] += 1
            else:
                assert r["tx"] in ("outside", "unknown"), (key, r)   # 폰이 판정할 수 없는 칸: 대조하지 않는다
            assert r["owner"]["known"] == row["ownership_snapshot"], (key, r, row)
            assert r["owner"]["dates"] == row["ownership_changed_on"], (key, r, row)
            compared["owner"] += 1
    # 대조가 실제로 일어났는지 (빈 비교로 통과하지 않게): 갈래마다 한 줄 이상
    for k in ("counted", "owner", "price", "delta", "linked", "not_collected", "incomplete"):
        assert compared[k] >= 1, (k, compared)
    assert any(r["tx_exact"] for r in pc.values() if r["tx_exact"]) and any(r["tx_prefix"] for r in pc.values() if r["tx_prefix"])
