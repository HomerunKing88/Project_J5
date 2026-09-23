"""j5 명령줄. inspect(패키지 검사), copy(독립 사본), db(정본 SQLite: init/status/load-seed/import/project/backup/restore/check-photos).

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
from j5.db.projection import EXIT_BY_OUTCOME as PROJECT_EXIT, ProjectionError, build_projection, copy_latest
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
    except DbError as e:
        print(f"정본 오류 [{e.code}]: {e.message}", file=sys.stderr)
        return 1
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
