"""J5-016B: 개발안 비교와 후보별 현금표·CSV (R5). 데이터 사전 §9 후단·§10, 릴리스 계획 §8.

시험: 후보 4종(현상 유지·리모델링·철거신축·공동매입)의 필요자기자본·최대 현금 나란히 계산, 공사기간 공실 미반영·부가세 미확인·예비현금 이중 반영·
승계 보증금 반환 미반영·감정가 차이·공동매입 필수 항목·건축사 검토 경고, 미확인 입력이 있는 후보의 미확정, CSV(덮어쓰지 않음), CLI. 가상자료만 쓴다.
"""

from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import Path

import pytest

from j5 import cli
from j5.calc.inputs import calc_text, load_calc_input, run_calc
from j5.calc.plans import CSV_COLUMNS, compare_plans, plans_csv, plans_text
from j5.db.validate import ValidationError

FIX = Path(__file__).resolve().parent / "fixtures" / "calc"
E = 100_000_000


def fixture() -> dict:
    return json.loads((FIX / "plans_compare.json").read_text(encoding="utf-8"))


def plan(r: dict, pid: str) -> dict:
    return next(p for p in r["plans"] if p["plan_id"] == pid)


def test_compare_four_plans_side_by_side():
    r = compare_plans(load_calc_input(FIX / "plans_compare.json", expected_kind="plans"))
    assert [p["plan_id"] for p in r["plans"]] == ["keep", "remodel", "rebuild", "joint"] and r["resolved"] is True
    keep, remodel, rebuild, joint = (plan(r, k) for k in ("keep", "remodel", "rebuild", "joint"))
    assert keep["equity"]["result_krw"] == 17 * E and keep["cash"]["result_krw"] == 18 * E
    assert remodel["cash"]["result_krw"] == 25 * E and remodel["cash"]["min_at"] == 2, "공사 2개월 동안 임대 0 + 공사비로 누적 최저 -24억 + 예비 1억"
    assert rebuild["cash"]["result_krw"] == 33 * E and rebuild["construction_months"] == 3
    assert joint["equity"]["result_krw"] == 2_255_200_000 and joint["joint"]["acquisition_order"] == ["본 필지", "인접 필지"]
    # 감정가 36억 × LTV 60% = 21.6억 < 매입가 가정 대출 24억 → 2.4억 더 필요
    assert keep["appraisal"]["loan_gap_krw"] == 240_000_000 and keep["appraisal"]["appraisal_based_equity_krw"] == 1_940_000_000
    assert any("감정가" in w for w in keep["warnings"]) and remodel["warnings"] == [] and any("건축사" in w for w in rebuild["warnings"]) and joint["warnings"] == []
    text = plans_text(r)
    assert "[현상 유지] keep" in text and "감정가 기준 1,940,000,000원" in text and "취득 순서 본 필지 → 인접 필지" in text and "고르지 않는다" in text


