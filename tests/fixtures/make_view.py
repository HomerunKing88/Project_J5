"""가상 조회 파생본(.j5view.zip) 생성기 (J5-023). 폰 앱의 "PC 자료 파일 가져오기" 와 이력 화면 시험용.

임시 실데이터 홈에 가상 정본을 만들고(가상 시드 5개 → 관측 패키지 반영 → 가상 필지 6개 반영·위치점 연결 → 가상 VWorld 속성 번들 반영과 나중 기준일의
공시지가·소유 변경(J5-026 속성 이력) → 가상 실거래 2개년(로컬 가짜 서버) 반영·범위 규칙·거래 1건 확정 연결 → 목표 매수가 기록 → 관심 단계 4건(J5-029)) `j5 db project` 로 파생본을 만들어 지정한 폴더에 복사한다. 생성 시각·run_id 가 들어가므로 바이트가
매번 다르다(저장소에 넣지 않고 시험 때 만든다). 가상자료만 쓰며 외부 통신은 없다(가짜 서버는 127.0.0.1).

    python tests/fixtures/make_view.py <출력 폴더> [--no-parcels]   → <출력 폴더>/synthetic.j5view.zip (--no-parcels 면 synthetic-noparcels.j5view.zip, 필지·연결 없음) 과 요약 JSON
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(REPO))

from j5.collect import rt  # noqa: E402
from j5.collect.rt import collect_months  # noqa: E402
from j5.db.importer import import_package  # noqa: E402
from j5.db.judgment import apply_record_input  # noqa: E402
from j5.db.parcels import apply_links, load_bundle, suggest_links  # noqa: E402
from j5.db.projection import build_projection  # noqa: E402
from j5.db.store import Db  # noqa: E402
from j5.db.tracking import set_status  # noqa: E402
from j5.db.transactions import load_run  # noqa: E402
from j5.db.txlinks import apply_decisions, candidates  # noqa: E402
from j5.db.zones import apply_rules, load_rules  # noqa: E402
from tests.test_j5_014_collect import KEY, _Handler, item  # noqa: E402

STUDY = "j5-synthetic-study"
SEED = ROOT / "assets.seed.synthetic.json"
PACKAGE = ROOT / "packages" / "valid"
PARCELS = ROOT / "parcels" / "synthetic.j5parcels.json"
PARCELS_VW = ROOT / "parcels" / "synthetic_vworld.j5parcels.json"
P1 = "9999900100100010000"
A = [f"7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a5{i}" for i in range(5)]
RULES = {"kind": "zone_rules", "name": "가상 범위", "core": ["가상동"], "comparison": ["가상2동"], "note": None}


def _row(i: int, ym: str, **over) -> dict:
    # 시군구는 가상 필지(PNU 앞 5자리)와 같은 99999: 지번 대조는 시군구 코드도 본다 (J5-044)
    it = item(i)
    it.update({"sggCd": "99999", "landUse": "일반상업", "shareDealingType": "", "cdealDay": "", "estateAgentSggNm": "", "buyerGbn": "", "slerGbn": "",
               "umdNm": "가상동", "dealYear": ym[:4], "dealMonth": str(int(ym[4:]))})
    it.update(over)
    return it


# 계약월별 가상 거래: 필지 1 (지번 1) 확정 연결 1건, 1-1, 1-* (마스킹, 번지대), * (동 전체 마스킹), 4-2, 비교 동
MONTHS = {
    "202508": [_row(0, "202508", jibun="1"), _row(1, "202508", jibun="1-*"), _row(2, "202508", jibun="9", umdNm="가상2동")],
    "202608": [_row(3, "202608", jibun="1-1"), _row(4, "202608", jibun="*"), _row(5, "202608", jibun="4-2"), _row(6, "202608", jibun="1", cdealType="O", cdealDay="26.08.30")],
}


FIXED_NOW = "2026-09-30T09:00:00Z"


def build(out_dir: Path, *, parcels: bool = True, inspect=None, extra=None) -> dict:
    """inspect(db, home, proj) 를 주면 정본을 닫기 전에, extra(db, home) 를 주면 기본 자료를 모두 넣은 뒤·파생본 생성 전에 부른다
    (폰·PC 대조 시험이 자기 자료를 더한다, J5-058). 둘 다 없으면 파생본 내용은 이전과 같다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="j5view-fixture-") as td:
        home = Path(td) / "home"
        home.mkdir()
        db = Db.create(home / "db" / "j5.sqlite3", study_id=STUDY, data_mode="synthetic")
        db.now = lambda: FIXED_NOW   # 파생본 생성 시각을 고정한다: 폰 연도별 요약의 끝 연도가 이 시각의 연도라 실제 시계를 따르면 해가 바뀔 때 시험이 바뀐다 (J5-053 리뷰)
        db.load_seed(json.loads(SEED.read_text(encoding="utf-8")))
        r = import_package(db, PACKAGE, home)
        assert r.outcome == "applied", r
        if parcels:
            load_bundle(db, json.loads(PARCELS.read_text(encoding="utf-8")))
            sug = suggest_links(db, effective_from="2026-09-23")
            apply_links(db, {k: v for k, v in sug.items() if not k.startswith("_")})
            # 필지 속성(J5-025)과 이력(J5-026): 가상 VWorld 번들(기준일 2026-09-05) 뒤에 기준일 2026-09-25 의 변형(필지 1 공시지가·소유 변경)을 넣는다
            vw = json.loads(PARCELS_VW.read_text(encoding="utf-8"))
            load_bundle(db, vw)
            vw["source"]["geometry_version"] = "2026-09-25"
            a1 = next(f for f in vw["features"] if f["id"] == P1)["properties"]["attrs"]
            a1.update({"official_land_price_krw_m2": 13_000_000, "ownership_kind_code": "06", "ownership_kind": "법인", "ownership_changed_on": "2026-09-24", "ownership_change_cause_code": "04"})
            load_bundle(db, vw)
        # 가짜 실거래 서버 (127.0.0.1) 로 2개년 수집 → 반영
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            endpoint = f"http://127.0.0.1:{srv.server_address[1]}/api"
            counter = iter(range(1, 100))
            rt._new_run_id = lambda: f"20260927-090000-{next(counter):06x}"
            _Handler.scenarios = {ym: {"kind": "pages", "items": items} for ym, items in MONTHS.items()}
            run = collect_months(home, key=KEY, key_source="fixture", lawd_cd="99999", months=sorted(MONTHS), endpoint=endpoint, sleep=lambda s: None)
            load_run(db, home, "99999", run.run_id)
        finally:
            srv.shutdown()
        rules = Path(td) / "zones.json"
        rules.write_text(json.dumps(RULES, ensure_ascii=False), encoding="utf-8")
        apply_rules(db, load_rules(rules))
        rows = candidates(db, "99999", sorted(MONTHS))
        for row in rows:
            row["_line"] = 2
            if row["jibun"] == "1" and row["deal_ymd"] == "202508":
                row.update({"decision": "confirmed", "asset_id": A[0], "basis_kind": "jibun_exact", "reviewed_on": "2026-09-25", "note": "지번 일치 (가상)"})
        apply_decisions(db, rows)
        apply_record_input(db, {"kind": "record", "asset_id": A[0], "record_type": "target_price", "source_kind": "personal_estimate", "observed_at": "2026-09-20", "supersedes_id": None,
                                "evidence": [], "payload": {"price_kind": "target_buy", "price_krw": 1_500_000_000, "decided_on": "2026-09-20", "strategy": "보유 후 리모델링 (가상)",
                                                            "composition_as_of": "2026-09-20", "assumptions": ["가상 가정"], "comparison_basis": [{"kind": "personal_estimate", "ref_id": None, "note": "가상 추정"}],
                                                            "valid_conditions": ["가상 조건"], "revision_reason": None}})
        # 관심 단계 (J5-029): 관찰 2개(물건 1·2, 관찰목록), 보류 1개(물건 4), 제외 1개(물건 5), 물건 3(주소만)은 미검토 그대로
        set_status(db, A[0], "watch", reason="가상 관찰", changed_on="2026-09-21")
        set_status(db, A[1], "detailed_review", reason="가상 상세 검토", changed_on="2026-09-22")
        set_status(db, A[3], "hold", reason="가상 보류", changed_on="2026-09-23")
        set_status(db, A[4], "excluded", reason="가상 제외", changed_on="2026-09-24")
        if extra:
            extra(db, home)
        proj = build_projection(db, home, photos=False)
        inspected = inspect(db, home, proj) if inspect else None
        db.close()
        src = home / proj.output_dir / proj.zip_name
        dst = out_dir / ("synthetic.j5view.zip" if parcels else "synthetic-noparcels.j5view.zip")
        shutil.copyfile(src, dst)
        return {"zip": str(dst), "bytes": dst.stat().st_size, "counts": proj.counts, "source_dataset_version": proj.source_dataset_version, **({"inspect": inspected} if inspect else {})}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = Path(args[0] if args else ROOT / "view")
    print(json.dumps(build(out, parcels="--no-parcels" not in sys.argv), ensure_ascii=False))


if __name__ == "__main__":
    main()
