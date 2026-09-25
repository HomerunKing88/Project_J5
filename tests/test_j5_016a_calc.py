"""J5-016A: 계산기 (R5). 데이터 사전 §9 용적률 기준 여유면적, §10.1 가격 정규화·매입 전체 필요자기자본, §10.2 사업기간 최대 필요자기자본, ADR-08.

가상 검증 사례(데이터 사전 표)를 그대로 재현하고, 미확인 입력·분모 0·배치 검토영역 자동 대입 금지·순액 이중 차감 금지·잔금만으로 총액 복원 금지·
소유자 자기자본 유입 금지·예비현금 이중 반영 금지를 시험한다. 가상자료만 쓴다.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from j5 import cli
from j5.calc import CALCULATION_VERSION
from j5.calc.cash import max_required_equity
from j5.calc.equity import normalize_price, required_equity
from j5.calc.far import far_headroom
from j5.calc.inputs import calc_text, load_calc_input, run_calc
from j5.db.validate import ValidationError

FIX = Path(__file__).resolve().parent / "fixtures" / "calc"
EOK = 100_000_000


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def test_far_dictionary_cases():
    r = far_headroom(fixture("far_gross"))
    assert r["result"] == {"review_area_m2": 600, "headroom_m2": 360, "utilization": 0.4} and r["unknown"] == [] and r["errors"] == []
    assert r["inputs"]["placement_envelope_m2"] == 60 and any("분모 A 를 대신하지 않는다" in n for n in r["notes"]), "배치 검토영역 60㎡ 는 분모를 바꾸지 않는다"
    assert r["calculation_version"] == CALCULATION_VERSION and r["inputs"]["regulation_version"].startswith("가상 일반상업")
    r2 = far_headroom(fixture("far_excluded"))
    assert r2["inputs"]["A_m2"] == 90 and r2["result"]["review_area_m2"] == 540 and r2["result"]["headroom_m2"] == 300
    assert any("재확인 필요" in n for n in r2["notes"]) and r2["unknown"] == []
    # 제약의 분모 제외 여부가 미확인이면 A 가 바뀔 수 있어 확정하지 않는다
    d = fixture("far_excluded")
    d["site"]["area_constraints"][1]["excluded_from_denominator"] = "unknown"
    r3 = far_headroom(d)
    assert r3["result"] is None and any("건축선 후퇴" in u for u in r3["unknown"])
    # 규제 버전이 없으면 적용 용적률을 확정할 수 없다
    d = fixture("far_gross")
    d["regulation"]["regulation_version"] = None
    r4 = far_headroom(d)
    assert r4["result"] is None and any("regulation_version" in u for u in r4["unknown"])


def test_far_unknown_zero_negative_and_inconsistent():
    d = fixture("far_gross")
    d["site"]["statutory"] = {"value_m2": None, "basis": "검토 전", "reason": "대지 구성·제외 근거 미확인"}
    r = far_headroom(d)
    assert r["result"] is None and r["unknown"] == ["statutory_site_area_m2: 대지 구성·제외 근거 미확인"], "미확인 입력이면 결과 없음 (배치 검토영역 60 을 대입하지 않는다)"
    d = fixture("far_gross")
    d["regulation"]["applied_far_pct"] = None
    d["regulation"]["reason"] = "지구단위계획 확인 전"
    assert far_headroom(d)["result"] is None
    d = fixture("far_gross")
    d["site"]["statutory"]["value_m2"] = 0
    r = far_headroom(d)
    assert r["result"] is None and any("분모 0" in e for e in r["errors"])
    d = fixture("far_gross")
    d["current_gfa"]["far_gfa"]["value_m2"] = 700
    d["current_gfa"]["total_gfa"]["value_m2"] = 800
    r = far_headroom(d)
    assert r["result"]["headroom_m2"] == -100 and any("음수" in n for n in r["notes"]), "음수 여유면적은 그대로 표시"
    d = fixture("far_gross")
    d["current_gfa"]["total_gfa"]["value_m2"] = 200
    assert any("총연면적" in e for e in far_headroom(d)["errors"]), "총연면적 < 산정용 연면적은 오류"
    d = fixture("far_excluded")
    d["site"]["statutory"]["value_m2"] = 95
    assert any("한쪽을 고친다" in e for e in far_headroom(d)["errors"])
    d = fixture("far_excluded")
    d["site"]["statutory_excluded"]["value_m2"] = None
    d["site"]["statutory_excluded"]["reason"] = "확인 전"
    assert far_headroom(d)["result"] is None


def test_equity_dictionary_cases():
    r = required_equity(fixture("equity_gross"))
    assert r["result_krw"] == 17 * EOK and r["components_krw"]["P"] == 40 * EOK and r["deposit_return_obligation_krw"] == 1 * EOK and r["unknown"] == []
    r = required_equity(fixture("equity_net"))
    assert r["result_krw"] == 17 * EOK and r["components_krw"]["P"] == 40 * EOK and r["remaining_cash_now_krw"] == 13 * EOK, "순액 39억 + 차감 1억 → P 40억 복원, 기지급 4억"
    r = required_equity(fixture("equity_unknown"))
    assert r["result_krw"] is None and len(r["unknown"]) == 2 and r["known_sum_krw"] == -(24 * EOK) - EOK + 16_000_000, "기준 미확인이면 완성 결과 미출력, 알려진 항목만"


def test_equity_normalization_rules():
    # 잔금만으로 총액을 복원하지 않는다
    P, unknown, _ = normalize_price({"price_input_basis": "settlement_residual", "price_input_krw": 20 * EOK})
    assert P is None and "잔금" in unknown[0]
    # 차감 후 입력인데 차감액 미확인 → 복원 불가
    P, unknown, _ = normalize_price({"price_input_basis": "net_after_deposit", "price_input_krw": 39 * EOK, "deposit_already_deducted_krw": None})
    assert P is None and "복원하지 못한다" in unknown[0]
    # 총액 입력에 차감액을 함께 주면 거절 (이미 뺀 값을 넣고 D 를 다시 빼는 입력 금지)
    with pytest.raises(ValidationError):
        normalize_price({"price_input_basis": "gross_contract", "price_input_krw": 39 * EOK, "deposit_already_deducted_krw": EOK})
    # 보증금 공제가 순대출 한도에 이미 들어 있으면 D 를 다시 차감하지 않는다 (반환 의무는 남는다)
    d = fixture("equity_gross")
    d["loan"]["deposit_netted_in_limit"] = True
    r = required_equity(d)
    assert r["result_krw"] == 18 * EOK and r["components_krw"]["D"] == 0 and r["deposit_return_obligation_krw"] == EOK
    # 가격에 포함된 비용은 다시 더하지 않는다
    d = fixture("equity_gross")
    d["costs"]["transaction_costs"]["included_in_price"] = True
    r = required_equity(d)
    assert r["result_krw"] == 17 * EOK - 16_000_000 and any("포함돼 0" in n for n in r["notes"])
    # 대출 미확인 → 결과 없음
    d = fixture("equity_gross")
    d["loan"] = {"amount_krw": None, "kind": "unknown", "reason": "은행 검토 전"}
    assert required_equity(d)["result_krw"] is None


def test_cash_dictionary_case_and_rules():
    r = max_required_equity(fixture("cash_basic"))
    assert [s["cumulative_krw"] for s in r["series"]] == [-17 * EOK, -20 * EOK, -18 * EOK] and r["min_at"] == 1
    assert r["result_krw"] == 21 * EOK and r["reserve_cash_krw"] == EOK
    # 예비현금이 CF 에 들어 있으면 마지막에 더하지 않는다
    d = fixture("cash_basic")
    d["flows"].append({"t": 0, "amount_krw": -EOK, "kind": "reserve", "note": "예비현금 적립"})
    d["reserve_cash_krw"] = None
    d["reserve_in_flows"] = True
    r = max_required_equity(d)
    assert r["result_krw"] == 21 * EOK and r["reserve_cash_krw"] is None
    # 이중 반영·소유자 유입·시점 혼용 거절
    for mutate, word in (
        (lambda x: x.update(reserve_in_flows=True, reserve_cash_krw=None), "kind=reserve"),
        (lambda x: x["flows"].append({"t": 0, "amount_krw": 5 * EOK, "kind": "owner_equity", "note": None}), "owner_equity"),
        (lambda x: x["flows"].append({"t": "2026-10-01", "amount_krw": 0, "kind": "other", "note": None}), "모두 기간 번호"),
    ):
        d = fixture("cash_basic")
        mutate(d)
        with pytest.raises(ValidationError) as e:
            max_required_equity(d)
        assert word in str(e.value)
    d = fixture("cash_basic")
    d["flows"].append({"t": 0, "amount_krw": -EOK, "kind": "reserve", "note": None})
    d["reserve_in_flows"] = True
    with pytest.raises(ValidationError) as e:
        max_required_equity(d)
    assert "이중 반영" in str(e.value)
    # 미확인 금액 → 알려진 항목만, 결과 없음. 예비현금 미확인도 마찬가지
    d = fixture("cash_basic")
    d["flows"][1]["amount_krw"] = None
    d["flows"][1]["reason"] = "공사 견적 전"
    r = max_required_equity(d)
    assert r["result_krw"] is None and r["known_sum_krw"] == 18 * EOK and "공사 견적 전" in r["unknown"][0]
    d = fixture("cash_basic")
    d["reserve_cash_krw"] = None
    assert max_required_equity(d)["result_krw"] is None
    # 날짜 시점도 된다 (정렬은 문자열 순)
    d = fixture("cash_basic")
    for i, f in enumerate(d["flows"]):
        f["t"] = f"2026-1{f['t']}-01"
    r = max_required_equity(d)
    assert r["result_krw"] == 21 * EOK and r["min_at"] == "2026-11-01"
    assert max(len(s["t"]) for s in r["series"]) == 10


def test_inputs_schema_and_cli(tmp_path, capsys):
    for name in ("far_gross", "far_excluded", "equity_gross", "equity_net", "equity_unknown", "cash_basic"):
        doc = load_calc_input(FIX / f"{name}.json")
        assert "결과" in calc_text(run_calc(doc))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**fixture("far_gross"), "kind": "nope"}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_calc_input(bad)
    with pytest.raises(ValidationError) as e:
        load_calc_input(FIX / "far_gross.json", expected_kind="cash")
    assert "kind" in str(e.value)
    d = copy.deepcopy(fixture("equity_gross"))
    d["costs"]["acquisition_tax"]["amount_krw"] = -1
    bad.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_calc_input(bad)
    # 미확인 금액에는 사유, 확인된 금액에는 근거가 필수. 날짜 시점은 YYYY-MM-DD 만
    for mutate in (
        lambda x: x["costs"]["acquisition_tax"].update(amount_krw=None, basis=None),
        lambda x: x["costs"]["transaction_costs"].pop("basis"),
        lambda x: x["loan"].update(amount_krw=None),
        lambda x: x["price"].update(price_input_krw=None),
    ):
        d = copy.deepcopy(fixture("equity_gross"))
        mutate(d)
        bad.write_text(json.dumps(d), encoding="utf-8")
        with pytest.raises(ValidationError):
            load_calc_input(bad)
    for mutate in (
        lambda x: x["flows"].__setitem__(0, {**x["flows"][0], "t": "2026-2-01"}),
        lambda x: x["flows"].__setitem__(0, {**x["flows"][0], "amount_krw": None}),
        lambda x: x["site"]["statutory"].update(value_m2=None, reason=None) if "site" in x else x["flows"].clear(),
    ):
        d = copy.deepcopy(fixture("cash_basic"))
        mutate(d)
        bad.write_text(json.dumps(d), encoding="utf-8")
        with pytest.raises(ValidationError):
            load_calc_input(bad)
    d = copy.deepcopy(fixture("far_gross"))
    d["site"]["statutory"].update(value_m2=None, reason=None)
    bad.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_calc_input(bad)
    assert cli.main(["calc", "far", str(FIX / "far_gross.json")]) == 0
    assert "여유면적 360㎡" in capsys.readouterr().out
    assert cli.main(["calc", "equity", str(FIX / "equity_net.json"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result_krw"] == 17 * EOK
    assert cli.main(["calc", "cash", str(FIX / "cash_basic.json")]) == 0
    assert "결과: 2,100,000,000원" in capsys.readouterr().out
    assert cli.main(["calc", "equity", str(FIX / "equity_unknown.json")]) == 1, "미확정 결과는 종료 코드 1"
    assert "미확정" in capsys.readouterr().out
    assert cli.main(["calc", "far", str(FIX / "cash_basic.json")]) == cli.USAGE_ERROR
    assert cli.main(["calc", "far", str(tmp_path / "none.json")]) == cli.USAGE_ERROR
    assert cli.main(["calc", "cash", str(bad)]) == cli.USAGE_ERROR