def test_plan_warnings_and_unresolved():
    d = fixture()
    # 공사기간 공실 미반영: 공사 중 임대가 들어오는데 vacancy 항목이 없다
    rm = next(p for p in d["plans"] if p["plan_id"] == "remodel")
    rm["cash"]["flows"] = [f for f in rm["cash"]["flows"] if f["kind"] != "vacancy"] + [{"t": 1, "amount_krw": 2 * E, "kind": "rent", "note": "공사 중에도 임대?"}]
    rm["construction"]["vacancy_months"] = None
    rm["equity"]["price"]["vat_included"] = None
    rm["equity"]["costs"]["transaction_costs"]["vat_included"] = None
    rm["equity"]["costs"]["reserve_cash"]["amount_krw"] = E
    rm["equity"]["costs"]["initial_repairs"]["included_in"] = "nope"
    # 승계 보증금 반환 미반영
    kp = next(p for p in d["plans"] if p["plan_id"] == "keep")
    kp["cash"]["flows"] = [f for f in kp["cash"]["flows"] if f["kind"] != "deposit_return"]
    kp["appraisal"] = {"amount_krw": 45 * E, "basis": "가상 감정가 (매입가보다 높음)"}
    # 공동매입 추가 가격·기간 미확인
    jt = next(p for p in d["plans"] if p["plan_id"] == "joint")
    jt["joint"]["extra_price"] = {"amount_krw": None, "reason": "호가 미확인"}
    jt["joint"]["period_months"] = None
    # 철거신축: 공사기간 미확인 → 경고, 공사비 미확인 → 후보 미확정
    rb = next(p for p in d["plans"] if p["plan_id"] == "rebuild")
    rb["construction"] = {"months": None, "reason": "공정표 전"}
    rb["cash"]["flows"][2]["amount_krw"] = None
    rb["cash"]["flows"][2]["reason"] = "설계비 견적 전"
    r = compare_plans(d)
    w_rm = "\n".join(plan(r, "remodel")["warnings"])
    assert "공실(vacancy)이 현금흐름에 없다" in w_rm and "vacancy_months" in w_rm and "가격의 부가세" in w_rm and "transaction_costs 의 부가세" in w_rm
    assert "이중 반영" in w_rm and "'nope'" in w_rm
    w_kp = "\n".join(plan(r, "keep")["warnings"])
    assert "반환 의무 100,000,000 이 현금흐름(deposit_return)에 없다" in w_kp and "감정가" not in w_kp, "감정가가 매입가 이상이면 자기자본이 늘지 않는다 (경고 없음)"
    assert plan(r, "keep")["appraisal"]["loan_gap_krw"] == int(24 * E - 45 * E * 0.6)
    w_jt = "\n".join(plan(r, "joint")["warnings"])
    assert "추가 가격 미확인: 호가 미확인" in w_jt and "취득 기간 미확인" in w_jt
    rbp = plan(r, "rebuild")
    assert rbp["resolved"] is False and rbp["cash"]["result_krw"] is None and any("설계비 견적 전" in u for u in rbp["unknown"]) and any("공사기간 미확인" in w for w in rbp["warnings"])
    assert r["resolved"] is False
    rows = {row["plan_id"]: row for row in csv.DictReader(io.StringIO(plans_csv(r)))}
    assert rows["rebuild"]["max_required_equity_krw"] == "" and rows["rebuild"]["unknown_count"] == "1" and "공정표 전" in rows["rebuild"]["warnings"]
    assert list(rows["keep"].keys()) == list(CSV_COLUMNS)


def test_plans_input_validation(tmp_path):
    bad = tmp_path / "bad.json"
    for mutate, word in (
        (lambda x: x["plans"].__setitem__(1, {**x["plans"][1], "plan_id": "keep"}), "겹친다"),
    ):
        d = fixture()
        mutate(d)
        with pytest.raises(ValidationError) as e:
            compare_plans(d)
        assert word in str(e.value)
    for mutate in (
        lambda x: x["plans"][1].pop("construction"),          # remodel 은 construction 필수
        lambda x: x["plans"][3].pop("joint"),                 # joint_purchase 는 joint 필수
        lambda x: x["plans"][3]["joint"].pop("partial_fallback"),
        lambda x: x["plans"][0]["equity"].update(kind="cash"),
        lambda x: x["plans"][0].update(plan_id="Keep Plan"),
        lambda x: x["plans"][1]["construction"].update(months=None),  # 미확인이면 reason 필수
    ):
        d = fixture()
        mutate(d)
        bad.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValidationError):
            load_calc_input(bad)


def test_cli_plans_and_csv(tmp_path, capsys):
    out = tmp_path / "plans.csv"
    assert cli.main(["calc", "plans", str(FIX / "plans_compare.json"), "--csv", str(out)]) == 0
    text = capsys.readouterr().out
    assert "개발안 비교" in text and f"CSV: {out}" in text and out.is_file()
    rows = list(csv.DictReader(out.open(encoding="utf-8", newline="")))
    assert [r["plan_id"] for r in rows] == ["keep", "remodel", "rebuild", "joint"] and rows[0]["required_equity_krw"] == str(17 * E) and rows[2]["max_required_equity_krw"] == str(33 * E)
    assert cli.main(["calc", "plans", str(FIX / "plans_compare.json"), "--csv", str(out)]) == cli.USAGE_ERROR, "덮어쓰지 않는다"
    assert "덮어쓰지 않는다" in capsys.readouterr().err
    assert cli.main(["calc", "plans", str(FIX / "plans_compare.json"), "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["kind"] == "plans" and j["resolved"] is True and len(j["plans"]) == 4
    d = fixture()
    d["plans"][0]["equity"]["loan"] = {"amount_krw": None, "kind": "unknown", "reason": "은행 검토 전"}
    f = tmp_path / "unres.json"
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["calc", "plans", str(f)]) == 1, "미확정 후보가 있으면 1"
    assert "미확인: 대출 L 미확인: 은행 검토 전" in capsys.readouterr().out
    assert cli.main(["calc", "equity", str(FIX / "plans_compare.json")]) == cli.USAGE_ERROR
    assert "개발안 비교" in calc_text(run_calc(load_calc_input(FIX / "plans_compare.json")))
