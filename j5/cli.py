"""j5 명령줄. inspect(패키지 검사), copy(독립 사본), db(정본 SQLite: init/status/load-seed/import/project/backup/restore/check-photos/survey-*),
parcels(연속지적도 SHP → 필지 번들: inspect/convert), collect(공식 API 수집: rt-sample/rt-report).

종료 코드: 0 ok·반영·중복 / 1 reject·실패 / 2 hold·보류 / 3 사용 오류.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from j5 import APP_VERSION
from j5.db.backup import EXIT_BY_OUTCOME as BACKUP_EXIT, BackupError, check_photos, check_photos_text, create_backup, restore_backup, verify_backup_dir
from j5.db.importer import EXIT_BY_OUTCOME, import_package
from j5.db.parcels import BUNDLE_SCHEMA as PARCELS_BUNDLE_SCHEMA, LINKS_SCHEMA as PARCELS_LINKS_SCHEMA, apply_links, load_bundle, load_json as load_parcels_json, suggest_links
from j5.db.projection import EXIT_BY_OUTCOME as PROJECT_EXIT, ProjectionError, build_projection, copy_latest
from j5.db.store import Db, DbError, default_db_path
from j5.db.survey import apply_input, compare, compare_text, load_input, overview, overview_text, vacancy, vacancy_text
from j5.db.validate import ValidationError
from j5.package.preserve import PreserveError, copy_package
from j5.package.validate import inspect_package
from j5.collect.config import ConfigError, config_permission_warning, load_config, redact, service_key
from j5.collect.rt import DEFAULT_ENDPOINT, DEFAULT_MAX_PAGES, DEFAULT_NUM_ROWS, CollectError, check_lawd, collect_months, month_range, months_done_on_disk, parse_months, recent_months, report_from_raw, report_text, run_text
from j5.db.transactions import coverage, coverage_text, load_run, unloaded_run_ids
from j5.db.txlinks import apply_decisions, candidates, candidates_csv, candidates_text, read_decisions_csv
from j5.db.zones import apply_rules, changes_since, changes_text, current_rules, load_rules, rules_text, zone_counts
from j5.db.judgment import apply_record_input, asof, asof_text, load_record_input
from j5.db.photos import export_series, photo_series, photo_tags, series_text
from j5.db.recheck import apply_recheck_input, load_recheck_input, recheck_add_text, recheck_status, recheck_text
from j5.calc.inputs import calc_text, load_calc_input, run_calc
from j5.parcels.convert import Clip, ConvertError, ConvertOptions, convert, convert_text, inspect_source, inspect_text, write_bundle
from j5.schemas_loader import schema_errors

EXIT = {"ok": 0, "reject": 1, "hold": 2}
USAGE_ERROR = 3


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="j5", description="종로5가 투자지도 PC 도구")
    p.add_argument("--version", action="version", version=f"j5 {APP_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("inspect", help="관측 패키지(.j5field.zip 또는 풀어 놓은 폴더) 검사")
    i.add_argument("package", type=Path)
    i.add_argument("--seed", type=Path, help="assets.seed.json. 지정하면 asset_id 연결을 검사")
    i.add_argument("--study-id", help="기대하는 study_id. 생략 시 환경변수 J5_STUDY_ID")
    i.add_argument("--json", action="store_true", help="JSON으로 출력")

    c = sub.add_parser("copy", help="패키지 ZIP의 독립 사본을 만들고 해시 파일을 남김")
    c.add_argument("package", type=Path)
    c.add_argument("dest_dir", type=Path)

    d = sub.add_parser("db", help="정본 SQLite (R1b). 경로는 --db 또는 J5_DATA_HOME/db/j5.sqlite3")
    d.add_argument("--db", type=Path, help="정본 파일 경로. 생략 시 J5_DATA_HOME/db/j5.sqlite3")
    dsub = d.add_subparsers(dest="db_command", required=True)
    di = dsub.add_parser("init", help="빈 정본을 만든다 (기존 파일은 덮어쓰지 않음)")
    di.add_argument("--study-id", help="생략 시 J5_STUDY_ID")
    di.add_argument("--data-mode", choices=["synthetic", "private_real"], help="생략 시 J5_DATA_MODE")
    ds = dsub.add_parser("status", help="스키마 버전·study_id·dataset_version·행 수·무결성·외래키 검사·파생본 상태")
    ds.add_argument("--data-home", type=Path, help="파생본 포인터·파일을 실제로 확인할 실데이터 홈. 생략 시 J5_DATA_HOME (없으면 파일 미확인으로 표시)")
    ds.add_argument("--json", action="store_true")
    dl = dsub.add_parser("load-seed", help="assets.seed.json 의 물건을 asset_id 그대로 승계해 반영한다")
    dl.add_argument("seed", type=Path)
    dm = dsub.add_parser("import", help="관측 패키지(.j5field.zip 또는 폴더)를 정본에 반영한다. 사진은 J5_DATA_HOME/photos 에 보관")
    dm.add_argument("package", type=Path)
    dm.add_argument("--data-home", type=Path, help="사진·로그를 둘 실데이터 홈. 생략 시 J5_DATA_HOME")
    dm.add_argument("--json", action="store_true")
    dp = dsub.add_parser("project", help="조회 파생본(.j5view.zip)을 정본에서 전량 생성·검증·게시한다. 실패 시 이전 파생본 유지")
    dp.add_argument("--data-home", type=Path, help="파생본을 둘 실데이터 홈(exports/private/projections). 생략 시 J5_DATA_HOME")
    dp.add_argument("--photos", action="store_true", help="기록이 참조한 사진을 파생본에 포함한다 (기본은 제외)")
    dp.add_argument("--json", action="store_true")
    dc = dsub.add_parser("project-copy", help="게시된 최신 파생본 ZIP 의 독립 사본. 정본보다 오래된 구본은 --allow-stale 없이는 막는다")
    dc.add_argument("dest_dir", type=Path)
    dc.add_argument("--data-home", type=Path)
    dc.add_argument("--allow-stale", action="store_true", help="구본 보존용 내보내기. 결과에 구버전임을 표시한다")
    db_ = dsub.add_parser("backup", help="일관된 정본 사본 + 참조 사진 + 해시 목록을 만들고 다시 읽어 검증한다 (J5_DATA_HOME/backups 또는 --dest)")
    db_.add_argument("--data-home", type=Path, help="정본·사진이 있는 실데이터 홈. 생략 시 J5_DATA_HOME")
    db_.add_argument("--dest", type=Path, help="백업을 둘 상위 폴더 (예: 외장 드라이브). 생략 시 J5_DATA_HOME/backups")
    db_.add_argument("--json", action="store_true")
    dv = dsub.add_parser("backup-verify", help="백업 폴더를 전부 다시 읽어 검증한다 (정본 불필요)")
    dv.add_argument("backup_dir", type=Path)
    dv.add_argument("--json", action="store_true")
    dr = dsub.add_parser("restore", help="백업을 빈 폴더에 복구하고 기록 수·해시·사진 연결을 확인한다 (기존 정본은 덮어쓰지 않음)")
    dr.add_argument("backup_dir", type=Path)
    dr.add_argument("dest_home", type=Path, help="비어 있거나 없는 폴더. 복구 후 J5_DATA_HOME 으로 쓴다")
    dr.add_argument("--json", action="store_true")
    dk = dsub.add_parser("check-photos", help="사진 대사: 정본이 참조한 사진의 존재·해시와 미참조 파일을 보고한다 (삭제 없음)")
    dk.add_argument("--data-home", type=Path)
    dk.add_argument("--json", action="store_true")
    sa = dsub.add_parser("survey-apply", help="조사 입력 파일(route_version / units / frame_version / session, schemas/survey_input.schema.json)을 정본에 반영한다")
    sa.add_argument("file", type=Path)
    sa.add_argument("--json", action="store_true")
    sv = dsub.add_parser("survey-vacancy", help="세션의 공실 집계 (N·K·V, 미확인·미조사 수, 관측표본 공실비율)")
    sv.add_argument("session_id")
    sv.add_argument("--json", action="store_true")
    sc = dsub.add_parser("survey-compare", help="두 세션의 공통 표본 시점 비교 (분할·통합은 비교 단절)")
    sc.add_argument("session_a")
    sc.add_argument("session_b")
    sc.add_argument("--json", action="store_true")
    so = dsub.add_parser("survey-overview", help="경로·표본틀·세션·점포 현황")
    so.add_argument("--json", action="store_true")
    pl = dsub.add_parser("parcels-load", help="필지 번들(.j5parcels.json, j5 parcels convert 출력)을 정본 parcels 에 반영한다 (PNU 기준, 새 도형 기준일이면 갱신)")
    pl.add_argument("bundle", type=Path)
    pl.add_argument("--json", action="store_true")
    ps_ = dsub.add_parser("parcels-suggest", help="위치점을 품는 필지를 물건마다 찾아 연결 제안 파일을 만든다 (정본에 쓰지 않음, 검토 후 parcels-link)")
    ps_.add_argument("--out", type=Path, help="제안을 쓸 JSON 파일 (덮어쓰지 않음). 생략하면 표준 출력")
    ps_.add_argument("--effective-from", help="연결 시작일 YYYY-MM-DD (기본 오늘, UTC)")
    pk = dsub.add_parser("parcels-link", help="검토한 연결 파일(asset_components_input)을 정본 asset_components 에 반영한다")
    rl = dsub.add_parser("rt-load", help="실거래 수집 실행 기록(run-*.json)과 원본 XML 을 정본에 반영한다 (collection_runs / transaction_observations / transactions, db_schema 6). 같은 실행은 다시 반영하지 않는다")
    rl.add_argument("--lawd-cd", required=True, help="시군구 코드 5자리")
    rl.add_argument("--run-id", help="특정 실행만. 생략 시 정본에 없는 실행을 오래된 순으로 모두 반영")
    rl.add_argument("--data-home", type=Path, help="원본·기록이 있는 실데이터 홈. 생략 시 J5_DATA_HOME")
    rl.add_argument("--json", action="store_true")
    rc = dsub.add_parser("rt-coverage", help="계약월 범위의 수집 현황(월별 완전/부분/실패/없음, 거래·취소·사라짐·연결 수). 수집 완료와 연결 완료를 따로 센다")
    rc.add_argument("--lawd-cd", required=True)
    rc.add_argument("--from", dest="from_ym", required=True, help="시작 계약월 YYYY-MM")
    rc.add_argument("--to", dest="to_ym", required=True, help="끝 계약월 YYYY-MM (포함)")
    rc.add_argument("--json", action="store_true")
    rk = dsub.add_parser("rt-candidates", help="범위 안의 현재 거래(취소 확정 제외)를 후보 물건(연결된 필지의 법정동·지번 대조)과 함께 CSV 로 낸다. 정본에 쓰지 않는다. 결정 열을 채워 rt-link 로 반영")
    rk.add_argument("--lawd-cd", required=True)
    rk.add_argument("--from", dest="from_ym", required=True, help="시작 계약월 YYYY-MM")
    rk.add_argument("--to", dest="to_ym", required=True, help="끝 계약월 YYYY-MM (포함)")
    rk.add_argument("--emd", help="법정동 이름 목록 (쉼표 구분, 예: 종로5가,종로6가). 생략 시 전체")
    rk.add_argument("--unlinked-only", action="store_true", help="유효한 연결이 없는 거래만")
    rk.add_argument("--out", type=Path, help="CSV 출력 경로 (덮어쓰지 않음). 생략 시 표준 출력")
    rk.add_argument("--json", action="store_true")
    rn = dsub.add_parser("rt-link", help="결정 열(decision·asset_id·decision_scope·basis_kind·reviewed_on·note)을 채운 후보 CSV 를 정본 transaction_links 에 반영한다 (검토 결정 이력 보존)")
    rn.add_argument("file", type=Path, help="rt-candidates 형식의 CSV")
    rn.add_argument("--json", action="store_true")
    rz = dsub.add_parser("rt-zones", help="핵심·비교 범위 규칙(법정동 목록, schemas/zone_rules.schema.json): show / apply <규칙.json>. 반영하면 모든 거래를 다시 분류한다")
    rz.add_argument("action", choices=["show", "apply"])
    rz.add_argument("file", type=Path, nargs="?", help="apply 때 규칙 파일")
    rz.add_argument("--json", action="store_true")
    ra = dsub.add_parser("record-add", help="목표 매수가·투자판단 기록을 넣는다 (schemas/judgment_records.schema.json, 불변 기록, 수정은 supersedes_id 로 새 기록)")
    ra.add_argument("file", type=Path, help="입력 JSON (kind: record)")
    ra.add_argument("--json", action="store_true")
    ao = dsub.add_parser("asof", help="물건의 시점 T 상태를 재구성한다. --known-by K 를 주면 정본 기록일이 K 이하인 근거·검토결정만 쓴다(당시 기록 기준). --as-recorded 는 K=T")
    ao.add_argument("--asset", required=True, help="asset_id")
    ao.add_argument("--at", required=True, help="시점 T (YYYY-MM-DD)")
    ao.add_argument("--known-by", help="당시 기록 기준일 K (YYYY-MM-DD)")
    ao.add_argument("--as-recorded", action="store_true", help="K=T 로 당시 기록 기준 보기")
    ao.add_argument("--json", action="store_true")
    ps = dsub.add_parser("photo-series", help="물건의 사진을 촬영 지점(viewpoint_id) → 이전 사진 사슬 → 태그 순으로 시계열 묶음을 만들고 연차 비교 쌍을 낸다. --export 는 묶음별 폴더로 해시 검증 복사(exports/private/photo_series, 덮어쓰지 않음)")
    ps.add_argument("--asset", required=True, help="asset_id")
    ps.add_argument("--tag", help="이 태그가 붙은 사진만 (front, ground_floor, lease_ad, construction, road, parking, adjacency)")
    ps.add_argument("--export", action="store_true", help="묶음별 폴더로 사진을 복사하고 index.json 을 쓴다 (실데이터 홈 안, 저장소에 넣지 않는다)")
    ps.add_argument("--data-home", type=Path, help="사진이 있는 실데이터 홈. 생략 시 J5_DATA_HOME")
    ps.add_argument("--json", action="store_true")
    rc = dsub.add_parser("recheck", help="현재 목표 매수가·투자판단 기록의 재확인 현황: 조건 목록, 판단 뒤 정본에 들어온 새 정보(관측·연결 결정·취소·비교 근거 변동·범위 규칙·구성 변경·반박 근거), 마지막 재확인")
    rc.add_argument("--asset", required=True, help="asset_id")
    rc.add_argument("--json", action="store_true")
    rca = dsub.add_parser("recheck-add", help="재확인 기록을 넣는다 (schemas/judgment_recheck.schema.json, kind: recheck, 불변). 조건 대조·반대 증거를 남기고 그 시점의 신호 요약을 저장한다. 판단 자체는 바꾸지 않는다")
    rca.add_argument("file", type=Path, help="입력 JSON (kind: recheck)")
    rca.add_argument("--json", action="store_true")
    rg = dsub.add_parser("rt-changes", help="어떤 실행 이후의 변경: 새 거래·취소로 바뀜·응답에서 사라짐 (취소·정정 점검 결과 읽기)")
    rg.add_argument("--lawd-cd", required=True)
    rg.add_argument("--since-run", required=True, help="기준 실행 ID (이 실행 뒤의 실행들을 본다)")
    rg.add_argument("--zone", action="append", choices=["core", "comparison", "outside", "unclassified"], help="범위 필터 (반복 가능)")
    rg.add_argument("--json", action="store_true")
    pk.add_argument("file", type=Path)
    pk.add_argument("--json", action="store_true")

    pa = sub.add_parser("parcels", help="필지 경계·지번 (ADR-13): 연속지적도 SHP → 폰 지도용 번들(.j5parcels.json)")
    psub = pa.add_subparsers(dest="parcels_command", required=True)
    pi = psub.add_parser("inspect", help="SHP(.shp 또는 ZIP)의 필드·레코드 수·좌표계·WGS84 범위·표본 레코드를 보여준다 (변환 전 확인)")
    pi.add_argument("source", type=Path)
    pi.add_argument("--crs", help=".prj 가 없거나 못 읽을 때 EPSG:5186 형식으로 지정")
    pi.add_argument("--encoding", help=".dbf 문자 인코딩 (기본 .cpg 또는 cp949)")
    pi.add_argument("--layer", help="ZIP 안에 .shp 가 여럿일 때 기본 이름")
    pi.add_argument("--json", action="store_true")
    pc = psub.add_parser("convert", help="조사 범위의 필지만 WGS84 GeoJSON 번들로 만든다 (원본은 수정하지 않음, 출력은 덮어쓰지 않음)")
    pc.add_argument("source", type=Path)
    pc.add_argument("--out", type=Path, required=True, help="출력 파일 (<이름>.j5parcels.json). 실데이터 홈 안에 둔다")
    pc.add_argument("--geometry-version", required=True, help="도형 기준일 YYYY-MM-DD (배포 자료의 기준 시점)")
    pc.add_argument("--source-name", required=True, help="자료명 (예: '연속지적도 서울특별시 종로구')")
    pc.add_argument("--bbox", help="WGS84 minlon,minlat,maxlon,maxlat")
    pc.add_argument("--center", help="WGS84 lon,lat (--radius-m 과 함께)")
    pc.add_argument("--radius-m", type=float, help="중심에서의 반경(m)")
    pc.add_argument("--crs", help=".prj 가 없거나 못 읽을 때 EPSG:5186 형식으로 지정")
    pc.add_argument("--encoding", help=".dbf 문자 인코딩 (기본 .cpg 또는 cp949)")
    pc.add_argument("--layer", help="ZIP 안에 .shp 가 여럿일 때 기본 이름")
    pc.add_argument("--license", help="이용허락 유형·출처 표시 문구 (확인한 값만)")
    pc.add_argument("--emd-name", action="append", default=[], metavar="CODE=이름", help="법정동 코드(10자리)→이름. 반복 가능")
    pc.add_argument("--pnu-field", help="PNU 필드 이름 (기본 PNU)")
    pc.add_argument("--jibun-field", help="지번 필드 이름 (기본 JIBUN)")
    pc.add_argument("--max-features", type=int, default=8000, help="범위 안 필지 상한 (기본이자 최대 8000, 번들 계약과 같다)")
    pc.add_argument("--synthetic", action="store_true", help="가상자료 표시 (data_mode synthetic)")
    pc.add_argument("--json", action="store_true")

    ca = sub.add_parser("calc", help="계산기 (R5, J5-016, 데이터 사전 §9~§10): 용적률 기준 여유면적 / 매입 전체 필요자기자본 / 사업기간 최대 필요자기자본. 입력은 schemas/calc_inputs.schema.json")
    casub = ca.add_subparsers(dest="calc_command")
    for name, help_ in (("far", "용적률 기준 여유면적 = A×F/100 − C (A 법정 산정 대지면적, F 적용 용적률, C 용적률 산정용 연면적). 미확인 입력이면 결과 없음"),
                        ("equity", "매입 전체 필요자기자본 = P − L − D + T + B + V + R + E (P 보증금 차감 전 계약 총액). 잔금만으로 총액을 복원하지 않는다"),
                        ("cash", "사업기간 최대 필요자기자본 = max(0, −min 누적 CF) + 별도 예비현금")):
        cx = casub.add_parser(name, help=help_)
        cx.add_argument("file", type=Path, help="입력 JSON (kind: %s)" % name)
        cx.add_argument("--json", action="store_true")
    co = sub.add_parser("collect", help="공식 API 수집 (J5-014). 인증키는 J5_DATA_HOME/config.env 의 DATA_GO_KR_SERVICE_KEY 에서만 읽는다")
    csub = co.add_subparsers(dest="collect_command", required=True)
    cs = csub.add_parser("rt-sample", help="상업·업무용 실거래 API 를 시군구·계약월 단위로 호출해 원본 응답과 요약을 남긴다 (R0 표본 월 실측). 정본에 쓰지 않는다")
    cs.add_argument("--lawd-cd", required=True, help="시군구 코드 5자리 (예: 종로구 11110)")
    cs.add_argument("--months", required=True, help="계약월 목록 YYYY-MM,YYYY-MM,… (예: 2026-08,2021-09,2006-03)")
    cs.add_argument("--data-home", type=Path, help="원본·기록·요약을 둘 실데이터 홈. 생략 시 J5_DATA_HOME")
    cs.add_argument("--config", type=Path, help="인증키가 있는 env 파일. 생략 시 J5_DATA_HOME/config.env")
    cs.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="API 주소. 공공데이터포털 API 상세 페이지의 값과 대조한다")
    cs.add_argument("--num-rows", type=int, default=DEFAULT_NUM_ROWS, help="페이지당 행 수 (1~1000)")
    cs.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="월당 최대 페이지 (1~500)")
    cs.add_argument("--json", action="store_true")
    cf = csub.add_parser("rt-fetch", help="계약월 범위를 받는다. 이미 완전히 받은 달(실행 기록의 complete/empty)은 건너뛴다 (--refresh 로 다시 받음). 정본에 쓰지 않는다 (반영은 db rt-load)")
    cf.add_argument("--lawd-cd", required=True, help="시군구 코드 5자리")
    cf.add_argument("--from", dest="from_ym", required=True, help="시작 계약월 YYYY-MM")
    cf.add_argument("--to", dest="to_ym", required=True, help="끝 계약월 YYYY-MM (포함)")
    cf.add_argument("--refresh", action="store_true", help="이미 받은 달도 다시 받는다 (취소·정정 점검)")
    cf.add_argument("--data-home", type=Path)
    cf.add_argument("--config", type=Path)
    cf.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    cf.add_argument("--num-rows", type=int, default=DEFAULT_NUM_ROWS)
    cf.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    cf.add_argument("--json", action="store_true")
    ck = csub.add_parser("rt-recheck", help="취소·정정 점검: 최근 N개월(--recent) 또는 실행 기록상 실패·부분 월(--failed)을 다시 받는다. 정본 반영은 db rt-load, 변경 확인은 db rt-changes")
    ck.add_argument("--lawd-cd", required=True)
    ck.add_argument("--recent", type=int, help="이번 달 포함 최근 N개월을 다시 받는다 (운영 기본 6)")
    ck.add_argument("--failed", action="store_true", help="실행 기록에서 complete/empty 가 아닌 달을 다시 받는다")
    ck.add_argument("--from", dest="from_ym", help="--failed 의 범위 시작 YYYY-MM (기본 2006-01)")
    ck.add_argument("--to", dest="to_ym", help="--failed 의 범위 끝 YYYY-MM (기본 이번 달)")
    ck.add_argument("--data-home", type=Path)
    ck.add_argument("--config", type=Path)
    ck.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ck.add_argument("--num-rows", type=int, default=DEFAULT_NUM_ROWS)
    ck.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    ck.add_argument("--json", action="store_true")
    cr = csub.add_parser("rt-report", help="저장된 원본 응답만으로 요약을 다시 만든다 (네트워크 없음)")
    cr.add_argument("--lawd-cd", required=True)
    cr.add_argument("--months", required=True)
    cr.add_argument("--data-home", type=Path)
    cr.add_argument("--run-id", help="특정 실행의 파일만")
    cr.add_argument("--dist", action="append", default=[], metavar="FIELD", help="이 필드는 값 종류 수와 무관하게 전체 분포를 보인다 (예: --dist umdNm). 반복 가능")
    cr.add_argument("--json", action="store_true")
    return p


def _data_home(args) -> Path | None:
    home = getattr(args, "data_home", None) or (Path(os.environ["J5_DATA_HOME"]) if os.environ.get("J5_DATA_HOME") else None)
    if home is None:
        print("실데이터 홈을 모른다: --data-home 을 주거나 J5_DATA_HOME 을 설정한다", file=sys.stderr)
    return home


def _db_path(args) -> Path | None:
    if args.db is not None:
        return args.db
    home = os.environ.get("J5_DATA_HOME")
    if not home:
        print("정본 경로를 모른다: --db 를 주거나 J5_DATA_HOME 을 설정한다 (저장소 안에 두지 않는다)", file=sys.stderr)
        return None
    return default_db_path(Path(home))


def _db_main(args) -> int:
    if args.db_command in ("restore", "backup-verify"):
        return _db_offline(args)
    path = _db_path(args)
    if path is None:
        return USAGE_ERROR
    try:
        if args.db_command == "init":
            study_id = args.study_id or os.environ.get("J5_STUDY_ID") or ""
            data_mode = args.data_mode or os.environ.get("J5_DATA_MODE") or ""
            if not study_id or not data_mode:
                print("--study-id 와 --data-mode 가 필요하다 (또는 J5_STUDY_ID·J5_DATA_MODE)", file=sys.stderr)
                return USAGE_ERROR
            with Db.create(path, study_id=study_id, data_mode=data_mode) as db:
                st = db.status()
            print(f"정본 생성: {path} (db_schema {st['db_schema_version']}, study {st['study_id']}, {st['data_mode']})")
            return 0
        with Db.open(path) as db:
            if args.db_command == "status":
                home = args.data_home or (Path(os.environ["J5_DATA_HOME"]) if os.environ.get("J5_DATA_HOME") else None)
                st = db.status(home)
                if args.json:
                    print(json.dumps(st, ensure_ascii=True, indent=2))
                else:
                    print(f"정본: {st['path']}")
                    print(f"db_schema {st['db_schema_version']} (도구 {st['tool_schema_version']}) · SQLite {st['sqlite_version']} · 외래키 {'켜짐' if st['foreign_keys'] == 1 else '꺼짐'}")
                    print(f"study_id {st['study_id']} · data_mode {st['data_mode']} · dataset_version {st['dataset_version']} · 생성 {st['created_at']}")
                    print("행 수: " + ", ".join(f"{k} {v}" for k, v in st["counts"].items()))
                    print(f"integrity_check: {', '.join(st['integrity_check'])} · foreign_key_check: {len(st['foreign_key_check'])}건 위반")
                    ps = st.get("projection") or {}
                    if ps:
                        line = f"파생본: {ps['state']}"
                        if ps.get("published_at"):
                            line += f" · 마지막 게시 {ps['published_at']}"
                        if ps.get("last_failed_at"):
                            line += f" · 마지막 실패 {ps['last_failed_at']}"
                        print(line)
                    bs = st.get("backup") or {}
                    if bs:
                        line = f"백업: {bs['state']}"
                        if bs.get("backup_at"):
                            line += f" · 마지막 백업 {bs['backup_at']} ({bs.get('backup_dir')})"
                        print(line)
                return 0 if st["ok"] else 1
            if args.db_command == "load-seed":
                seed = _load_seed(args.seed)
                if seed is None:
                    return USAGE_ERROR
                r = db.load_seed(seed)
                changed = "정본이 바뀌어 올렸다" if (r.inserted or r.updated) else "변화 없음이라 유지"
                print(f"시드 반영: 신규 {r.inserted}, 갱신 {r.updated}, 변화 없음 {r.unchanged} (data_mode {db.data_mode}). dataset_version {r.dataset_version} ({changed})")
                return 0
            if args.db_command == "import":
                home = _data_home(args)
                if home is None:
                    return USAGE_ERROR
                if not args.package.exists():
                    print(f"입력이 없음: {args.package}", file=sys.stderr)
                    return USAGE_ERROR
                r = import_package(db, args.package, home)
                sys.stdout.write(r.to_json() if args.json else r.to_text())
                return EXIT_BY_OUTCOME[r.outcome]
            if args.db_command == "project":
                home = _data_home(args)
                if home is None:
                    return USAGE_ERROR
                r = build_projection(db, home, photos=args.photos)
                sys.stdout.write(r.to_json() if args.json else r.to_text())
                return PROJECT_EXIT[r.outcome]
            if args.db_command == "project-copy":
                home = _data_home(args)
                if home is None:
                    return USAGE_ERROR
                if not args.dest_dir.is_dir():
                    print(f"대상 폴더가 없음: {args.dest_dir}", file=sys.stderr)
                    return USAGE_ERROR
                try:
                    r = copy_latest(db, home, args.dest_dir, allow_stale=args.allow_stale)
                except ProjectionError as e:
                    print(f"내보내기 거절 [{e.code}]: {e.message}", file=sys.stderr)
                    return 1
                label = f"구본 {r['state']}" if r["stale"] else "최신"
                print(f"사본 작성됨: {r['dest']} ({r['bytes']} 바이트, sha256 {r['sha256']}) · 파생본 v{r['source_dataset_version']} {r['generated_at']} · {label}")
                print("이 사본은 백업 완료를 뜻하지 않는다.")
                return 0
            if args.db_command == "backup":
                home = _data_home(args)
                if home is None:
                    return USAGE_ERROR
                r = create_backup(db, home, dest_root=args.dest)
                sys.stdout.write(r.to_json() if args.json else r.to_text())
                return BACKUP_EXIT[r.outcome]
            if args.db_command == "check-photos":
                home = _data_home(args)
                if home is None:
                    return USAGE_ERROR
                c = check_photos(db, home)
                sys.stdout.write(json.dumps(c, ensure_ascii=True, sort_keys=True, indent=2) + "\n" if args.json else check_photos_text(c))
                return 0 if c["ok_all"] else 1
            if args.db_command == "survey-apply":
                if not args.file.is_file():
                    print(f"입력 파일이 없음: {args.file}", file=sys.stderr)
                    return USAGE_ERROR
                r = apply_input(db, load_input(args.file))
                sys.stdout.write(json.dumps(r.to_dict(), ensure_ascii=True, sort_keys=True, indent=2) + "\n" if args.json else r.to_text())
                return 0
            if args.db_command == "survey-vacancy":
                v = vacancy(db, args.session_id)
                sys.stdout.write(json.dumps(v, ensure_ascii=True, sort_keys=True, indent=2) + "\n" if args.json else vacancy_text(v))
                return 0
            if args.db_command == "survey-compare":
                c = compare(db, args.session_a, args.session_b)
                sys.stdout.write(json.dumps(c, ensure_ascii=True, sort_keys=True, indent=2) + "\n" if args.json else compare_text(c))
                return 0
            if args.db_command == "survey-overview":
                o = overview(db)
                sys.stdout.write(json.dumps(o, ensure_ascii=True, sort_keys=True, indent=2) + "\n" if args.json else overview_text(o))
                return 0
            if args.db_command == "rt-load":
                home = _data_home(args)
                if home is None:
                    return USAGE_ERROR
                lawd = check_lawd(args.lawd_cd)
                run_ids = [args.run_id] if args.run_id else unloaded_run_ids(db, home, lawd)
                if not run_ids:
                    print(f"정본에 없는 실행 기록이 없다 ({home / 'raw' / 'rt_nrg' / lawd})", file=sys.stderr)
                    return 0
                results = [load_run(db, home, lawd, rid) for rid in run_ids]
                sys.stdout.write(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2) + "\n" if args.json else "".join(r.to_text() for r in results))
                return 0
            if args.db_command == "rt-coverage":
                lawd = check_lawd(args.lawd_cd)
                c = coverage(db, lawd, month_range(args.from_ym, args.to_ym))
                sys.stdout.write(json.dumps(c, ensure_ascii=False, indent=2) + "\n" if args.json else coverage_text(c))
                return 0
            if args.db_command == "rt-candidates":
                lawd = check_lawd(args.lawd_cd)
                emd = [e.strip() for e in args.emd.split(",") if e.strip()] if args.emd else None
                rows = candidates(db, lawd, month_range(args.from_ym, args.to_ym), emd_names=emd, unlinked_only=args.unlinked_only)
                if args.json:
                    sys.stdout.write(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
                elif args.out is not None:
                    try:
                        with open(args.out, "x", encoding="utf-8", newline="") as f:
                            f.write(candidates_csv(rows))
                    except FileExistsError:
                        print(f"이미 있는 파일을 덮어쓰지 않는다: {args.out}", file=sys.stderr)
                        return USAGE_ERROR
                    sys.stdout.write(candidates_text(rows) + f"CSV: {args.out}\n")
                else:
                    sys.stdout.write(candidates_csv(rows))
                return 0
            if args.db_command == "rt-link":
                r = apply_decisions(db, read_decisions_csv(args.file))
                sys.stdout.write(json.dumps(r.to_dict(), ensure_ascii=False, indent=2) + "\n" if args.json else r.to_text())
                return 0
            if args.db_command == "rt-zones":
                if args.action == "apply":
                    if args.file is None or not args.file.is_file():
                        print("규칙 파일이 필요하다: rt-zones apply <규칙.json>", file=sys.stderr)
                        return USAGE_ERROR
                    r = apply_rules(db, load_rules(args.file))
                    sys.stdout.write(json.dumps(r.to_dict(), ensure_ascii=False, indent=2) + "\n" if args.json else r.to_text())
                    return 0
                rules = current_rules(db)
                counts = zone_counts(db)
                sys.stdout.write(json.dumps({"rules": rules, "counts": counts}, ensure_ascii=False, indent=2) + "\n" if args.json else rules_text(rules, counts))
                return 0
            if args.db_command == "record-add":
                if not args.file.is_file():
                    print(f"입력 파일이 없음: {args.file}", file=sys.stderr)
                    return USAGE_ERROR
                r = apply_record_input(db, load_record_input(args.file))
                sys.stdout.write(json.dumps(r.to_dict(), ensure_ascii=False, indent=2) + "\n" if args.json else r.to_text())
                return 0
            if args.db_command == "asof":
                known = args.at if args.as_recorded and not args.known_by else args.known_by
                a = asof(db, args.asset, args.at, known_by=known)
                sys.stdout.write(json.dumps(a, ensure_ascii=False, indent=2) + "\n" if args.json else asof_text(a))
                return 0
            if args.db_command == "recheck":
                st_ = recheck_status(db, args.asset)
                sys.stdout.write(json.dumps(st_, ensure_ascii=False, indent=2) + "\n" if args.json else recheck_text(st_))
                return 0
            if args.db_command == "recheck-add":
                if not args.file.is_file():
                    print(f"입력 파일이 없음: {args.file}", file=sys.stderr)
                    return USAGE_ERROR
                r = apply_recheck_input(db, load_recheck_input(args.file))
                sys.stdout.write(json.dumps(r, ensure_ascii=False, indent=2) + "\n" if args.json else recheck_add_text(r))
                return 0
            if args.db_command == "photo-series":
                if args.tag is not None and args.tag not in photo_tags():
                    print(f"알 수 없는 사진 태그 {args.tag!r}. 허용: {', '.join(photo_tags())}", file=sys.stderr)
                    return USAGE_ERROR
                if args.export:
                    home = _data_home(args)
                    if home is None:
                        return USAGE_ERROR
                    r = export_series(db, home, args.asset, tag=args.tag)
                    if args.json:
                        sys.stdout.write(json.dumps(r, ensure_ascii=False, indent=2) + "\n")
                    else:
                        print(f"사진 시계열 내보내기 → {r['dest']}: 묶음 {r['groups']}개, 사진 {r['photos']}장, 복사 {r['copied']}, 건너뜀(같은 내용) {r['skipped']}, 문제 {len(r['problems'])}")
                        for pmsg in r["problems"]:
                            print(f"  [문제] {pmsg}")
                        print("내보낸 사진은 실데이터다. 저장소·공개 배포에 넣지 않는다. 내보내기 성공은 백업 완료가 아니다")
                    return 0 if not r["problems"] else 1
                s = photo_series(db, args.asset, tag=args.tag)
                sys.stdout.write(json.dumps(s, ensure_ascii=False, indent=2) + "\n" if args.json else series_text(s))
                return 0
            if args.db_command == "rt-changes":
                lawd = check_lawd(args.lawd_cd)
                c = changes_since(db, lawd, args.since_run, zones=tuple(args.zone) if args.zone else None)
                sys.stdout.write(json.dumps(c, ensure_ascii=False, indent=2) + "\n" if args.json else changes_text(c))
                return 0
            if args.db_command == "parcels-load":
                if not args.bundle.is_file():
                    print(f"번들 파일이 없음: {args.bundle}", file=sys.stderr)
                    return USAGE_ERROR
                r = load_bundle(db, load_parcels_json(args.bundle, PARCELS_BUNDLE_SCHEMA))
                sys.stdout.write(json.dumps(r.to_dict(), ensure_ascii=True, sort_keys=True, indent=2) + "\n" if args.json else r.to_text())
                return 0
            if args.db_command == "parcels-suggest":
                eff = args.effective_from or db.now()[:10]
                sug = suggest_links(db, effective_from=eff)
                text_ = json.dumps({k: v for k, v in sug.items() if not k.startswith("_")}, ensure_ascii=False, indent=2) + "\n"
                if args.out is None:
                    sys.stdout.write(text_)
                else:
                    if args.out.exists():
                        print(f"출력 파일이 이미 있다 (덮어쓰지 않음): {args.out}", file=sys.stderr)
                        return 1
                    args.out.parent.mkdir(parents=True, exist_ok=True)
                    args.out.write_text(text_, encoding="utf-8")
                    print(f"연결 제안 {len(sug['links'])}건 → {args.out}. 검토·수정한 뒤 `j5 db parcels-link {args.out}` 로 반영한다")
                print(f"위치점 없는 물건 {len(sug['_unlocated_asset_ids'])}, 필지 밖 위치점 {len(sug['_unmatched_asset_ids'])}", file=sys.stderr)
                return 0
            if args.db_command == "parcels-link":
                if not args.file.is_file():
                    print(f"연결 파일이 없음: {args.file}", file=sys.stderr)
                    return USAGE_ERROR
                r = apply_links(db, load_parcels_json(args.file, PARCELS_LINKS_SCHEMA))
                sys.stdout.write(json.dumps(r.to_dict(), ensure_ascii=True, sort_keys=True, indent=2) + "\n" if args.json else r.to_text())
                return 0
    except DbError as e:
        print(f"정본 오류 [{e.code}]: {e.message}", file=sys.stderr)
        return 1
    except CollectError as e:
        print(f"입력 오류 [{e.code}]: {e.message}", file=sys.stderr)
        return USAGE_ERROR
    except sqlite3.Error as e:
        print(f"SQLite 오류 [{type(e).__name__}]: {e}. 정본은 트랜잭션 단위로 되돌아갔다", file=sys.stderr)
        return 1
    except ValidationError as e:
        print("입력 거절:", file=sys.stderr)
        for m in e.errors:
            print(f"  {m}", file=sys.stderr)
        return 1
    return USAGE_ERROR


def _db_offline(args) -> int:
    """정본 연결 없이 하는 명령: 백업 검증·복구."""
    if not args.backup_dir.is_dir():
        print(f"백업 폴더가 없음: {args.backup_dir}", file=sys.stderr)
        return USAGE_ERROR
    if args.db_command == "backup-verify":
        try:
            v = verify_backup_dir(args.backup_dir)
        except (BackupError, DbError, OSError, sqlite3.Error) as e:
            print(f"백업 검증 실패 [{getattr(e, 'code', type(e).__name__)}]: {getattr(e, 'message', e)}", file=sys.stderr)
            return 1
        m = v["manifest"]
        if args.json:
            print(json.dumps({"ok": True, "manifest_sha256": v["manifest_sha256"], "dataset_version": m["dataset_version"], "db_schema_version": m["db_schema_version"],
                              "created_at": m["created_at"], "counts": v["counts"], "photos": v["photos"]}, ensure_ascii=True, sort_keys=True, indent=2))
        else:
            print(f"백업 검증 통과: {args.backup_dir} · 정본 v{m['dataset_version']} (db_schema {m['db_schema_version']}, {m['created_at']}) · 사진 {v['photos']}장 연결 확인")
            print("행 수: " + ", ".join(f"{k} {v_}" for k, v_ in v["counts"].items()))
        return 0
    r = restore_backup(args.backup_dir, args.dest_home)
    sys.stdout.write(r.to_json() if args.json else r.to_text())
    return BACKUP_EXIT[r.outcome]


def _parcels_main(args) -> int:
    try:
        if args.parcels_command == "inspect":
            r = inspect_source(args.source, crs_arg=args.crs, encoding=args.encoding, layer=args.layer)
            sys.stdout.write(json.dumps(r, ensure_ascii=False, indent=2) + "\n" if args.json else inspect_text(r))
            return 0 if r["crs"] else 1
        if args.parcels_command == "convert":
            if args.bbox and (args.center or args.radius_m is not None):
                print("--bbox 와 --center/--radius-m 은 함께 쓰지 않는다", file=sys.stderr)
                return USAGE_ERROR
            if args.bbox:
                clip = Clip.from_bbox(args.bbox)
            elif args.center and args.radius_m is not None:
                clip = Clip.from_center(args.center, args.radius_m)
            else:
                print("범위가 필요하다: --bbox 또는 --center 와 --radius-m", file=sys.stderr)
                return USAGE_ERROR
            emd = {}
            for item in args.emd_name:
                code, sep, name = item.partition("=")
                if not sep or len(code) != 10 or not code.isdigit() or not name:
                    print(f"--emd-name 은 10자리코드=이름 형식: {item!r}", file=sys.stderr)
                    return USAGE_ERROR
                emd[code] = name
            opts = ConvertOptions(clip=clip, geometry_version=args.geometry_version, source_name=args.source_name, crs_arg=args.crs, encoding=args.encoding,
                                  license=args.license, emd_names=emd, pnu_field=args.pnu_field, jibun_field=args.jibun_field, max_features=args.max_features,
                                  data_mode="synthetic" if args.synthetic else "real", layer=args.layer)
            bundle = convert(args.source, opts)
            written = write_bundle(bundle, args.out)
            if args.json:
                print(json.dumps({"written": written, "count": bundle["count"], "stats": bundle["stats"], "warnings": bundle["warnings"], "source": bundle["source"], "clip": bundle["clip"]}, ensure_ascii=False, indent=2))
            else:
                sys.stdout.write(convert_text(bundle, written))
            return 0
    except ConvertError as e:
        print(f"필지 변환 실패 [{e.code}]: {e.message}", file=sys.stderr)
        return 1
    return USAGE_ERROR


def _collect_main(args) -> int:
    home = _data_home(args)
    if home is None:
        return USAGE_ERROR
    try:
        lawd = check_lawd(args.lawd_cd)
        if args.collect_command == "rt-recheck":
            if bool(args.recent) == bool(args.failed):
                print("--recent N 또는 --failed 중 하나를 준다", file=sys.stderr)
                return USAGE_ERROR
            if args.recent:
                months = recent_months(args.recent)
                why = f"최근 {args.recent}개월 재조회"
            else:
                done = months_done_on_disk(home, lawd)
                span = month_range(args.from_ym or "2006-01", args.to_ym or recent_months(1)[0][:4] + "-" + recent_months(1)[0][4:])
                months = [m for m in span if done.get(m) not in ("complete", "empty")]
                why = f"실패·부분·미수집 월 재조회 ({span[0]}~{span[-1]} 중 {len(months)}개월)"
            if not months:
                print("다시 받을 달이 없다", file=sys.stderr)
                return 0
            key, source = service_key(home, config_path=args.config)
            print(f"점검 수집 시작: 시군구 {lawd}, {why}, 인증키 출처 {source}. 이 호출은 공공데이터포털 일일 트래픽을 약 {len(months)}회 이상 쓴다.", file=sys.stderr)
            run = collect_months(home, key=key, key_source=source, lawd_cd=lawd, months=months, endpoint=args.endpoint, num_rows=args.num_rows, max_pages=args.max_pages)
            text_ = json.dumps(run.to_dict(), ensure_ascii=False, indent=2) + "\n" if args.json else run_text(run) + f"다음: `j5 db rt-load --lawd-cd {lawd}` 로 반영하고 `j5 db rt-changes --lawd-cd {lawd} --since-run <이전 실행>` 으로 변경을 본다.\n"
            sys.stdout.write(redact(text_, key))
            return 0 if run.ok else 1
        if args.collect_command == "rt-fetch":
            wanted = month_range(args.from_ym, args.to_ym)
            done = months_done_on_disk(home, lawd)
            skipped = [] if args.refresh else [m for m in wanted if done.get(m) in ("complete", "empty")]
            months = [m for m in wanted if m not in skipped]
            if not months:
                print(f"{len(wanted)}개월이 모두 이미 완전히 받아져 있다 (--refresh 로 다시 받는다)", file=sys.stderr)
                return 0
            key, source = service_key(home, config_path=args.config)
            warn = config_permission_warning(load_config(home, path=args.config)[1])
            if warn:
                print(f"주의: {warn}", file=sys.stderr)
            print(f"수집 시작: 시군구 {lawd}, {wanted[0]}~{wanted[-1]} 중 {len(months)}개월 (이미 받은 {len(skipped)}개월 건너뜀), 인증키 출처 {source}."
                  f" 이 호출은 공공데이터포털 일일 트래픽을 약 {len(months)}회 이상 쓴다.", file=sys.stderr)
            run = collect_months(home, key=key, key_source=source, lawd_cd=lawd, months=months, endpoint=args.endpoint, num_rows=args.num_rows, max_pages=args.max_pages)
            text_ = json.dumps(run.to_dict(), ensure_ascii=False, indent=2) + "\n" if args.json else run_text(run) + "다음: `j5 db rt-load --lawd-cd " + lawd + "` 로 정본에 반영한다.\n"
            sys.stdout.write(redact(text_, key))
            return 0 if run.ok else 1
        months = parse_months(args.months)
        if args.collect_command == "rt-report":
            rep = report_from_raw(home, lawd, months, args.run_id, dist_fields=tuple(args.dist))
            sys.stdout.write(json.dumps(rep, ensure_ascii=False, indent=2) + "\n" if args.json else report_text(rep))
            return 0
        if args.collect_command == "rt-sample":
            key, source = service_key(home, config_path=args.config)
            _, cfg_path = load_config(home, path=args.config)
            warn = config_permission_warning(cfg_path)
            if warn:
                print(f"주의: {warn}", file=sys.stderr)
            print(f"수집 시작: 시군구 {lawd}, 계약월 {', '.join(months)}, 인증키 출처 {source}. 이 호출은 공공데이터포털 일일 트래픽을 쓴다.", file=sys.stderr)
            run = collect_months(home, key=key, key_source=source, lawd_cd=lawd, months=months, endpoint=args.endpoint, num_rows=args.num_rows, max_pages=args.max_pages)
            text_ = json.dumps(run.to_dict(), ensure_ascii=False, indent=2) + "\n" if args.json else run_text(run)
            sys.stdout.write(redact(text_, key))  # 화면 출력도 한 번 더 가린다
            return 0 if run.ok else 1
    except ConfigError as e:
        print(f"설정 오류 [{e.code}]: {e.message}", file=sys.stderr)
        return USAGE_ERROR
    except CollectError as e:
        print(f"수집 실패 [{e.code}]: {e.message}", file=sys.stderr)
        return 1
    return USAGE_ERROR


def _load_seed(path: Path) -> list[dict] | None:
    if not path.is_file():
        print(f"시드 파일이 없음: {path}", file=sys.stderr)
        return None
    try:
        seed = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        print(f"시드 파일 해석 실패: {e}", file=sys.stderr)
        return None
    errs = schema_errors("assets_seed.schema.json", seed)
    if errs:
        print("시드 파일이 스키마에 맞지 않음:", file=sys.stderr)
        for m in errs:
            print(f"  {m}", file=sys.stderr)
        return None
    return seed


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code in (0,) else USAGE_ERROR

    if args.command == "inspect":
        if not args.package.exists():
            print(f"입력이 없음: {args.package}", file=sys.stderr)
            return USAGE_ERROR
        seed = None
        if args.seed is not None:
            seed = _load_seed(args.seed)
            if seed is None:
                return USAGE_ERROR
        study_id = args.study_id or os.environ.get("J5_STUDY_ID") or None
        report = inspect_package(args.package, seed=seed, study_id=study_id)
        sys.stdout.write(report.to_json() if args.json else report.to_text())
        return EXIT[report.verdict()]

    if args.command == "copy":
        try:
            r = copy_package(args.package, args.dest_dir)
        except PreserveError as e:
            print(f"사본 실패 [{e.code}]: {e.message}", file=sys.stderr)
            return 1
        print(f"사본 작성됨: {r.dest} ({r.bytes} 바이트, sha256 {r.sha256})")
        print(f"해시 파일: {r.sidecar}")
        if r.same_device:
            print("주의: 원본과 같은 장치에 있음. 독립 위치 여부는 사용자가 확인한다.")
        print("이 사본은 정본 반영·백업 완료를 뜻하지 않는다. 원본은 삭제하지 않았다.")
        return 0

    if args.command == "db":
        return _db_main(args)
    if args.command == "parcels":
        return _parcels_main(args)
    if args.command == "collect":
        return _collect_main(args)
    if args.command == "calc":
        return _calc_main(args)
    return USAGE_ERROR


def _calc_main(args) -> int:
    if not args.calc_command:
        print("calc 하위 명령: far / equity / cash", file=sys.stderr)
        return USAGE_ERROR
    if not args.file.is_file():
        print(f"입력 파일이 없음: {args.file}", file=sys.stderr)
        return USAGE_ERROR
    try:
        r = run_calc(load_calc_input(args.file, expected_kind=args.calc_command))
    except ValidationError as e:
        print("입력 거절:", file=sys.stderr)
        for m in e.errors:
            print(f"  {m}", file=sys.stderr)
        return USAGE_ERROR
    sys.stdout.write(json.dumps(r, ensure_ascii=False, indent=2) + "\n" if args.json else calc_text(r))
    # 결과가 미확정(미확인 입력·오류)이면 1: 완성값이 아님을 종료 코드로도 알린다
    return 0 if r.get("result") is not None or r.get("result_krw") is not None else 1
