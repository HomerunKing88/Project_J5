"""j5 명령줄. inspect(패키지 검사)와 copy(독립 사본).

종료 코드: 0 ok / 1 reject / 2 hold / 3 사용 오류.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from j5 import APP_VERSION
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
    return p


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
    return USAGE_ERROR
