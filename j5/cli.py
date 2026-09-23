"""j5 명령줄. inspect(패키지 검사), copy(독립 사본), db(정본 SQLite: init/status/load-seed).

종료 코드: 0 ok / 1 reject·실패 / 2 hold / 3 사용 오류.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from j5 import APP_VERSION
from j5.db.store import Db, DbError, default_db_path
from j5.db.validate import ValidationError
from j5.package.preserve import PreserveError, copy_package
from j5.package.validate import inspect_package
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
    ds = dsub.add_parser("status", help="스키마 버전·study_id·dataset_version·행 수·무결성·외래키 검사")
    ds.add_argument("--json", action="store_true")
    dl = dsub.add_parser("load-seed", help="assets.seed.json 의 물건을 asset_id 그대로 승계해 반영한다")
    dl.add_argument("seed", type=Path)
    return p


def _db_path(args) -> Path | None:
    if args.db is not None:
        return args.db
    home = os.environ.get("J5_DATA_HOME")
    if not home:
        print("정본 경로를 모른다: --db 를 주거나 J5_DATA_HOME 을 설정한다 (저장소 안에 두지 않는다)", file=sys.stderr)
        return None
    return default_db_path(Path(home))


def _db_main(args) -> int:
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
                st = db.status()
                if args.json:
                    print(json.dumps(st, ensure_ascii=True, indent=2))
                else:
                    print(f"정본: {st['path']}")
                    print(f"db_schema {st['db_schema_version']} (도구 {st['tool_schema_version']}) · SQLite {st['sqlite_version']} · 외래키 {'켜짐' if st['foreign_keys'] == 1 else '꺼짐'}")
                    print(f"study_id {st['study_id']} · data_mode {st['data_mode']} · dataset_version {st['dataset_version']} · 생성 {st['created_at']}")
                    print("행 수: " + ", ".join(f"{k} {v}" for k, v in st["counts"].items()))
                    print(f"integrity_check: {', '.join(st['integrity_check'])} · foreign_key_check: {len(st['foreign_key_check'])}건 위반")
                return 0 if st["ok"] else 1
            if args.db_command == "load-seed":
                seed = _load_seed(args.seed)
                if seed is None:
                    return USAGE_ERROR
                r = db.load_seed(seed)
                print(f"시드 반영: 신규 {r.inserted}, 갱신 {r.updated}, 변화 없음 {r.unchanged} (data_mode {db.data_mode}). dataset_version 은 바뀌지 않았다")
                return 0
    except DbError as e:
        print(f"정본 오류 [{e.code}]: {e.message}", file=sys.stderr)
        return 1
    except ValidationError as e:
        print("입력 거절:", file=sys.stderr)
        for m in e.errors:
            print(f"  {m}", file=sys.stderr)
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
    return USAGE_ERROR
