"""건축물대장 API 표본 수집 (J5-034, J5-013B-3 첫 조각). 릴리스 계획 §6 R2 "허용된 필지·건축물 자료", ADR-05, AGENTS "약관·승인·실제 응답을 먼저 확인한다".

하는 일: 지정한 필지(PNU)마다 국토교통부 건축HUB 건축물대장정보 서비스의 오퍼레이션(기본개요·총괄표제부·표제부·층별개요·부속지번 등)을 불러
원본 응답을 그대로 `J5_DATA_HOME/raw/br_hub/<PNU>/<오퍼레이션 키>/pNNN-<실행>.xml` 에 두고, 요청·응답 기록(`run-<실행>.json`, 키 가림)과
오퍼레이션별 필드 가용성·건수 요약(`report-<실행>.json`)을 만든다. 정본에는 쓰지 않는다(정규화·필지 연결은 실제 응답을 확인한 다음 조각).
하지 않는 일: 필드 이름 가정·값 해석·면적 판단. 소유자 정보 조회(이 서비스에 요청하지 않는다). 비공개 API 추측·차단 우회. 인증키 출력.

대상은 지정한 필지만이다(AGENTS "상세 필지·대장은 지정 대상만 수집한다"): `--pnu` 또는 정본의 물건↔필지 연결. 한 번에 MAX_PNUS 필지까지.
엔드포인트는 공공데이터포털 API 상세 페이지의 값을 사용자가 확인해 `--endpoint-base` 로 줄 수 있다. 기본값과 오퍼레이션 이름은 코드에 적은
알려진 주소이며, 실제 응답으로 확인하기 전까지는 가정이다(작업 환경에서 포털 문서에 접속하지 못했다, 작업 기록 J5-034).
응답 해석·재시도·가림·배타적 생성은 실거래 수집(j5.collect.rt)과 같은 함수를 쓴다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from j5.collect.config import redact
from j5.collect.rt import OK_CODES, CollectError, PageResult, _log, _new_run_id, _now, _write_new, fetch_page, page_mismatch, page_of_file, parse_response, summarize_items

DEFAULT_ENDPOINT_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"  # 사용자가 API 상세 페이지에서 확인한다
PROVIDER = "data.go.kr:BldRgstHubService"
RAW_DIR = Path("raw") / "br_hub"
LOG_NAME = "collect.log"
# 키: (오퍼레이션 이름, 설명). 소유자·주택가격·오수정화시설은 넣지 않는다 (매입 검토 단위의 건물 구성과 면적에 필요한 것만)
OPERATIONS = {
    "basis": ("getBrBasisOulnInfo", "기본개요"),
    "recap": ("getBrRecapTitleInfo", "총괄표제부"),
    "title": ("getBrTitleInfo", "표제부"),
    "floor": ("getBrFlrOulnInfo", "층별개요"),
    "atch": ("getBrAtchJibunInfo", "부속지번"),
    "expos": ("getBrExposInfo", "전유부"),
    "area": ("getBrExposPubuseAreaInfo", "전유공용면적"),
    "zone": ("getBrJijiguInfo", "지역지구구역"),
}
DEFAULT_OPS = ("basis", "recap", "title", "floor", "atch")
MAX_PNUS = 50
DEFAULT_NUM_ROWS = 100
DEFAULT_MAX_PAGES = 20
# 요약의 값 분포는 값 종류 20개 이하이고 모든 값이 이 길이 이하인 필드만 싣는다. 주소·건물명처럼 긴 글은 분포 없이 채움 비율만 (요약은 사용자가 공유하는 파일이다)
DIST_MAX_LEN = 12
PNU_RE = re.compile(r"^\d{19}$")
RUN_FILE_RE = re.compile(r"^run-(\d{8}-\d{6}-[0-9a-f]{6})\.json$")


@dataclass
class TargetResult:
    pnu: str
    op: str
    outcome: str = "failed"            # complete / empty / partial / failed
    total_count: int | None = None
    items: int = 0
    pages: list[PageResult] = field(default_factory=list)
    message: str = ""


@dataclass
class BrRunResult:
    run_id: str
    provider: str
    endpoint_base: str
    started_at: str
    finished_at: str | None = None
    ops: list[str] = field(default_factory=list)
    pnus: list[str] = field(default_factory=list)
    targets: list[TargetResult] = field(default_factory=list)
    key_source: str = ""
    run_path: str | None = None
    report_path: str | None = None

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "provider": self.provider, "endpoint_base": self.endpoint_base, "started_at": self.started_at, "finished_at": self.finished_at,
                "ops": self.ops, "pnus": self.pnus, "key_source": self.key_source, "run_path": self.run_path, "report_path": self.report_path,
                "targets": [{"pnu": t.pnu, "op": t.op, "operation": OPERATIONS[t.op][0], "outcome": t.outcome, "total_count": t.total_count, "items": t.items, "message": t.message,
                             "pages": [{k: v for k, v in p.__dict__.items() if k != "error_body"} for p in t.pages]} for t in self.targets]}

    @property
    def ok(self) -> bool:
        return all(t.outcome in ("complete", "empty") for t in self.targets)


# ---------------------------------------------------------------- 입력

def parse_pnus(values: list[str]) -> list[str]:
    """'P1,P2' 여러 개 → PNU 목록 (순서 유지, 중복 제거). 19자리 숫자이고 산 구분(11번째 자리)이 1·2 인 것만."""
    out: list[str] = []
    for v in values:
        for part in [p.strip() for p in v.split(",") if p.strip()]:
            pnu_params(part)
            if part not in out:
                out.append(part)
    return out


def pnu_params(pnu: str) -> dict:
    """PNU(시군구 5·법정동 5·산 구분 1·본번 4·부번 4) → 요청 변수. 산 구분 1(일반)→대지구분 0, 2(산)→1. 블록(2)은 PNU 로 나타나지 않는다."""
    if not PNU_RE.match(pnu or ""):
        raise CollectError("bad_pnu", f"PNU 는 19자리 숫자: {pnu!r}")
    plat = {"1": "0", "2": "1"}.get(pnu[10])
    if plat is None:
        raise CollectError("bad_pnu", f"PNU 11번째 자리(산 구분)는 1 또는 2: {pnu!r}")
    return {"sigunguCd": pnu[:5], "bjdongCd": pnu[5:10], "platGbCd": plat, "bun": pnu[11:15], "ji": pnu[15:19]}


def parse_ops(text: str | None) -> list[str]:
    if not text:
        return list(DEFAULT_OPS)
    out: list[str] = []
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        if part not in OPERATIONS:
            raise CollectError("bad_op", f"모르는 오퍼레이션 키 {part!r}. 가능한 키: {', '.join(OPERATIONS)}")
        if part not in out:
            out.append(part)
    if not out:
        raise CollectError("bad_op", "오퍼레이션이 없다")
    return out


def build_url(endpoint_base: str, op: str, key: str, pnu: str, page_no: int, num_rows: int) -> str:
    """인증키에 '%' 가 있으면 이미 URL 부호화된 키로 보고 그대로 붙인다 (실거래 수집과 같은 규칙)."""
    params = urlencode({**pnu_params(pnu), "pageNo": page_no, "numOfRows": num_rows, "_type": "xml"})
    k = key if "%" in key else quote(key, safe="")
    base = endpoint_base.rstrip("/")
    if urlsplit(base).query:
        raise CollectError("bad_endpoint", "엔드포인트 기본 주소에 물음표 뒤 값을 넣지 않는다 (오퍼레이션 이름이 뒤에 붙는다)")
    return f"{base}/{OPERATIONS[op][0]}?serviceKey={k}&{params}"


# ---------------------------------------------------------------- 수집 실행

def _collect_target(tr: TargetResult, *, data_home: Path, run_id: str, url_for_page, key: str, max_pages: int, opener, sleep, pause_s: float, log: Path) -> None:
    """한 (필지, 오퍼레이션)의 페이지를 끝까지 받는다. 결과 구분은 실거래 수집의 월 결과와 같다."""
    out_dir = data_home / RAW_DIR / tr.pnu / tr.op
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{run_id} br {tr.pnu} {tr.op}"
    page = 1
    while page <= max_pages:
        pr, data = fetch_page(url_for_page(page), key, opener=opener, sleep=sleep)
        pr.page_no = page
        tr.pages.append(pr)
        if data is None:
            if pr.error_body:
                epath = out_dir / f"p{page:03d}-{run_id}-http{pr.http_status}.txt"
                _write_new(epath, redact(pr.error_body.decode("utf-8", errors="replace"), key).encode("utf-8"))
                pr.error_body_path = epath.relative_to(data_home).as_posix()
            pr.error_body = None
            _log(log, f"{tag} p{page} {pr.outcome} {pr.error}", key)
            break
        path = out_dir / f"p{page:03d}-{run_id}.xml"
        _write_new(path, data)
        pr.path = path.relative_to(data_home).as_posix()
        try:
            parsed = parse_response(data)
        except CollectError as e:
            pr.outcome, pr.error = "bad_response", redact(e.message, key)
            _log(log, f"{tag} p{page} bad_response {e.code}", key)
            break
        pr.result_code = redact(parsed["result_code"], key) if parsed["result_code"] else None
        pr.result_msg = redact(parsed["result_msg"], key) if parsed["result_msg"] else None
        pr.total_count, pr.num_rows, pr.item_count = parsed["total_count"], parsed["num_rows"], len(parsed["items"])
        if pr.result_code not in OK_CODES:
            pr.outcome, pr.error = "api_error", f"resultCode {pr.result_code}: {pr.result_msg}"
            _log(log, f"{tag} p{page} api_error {pr.result_code} {pr.result_msg}", key)
            break
        if page_mismatch(parsed, page):
            # 요청한 페이지와 다른 페이지가 오면(pageNo 를 무시하고 앞 페이지를 되풀이하는 등) 항목을 더하지 않고 실패로 둔다.
            # 더하면 같은 행이 겹쳐 totalCount 에 닿아 뒷 행을 받지 않고도 complete 가 된다 (리뷰 반영 PR #81)
            pr.outcome, pr.error = "bad_response", f"요청한 페이지 {page} 에 응답 pageNo {parsed['page_no']} 가 왔다 (페이지 넘김이 맞지 않음)"
            _log(log, f"{tag} p{page} bad_response page_mismatch {parsed['page_no']}", key)
            break
        pr.outcome = "ok" if pr.item_count else "empty"
        tr.items += pr.item_count
        if tr.total_count is None:
            tr.total_count = pr.total_count
        _log(log, f"{tag} p{page} {pr.outcome} items={pr.item_count} total={pr.total_count} bytes={pr.bytes}", key)
        if pr.total_count is None or pr.item_count == 0 or tr.items >= pr.total_count:
            break
        page += 1
        sleep(pause_s)
    last = tr.pages[-1] if tr.pages else None
    if last is None or last.outcome in ("http_error", "network_error", "bad_response", "api_error"):
        tr.outcome = "failed"
        tr.message = f"실패: {last.outcome if last else '요청 없음'} ({last.error if last else ''})"
    elif tr.total_count == 0:
        tr.outcome = "empty"
        tr.message = "정상 응답, 0건 (API 실패가 아님. 그 필지에 해당 대장이 없거나 지번이 다르게 등록됨)"
    elif tr.total_count is None:
        tr.outcome = "partial"
        tr.message = f"totalCount 가 없거나 숫자가 아니라 완전성을 확인할 수 없다 ({tr.items}건 받음). 응답 형식을 확인한다"
    elif tr.items < tr.total_count:
        tr.outcome = "partial"
        tr.message = f"페이지 누락: totalCount {tr.total_count} 중 {tr.items}건 (max_pages {max_pages} 또는 빈 페이지)"
    else:
        tr.outcome = "complete"
        tr.message = f"{tr.items}건 ({len(tr.pages)}페이지)"


def collect_buildings(data_home: Path, *, key: str, key_source: str, pnus: list[str], ops: list[str], endpoint_base: str = DEFAULT_ENDPOINT_BASE,
                      num_rows: int = DEFAULT_NUM_ROWS, max_pages: int = DEFAULT_MAX_PAGES, opener=None, sleep=time.sleep, pause_s: float = 0.2) -> BrRunResult:
    """필지 × 오퍼레이션마다 원본과 기록을 남긴다. 하나가 실패해도 나머지를 계속하고 결과에 구분해 적는다."""
    data_home = Path(data_home)
    if not pnus:
        raise CollectError("no_pnu", "대상 필지가 없다 (--pnu 또는 --linked)")
    if len(pnus) > MAX_PNUS:
        raise CollectError("too_many_pnus", f"한 번에 {MAX_PNUS}필지까지 받는다 ({len(pnus)}필지). 지정 대상만 나눠 받는다")
    for p in pnus:
        pnu_params(p)
    for op in ops:
        if op not in OPERATIONS:
            raise CollectError("bad_op", f"모르는 오퍼레이션 키 {op!r}")
    if not (1 <= num_rows <= 100) or not (1 <= max_pages <= 100):
        raise CollectError("bad_paging", "numOfRows 는 1~100, max_pages 는 1~100")
    run = BrRunResult(run_id=_new_run_id(), provider=PROVIDER, endpoint_base=endpoint_base, started_at=_now(), ops=list(ops), pnus=list(pnus), key_source=key_source)
    log = data_home / "logs" / LOG_NAME
    log.parent.mkdir(parents=True, exist_ok=True)
    for pnu in pnus:
        for op in ops:
            tr = TargetResult(pnu=pnu, op=op)
            run.targets.append(tr)
            _collect_target(tr, data_home=data_home, run_id=run.run_id, key=key, max_pages=max_pages, opener=opener, sleep=sleep, pause_s=pause_s, log=log,
                            url_for_page=lambda page, pnu=pnu, op=op: build_url(endpoint_base, op, key, pnu, page, num_rows))
            sleep(pause_s)
    run.finished_at = _now()
    run_path = data_home / RAW_DIR / f"run-{run.run_id}.json"
    _write_new(run_path, (redact(json.dumps(run.to_dict(), ensure_ascii=False, indent=2), key) + "\n").encode("utf-8"))
    run.run_path = run_path.relative_to(data_home).as_posix()
    report = report_from_raw(data_home, run.run_id)
    report_path = data_home / RAW_DIR / f"report-{run.run_id}.json"
    _write_new(report_path, (redact(json.dumps(report, ensure_ascii=False, indent=2), key) + "\n").encode("utf-8"))
    run.report_path = report_path.relative_to(data_home).as_posix()
    return run


# ---------------------------------------------------------------- 요약

def list_run_ids(data_home: Path) -> list[str]:
    d = Path(data_home) / RAW_DIR
    if not d.is_dir():
        return []
    return sorted(m.group(1) for p in d.glob("run-*.json") if (m := RUN_FILE_RE.match(p.name)))


def load_target_items(data_home: Path, pnu: str, op: str, run_id: str) -> tuple[list[dict], list[str]]:
    """저장된 원본 XML 에서 한 실행의 항목을 다시 읽는다 (네트워크 없음). 다른 실행의 파일은 합치지 않는다."""
    d = Path(data_home) / RAW_DIR / pnu / op
    items: list[dict] = []
    files: list[str] = []
    if not d.is_dir():
        return items, files
    for p in sorted(d.glob(f"p*-{run_id}.xml")):
        try:
            parsed = parse_response(p.read_bytes())
        except CollectError:
            continue
        if parsed["result_code"] in OK_CODES and page_of_file(p.name) is not None and not page_mismatch(parsed, page_of_file(p.name)):   # 페이지 넘김이 맞지 않은 원본은 요약에 넣지 않는다 (J5-035)
            items.extend(parsed["items"])
            files.append(p.name)
    return items, files


def _safe_summary(items: list[dict]) -> dict:
    """필드 가용성 요약. 긴 글(주소·건물명 등)의 값 분포는 뺀다 (DIST_MAX_LEN)."""
    s = summarize_items(items)
    for f in s["fields"].values():
        dist = f.get("distribution")
        if dist is not None and any(len(v) > DIST_MAX_LEN for v in dist):
            del f["distribution"]
    return s


def report_from_raw(data_home: Path, run_id: str | None = None) -> dict:
    """한 실행(지정 또는 가장 최근)의 기록 파일과 원본으로 오퍼레이션별 요약을 만든다. 원본 행은 옮기지 않는다."""
    runs = list_run_ids(data_home)
    rid = run_id or (runs[-1] if runs else None)
    if rid is None:
        raise CollectError("no_run", f"건축물대장 수집 기록이 없다 ({RAW_DIR}/run-*.json)")
    path = Path(data_home) / RAW_DIR / f"run-{rid}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CollectError("bad_run", f"수집 기록을 읽을 수 없다: {path.name} ({e})") from e
    targets = doc.get("targets") if isinstance(doc, dict) else None
    if not isinstance(targets, list):
        raise CollectError("bad_run", f"수집 기록 형식이 아니다: {path.name}")
    by_op: dict[str, dict] = {}
    for t in targets:
        if not isinstance(t, dict) or t.get("op") not in OPERATIONS or not isinstance(t.get("pnu"), str):
            continue
        items, files = load_target_items(data_home, t["pnu"], t["op"], rid)
        e = by_op.setdefault(t["op"], {"op": t["op"], "operation": OPERATIONS[t["op"]][0], "label": OPERATIONS[t["op"]][1], "targets": [], "_items": []})
        e["targets"].append({"pnu": t["pnu"], "outcome": t.get("outcome"), "total_count": t.get("total_count"), "items": len(items), "files": len(files)})
        e["_items"].extend(items)
    ops = []
    for op in [o for o in OPERATIONS if o in by_op]:
        e = by_op[op]
        items = e.pop("_items")
        counts: dict[str, int] = {}
        for t in e["targets"]:
            counts[t["outcome"] or "?"] = counts.get(t["outcome"] or "?", 0) + 1
        ops.append({**e, "outcomes": counts, **_safe_summary(items)})
    return {"run_id": rid, "provider": doc.get("provider", PROVIDER), "endpoint_base": doc.get("endpoint_base"), "generated_at": _now(),
            "pnus": len({t.get("pnu") for t in targets if isinstance(t, dict)}), "other_runs": [r for r in runs if r != rid],
            "note": "건축물대장 표본 실측. 원본 행은 raw/ 에 있고 이 요약은 오퍼레이션별 결과·필드 가용성만 담는다(긴 글의 값은 싣지 않음). 정규화·필지 연결은 다음 조각.",
            "operations": ops}


def run_text(run: BrRunResult) -> str:
    lines = [f"건축물대장 표본 수집 [{run.run_id}] {len(run.pnus)}필지 × {len(run.ops)}오퍼레이션 · {run.endpoint_base}", f"인증키 출처: {run.key_source} (키는 기록하지 않음)"]
    for t in run.targets:
        lines.append(f"  {t.pnu} {OPERATIONS[t.op][1]}({t.op}): {t.outcome} · {t.message}")
        for p in t.pages:
            if p.outcome not in ("ok", "empty"):
                lines.append(f"    p{p.page_no}: {p.outcome} {p.error or ''} (HTTP {p.http_status})" + (f" · 본문 {p.error_body_path}" if p.error_body_path else ""))
    lines.append(f"원본·기록: {run.run_path} · 요약: {run.report_path}")
    lines.append("이 수집은 정본 반영이 아니다. 요약(report)만 공유하고 원본 행·인증키는 보내지 않는다.")
    return "\n".join(lines) + "\n"


def report_text(report: dict) -> str:
    lines = [f"건축물대장 표본 요약 [{report['run_id']}] {report['pnus']}필지" + (f" (다른 실행 {len(report['other_runs'])}개는 제외)" if report.get("other_runs") else "")]
    for o in report["operations"]:
        oc = ", ".join(f"{k} {v}" for k, v in sorted(o["outcomes"].items()))
        lines.append(f"  {o['label']}({o['op']}, {o['operation']}): 항목 {o['items']}건 · 필지 결과 {oc}")
        for k, f in o["fields"].items():
            dist = ""
            if "distribution" in f:
                dist = " · " + ", ".join(f"{v or '(빈값)'}:{c}" for v, c in list(f["distribution"].items())[:5])
            lines.append(f"    {k}: 채움 {f['fill_rate']:.0%} · 값 종류 {f['distinct_seen']}{dist}")
    return "\n".join(lines) + "\n"
