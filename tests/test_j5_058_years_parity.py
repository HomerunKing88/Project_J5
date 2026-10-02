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
        return parcel_years(db, pnus, year_from=2000, year_to=2026)["rows"]

    built = make_view.build(tmp_path, inspect=inspect)
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

    compared = {"price": 0, "counted": 0, "missing": 0, "owner": 0}
    for pnu, rows in phone.items():
        assert rows, f"{pnu}: 폰 연도별 줄이 없다"
        for r in rows:
            key = (pnu, r["year"])
            assert key in pc, f"{key}: 폰에만 있는 연도"
            row = pc[key]
            assert r["price"] == row["price_krw_m2"], (key, r["price"], row["price_krw_m2"])
            compared["price"] += 1
            if r["tx"] == "counted":
                assert row["tx_missing_reason"] is None, (key, row)
                assert [r["exact"], r["prefix"], r["linked"]] == [row["tx_exact"], row["tx_prefix"], row["tx_linked"]], (key, r, row)
                assert r["monthsComplete"] == row["rt_months_complete"], (key, r, row)
                compared["counted"] += 1
            elif r["tx"] in ("not_collected", "incomplete"):
                assert row["tx_missing_reason"] == {"not_collected": "not_collected", "incomplete": "collection_incomplete"}[r["tx"]], (key, r, row)
                assert r["monthsAny"] == row["rt_months_any"], (key, r, row)
                compared["missing"] += 1
            else:
                assert r["tx"] in ("outside", "unknown"), (key, r)   # 폰이 판정할 수 없는 칸: 대조하지 않는다
            assert r["owner"]["known"] == row["ownership_snapshot"], (key, r, row)
            assert r["owner"]["dates"] == row["ownership_changed_on"], (key, r, row)
            compared["owner"] += 1
    # 대조가 실제로 일어났는지 (빈 비교로 통과하지 않게). 이 가상 정본은 2025·2026 을 모두 받았으므로 미수집·일부만 줄은 없다
    # (그 판정은 양쪽 단위 시험 tests/web/view.test.mjs·tests/test_j5_045_parcel_years.py 가 본다)
    assert compared["counted"] >= 12 and compared["owner"] >= 12 and compared["price"] >= 12, compared
    assert any(r["tx_exact"] for r in pc.values() if r["tx_exact"]) and any(r["tx_prefix"] for r in pc.values() if r["tx_prefix"])
