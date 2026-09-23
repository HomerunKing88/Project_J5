"""상업·업무용 부동산 매매 실거래 API 표본 수집 (J5-014A). 릴리스 계획 §4 R0 통과조건, 데이터 사전 §7.1, ADR-05 [E01·E02].

하는 일: 시군구 코드 5자리·계약월(YYYYMM) 단위로 공식 API 를 호출해 원본 응답을 그대로 `J5_DATA_HOME/raw/rt_nrg/<시군구>/<YYYYMM>/pNNN.xml` 에 두고,
요청·응답 기록(`run-<stamp>.json`, 키 가림)과 필드 가용성·건수·마스킹 요약(`report.json`)을 만든다. 정본에는 쓰지 않는다(정규화·연결은 R3).
하지 않는 일: 응답 해석·PNU 연결·가격 판단. 비공개 API 추측·차단 우회. 인증키 출력.

엔드포인트는 공공데이터포털 API 상세 페이지의 값을 사용자가 확인해 `--endpoint` 로 줄 수 있다(기본값은 코드에 적힌 최신 알려진 주소이며, 실제 응답으로 확인하기 전까지는 가정이다).
결과 코드는 신형(`000`)·구형(`00`) 모두 정상으로 본다. 정상 0건·API 실패·페이지 누락을 구분한다.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from j5.collect.config import redact

DEFAULT_ENDPOINT = "https://apis.data.go.kr/1613000/RTMSDataSvcNrgTrade/getRTMSDataSvcNrgTrade"  # 사용자가 API 상세 페이지에서 확인한다
PROVIDER = "data.go.kr:RTMSDataSvcNrgTrade"
RAW_DIR = Path("raw") / "rt_nrg"
LOG_NAME = "collect.log"
OK_CODES = ("000", "00")
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
DEFAULT_NUM_ROWS = 1000
DEFAULT_MAX_PAGES = 50
TIMEOUT_S = 30
RETRIES = 3
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
LAWD_RE = re.compile(r"^\d{5}$")


class CollectError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class PageResult:
    page_no: int
    url_redacted: str
    fetched_at: str
    elapsed_ms: int = 0
    http_status: int | None = None
    bytes: int = 0
    sha256: str | None = None
    path: str | None = None            # J5_DATA_HOME 기준 상대경로
    result_code: str | None = None
    result_msg: str | None = None
    total_count: int | None = None
    num_rows: int | None = None
    item_count: int = 0
    outcome: str = "failed"            # ok / empty / api_error / http_error / network_error / bad_response
    error: str | None = None
    attempts: int = 1


@dataclass
class MonthResult:
    lawd_cd: str
    deal_ymd: str
    outcome: str = "failed"            # complete / empty / partial / failed
    total_count: int | None = None
    items: int = 0
    pages: list[PageResult] = field(default_factory=list)
    message: str = ""


@dataclass
class RunResult:
    run_id: str
    provider: str
    endpoint: str
    started_at: str
    finished_at: str | None = None
    lawd_cd: str = ""
    months: list[MonthResult] = field(default_factory=list)
    key_source: str = ""
    run_path: str | None = None
    report_path: str | None = None

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "provider": self.provider, "endpoint": self.endpoint, "started_at": self.started_at, "finished_at": self.finished_at,
                "lawd_cd": self.lawd_cd, "key_source": self.key_source, "run_path": self.run_path, "report_path": self.report_path,
                "months": [{"lawd_cd": m.lawd_cd, "deal_ymd": m.deal_ymd, "outcome": m.outcome, "total_count": m.total_count, "items": m.items, "message": m.message,
                            "pages": [p.__dict__ for p in m.pages]} for m in self.months]}

    @property
    def ok(self) -> bool:
        return all(m.outcome in ("complete", "empty") for m in self.months)


# ---------------------------------------------------------------- 입력

def parse_months(text: str) -> list[str]:
    """'2026-08,2021-09,2006-03' → ['202608', '202109', '200603']. 순서 유지, 중복 제거."""
    out: list[str] = []
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        m = MONTH_RE.match(part)
        if not m or not (1 <= int(m.group(2)) <= 12) or int(m.group(1)) < 2006:
            raise CollectError("bad_month", f"계약월은 YYYY-MM (2006-01 이후) 형식: {part!r}")
        ymd = m.group(1) + m.group(2)
        if ymd not in out:
            out.append(ymd)
    if not out:
        raise CollectError("bad_month", "계약월이 없다")
    return out


def check_lawd(lawd_cd: str) -> str:
    if not LAWD_RE.match(lawd_cd or ""):
        raise CollectError("bad_lawd", f"시군구 코드는 5자리 숫자 (예: 종로구 11110): {lawd_cd!r}")
    return lawd_cd


def build_url(endpoint: str, key: str, lawd_cd: str, deal_ymd: str, page_no: int, num_rows: int) -> str:
    """인증키에 '%' 가 있으면 이미 URL 부호화된 키로 보고 그대로 붙인다. 아니면 부호화한다(포털의 Decoding 키)."""
    params = urlencode({"LAWD_CD": lawd_cd, "DEAL_YMD": deal_ymd, "pageNo": page_no, "numOfRows": num_rows})
    k = key if "%" in key else quote(key, safe="")
    sep = "&" if urlsplit(endpoint).query else "?"
    return f"{endpoint}{sep}serviceKey={k}&{params}"


def redacted_url(url: str, key: str) -> str:
    parts = urlsplit(url)
    q = re.sub(r"serviceKey=[^&]*", "serviceKey=<redacted>", parts.query)
    return redact(urlunsplit((parts.scheme, parts.netloc, parts.path, q, parts.fragment)), key)


# ---------------------------------------------------------------- 응답

def parse_response(data: bytes) -> dict:
    """XML 응답의 header/body 를 읽는다. 항목은 태그명→문자열 dict 목록. DOCTYPE·외부 실체는 거절한다."""
    if len(data) > MAX_RESPONSE_BYTES:
        raise CollectError("response_too_big", f"응답이 {len(data)} 바이트로 상한 {MAX_RESPONSE_BYTES} 를 넘는다")
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise CollectError("response_doctype", "응답에 DOCTYPE/ENTITY 선언이 있어 해석하지 않는다")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        snippet = data[:200].decode("utf-8", errors="replace")
        raise CollectError("response_not_xml", f"XML 해석 실패: {e}. 앞부분: {snippet!r}") from e
    out = {"result_code": None, "result_msg": None, "total_count": None, "num_rows": None, "page_no": None, "items": []}
    header = root.find("header")
    if header is not None:
        out["result_code"] = (header.findtext("resultCode") or "").strip() or None
        out["result_msg"] = (header.findtext("resultMsg") or "").strip() or None
    else:
        # 일부 오류 응답은 <OpenAPI_ServiceResponse><cmmMsgHeader>… 형태다
        cmm = root.find(".//cmmMsgHeader")
        if cmm is not None:
            out["result_code"] = (cmm.findtext("returnReasonCode") or "").strip() or None
            out["result_msg"] = (cmm.findtext("returnAuthMsg") or cmm.findtext("errMsg") or "").strip() or None
    body = root.find("body")
    if body is not None:
        for k, tag in (("total_count", "totalCount"), ("num_rows", "numOfRows"), ("page_no", "pageNo")):
            v = (body.findtext(tag) or "").strip()
            out[k] = int(v) if v.isdigit() else None
        items = body.find("items")
        if items is not None:
            for it in items.findall("item"):
                out["items"].append({child.tag: (child.text or "").strip() for child in it})
    return out


def _fetch_once(url: str) -> tuple[int, bytes]:
    req = Request(url, headers={"User-Agent": "j5-collect/0.1 (+standard library urllib)", "Accept": "application/xml, text/xml"})
    with urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - 공식 API 의 https 주소만 쓴다
        return resp.status, resp.read(MAX_RESPONSE_BYTES + 1)


def fetch_page(url: str, key: str, *, opener=None, sleep=time.sleep) -> tuple[PageResult, bytes | None]:
    """한 페이지 요청. 네트워크 오류는 RETRIES 회 재시도(지수 대기). HTTP 오류·API 오류는 재시도하지 않는다."""
    pr = PageResult(page_no=0, url_redacted=redacted_url(url, key), fetched_at=_now())
    fetch = opener or _fetch_once
    started = time.monotonic()
    data: bytes | None = None
    for attempt in range(1, RETRIES + 1):
        pr.attempts = attempt
        try:
            status, data = fetch(url)
            pr.http_status = status
            break
        except HTTPError as e:
            pr.http_status = e.code
            pr.outcome, pr.error = "http_error", redact(f"HTTP {e.code} {e.reason}", key)
            data = None
            break
        except (URLError, TimeoutError, OSError) as e:
            pr.outcome, pr.error = "network_error", redact(f"{type(e).__name__}: {getattr(e, 'reason', e)}", key)
            data = None
            if attempt < RETRIES:
                sleep(2 ** (attempt - 1))
    pr.elapsed_ms = int((time.monotonic() - started) * 1000)
    if data is None:
        return pr, None
    pr.bytes = len(data)
    pr.sha256 = hashlib.sha256(data).hexdigest()
    return pr, data


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- 수집 실행

def collect_months(data_home: Path, *, key: str, key_source: str, lawd_cd: str, months: list[str], endpoint: str = DEFAULT_ENDPOINT,
                   num_rows: int = DEFAULT_NUM_ROWS, max_pages: int = DEFAULT_MAX_PAGES, opener=None, sleep=time.sleep, pause_s: float = 0.2) -> RunResult:
    """월마다 페이지를 끝까지 받아 원본과 기록을 남긴다. 한 달이 실패해도 다음 달을 계속하고 결과에 구분해 적는다."""
    data_home = Path(data_home)
    check_lawd(lawd_cd)
    if not (1 <= num_rows <= 1000) or not (1 <= max_pages <= 500):
        raise CollectError("bad_paging", "numOfRows 는 1~1000, max_pages 는 1~500")
    run = RunResult(run_id=_new_run_id(), provider=PROVIDER, endpoint=endpoint, started_at=_now(), lawd_cd=lawd_cd, key_source=key_source)
    log = data_home / "logs" / LOG_NAME
    log.parent.mkdir(parents=True, exist_ok=True)
    for deal_ymd in months:
        mr = MonthResult(lawd_cd=lawd_cd, deal_ymd=deal_ymd)
        run.months.append(mr)
        out_dir = data_home / RAW_DIR / lawd_cd / deal_ymd
        out_dir.mkdir(parents=True, exist_ok=True)
        page = 1
        while page <= max_pages:
            url = build_url(endpoint, key, lawd_cd, deal_ymd, page, num_rows)
            pr, data = fetch_page(url, key, opener=opener, sleep=sleep)
            pr.page_no = page
            mr.pages.append(pr)
            if data is None:
                _log(log, f"{run.run_id} {lawd_cd} {deal_ymd} p{page} {pr.outcome} {pr.error}", key)
                break
            path = out_dir / f"p{page:03d}-{run.run_id}.xml"
            _write_new(path, data)
            pr.path = path.relative_to(data_home).as_posix()
            try:
                parsed = parse_response(data)
            except CollectError as e:
                # 응답 앞부분이 요청 URL 을 되비칠 수 있어 결과 객체에 넣기 전에 가린다 (기록·화면 출력 모두)
                pr.outcome, pr.error = "bad_response", redact(e.message, key)
                _log(log, f"{run.run_id} {lawd_cd} {deal_ymd} p{page} bad_response {e.code}", key)
                break
            pr.result_code = redact(parsed["result_code"], key) if parsed["result_code"] else None
            pr.result_msg = redact(parsed["result_msg"], key) if parsed["result_msg"] else None
            pr.total_count, pr.num_rows, pr.item_count = parsed["total_count"], parsed["num_rows"], len(parsed["items"])
            if pr.result_code not in OK_CODES:
                pr.outcome, pr.error = "api_error", f"resultCode {pr.result_code}: {pr.result_msg}"
                _log(log, f"{run.run_id} {lawd_cd} {deal_ymd} p{page} api_error {pr.result_code} {pr.result_msg}", key)
                break
            pr.outcome = "ok" if pr.item_count else "empty"
            mr.items += pr.item_count
            if mr.total_count is None:
                mr.total_count = pr.total_count
            _log(log, f"{run.run_id} {lawd_cd} {deal_ymd} p{page} {pr.outcome} items={pr.item_count} total={pr.total_count} bytes={pr.bytes}", key)
            if pr.total_count is None or pr.item_count == 0 or mr.items >= pr.total_count:
                break
            page += 1
            sleep(pause_s)
        last = mr.pages[-1] if mr.pages else None
        if last is None or last.outcome in ("http_error", "network_error", "bad_response", "api_error"):
            mr.outcome = "failed"
            mr.message = f"실패: {last.outcome if last else '요청 없음'} ({last.error if last else ''})"
        elif mr.total_count == 0:
            mr.outcome = "empty"
            mr.message = "정상 응답, 0건 (API 실패가 아님)"
        elif mr.total_count is None:
            mr.outcome = "partial"
            mr.message = f"totalCount 가 없거나 숫자가 아니라 완전성을 확인할 수 없다 ({mr.items}건 받음). 응답 형식을 확인한다"
        elif mr.items < mr.total_count:
            mr.outcome = "partial"
            mr.message = f"페이지 누락: totalCount {mr.total_count} 중 {mr.items}건 (max_pages {max_pages} 또는 빈 페이지)"
        else:
            mr.outcome = "complete"
            mr.message = f"{mr.items}건 ({len(mr.pages)}페이지)"
    run.finished_at = _now()
    run_path = data_home / RAW_DIR / lawd_cd / f"run-{run.run_id}.json"
    _write_new(run_path, (redact(json.dumps(run.to_dict(), ensure_ascii=False, indent=2), key) + "\n").encode("utf-8"))
    run.run_path = run_path.relative_to(data_home).as_posix()
    report = build_report(data_home, run)
    report_path = data_home / RAW_DIR / lawd_cd / f"report-{run.run_id}.json"
    _write_new(report_path, (redact(json.dumps(report, ensure_ascii=False, indent=2), key) + "\n").encode("utf-8"))
    run.report_path = report_path.relative_to(data_home).as_posix()
    return run


def _new_run_id() -> str:
    """UTC 초 단위 시각 + 무작위 6자리. 같은 초의 실행·동시 실행이 같은 이름을 갖지 않게 한다."""
    return _now().replace("-", "").replace(":", "").replace("T", "-").rstrip("Z") + "-" + secrets.token_hex(3)


def _write_new(path: Path, data: bytes) -> None:
    """원본·기록은 덮어쓰지 않는다. 같은 이름이 있으면 실패한다 (배타적 생성)."""
    try:
        with open(path, "xb") as f:
            f.write(data)
    except FileExistsError:
        raise CollectError("raw_exists", f"이미 있는 파일을 덮어쓰지 않는다: {path}") from None


def _log(log: Path, line: str, key: str) -> None:
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{_now()} {redact(line, key)}\n")


# ---------------------------------------------------------------- 요약

def summarize_items(items: list[dict]) -> dict:
    """필드 가용성(채움 비율), 낮은 카디널리티 필드의 분포, `*` 마스킹 비율. 개별 행은 옮기지 않는다."""
    n = len(items)
    fields: dict[str, dict] = {}
    masked_rows = 0
    for it in items:
        if any("*" in v for v in it.values()):
            masked_rows += 1
        for k, v in it.items():
            f = fields.setdefault(k, {"filled": 0, "masked": 0, "values": {}})
            if v != "":
                f["filled"] += 1
            if "*" in v:
                f["masked"] += 1
            if len(f["values"]) < 200:
                f["values"][v] = f["values"].get(v, 0) + 1
    out_fields = {}
    for k, f in sorted(fields.items()):
        distinct = len(f["values"])
        entry = {"filled": f["filled"], "fill_rate": round(f["filled"] / n, 3) if n else 0.0, "masked": f["masked"], "distinct_seen": distinct}
        if distinct <= 20:
            entry["distribution"] = dict(sorted(f["values"].items(), key=lambda kv: (-kv[1], kv[0])))
        out_fields[k] = entry
    return {"items": n, "masked_rows": masked_rows, "masked_rate": round(masked_rows / n, 3) if n else 0.0, "fields": out_fields}


RUN_SUFFIX_RE = re.compile(r"^p\d{3}-(\d{8}-\d{6}(?:-[0-9a-f]{6})?)\.xml$")


def list_runs(data_home: Path, lawd_cd: str, deal_ymd: str) -> list[str]:
    """그 달 폴더에 원본이 있는 실행 ID 목록 (오래된 것부터). 같은 초의 실행은 무작위 접미사가 아니라 파일 생성 시각으로 순서를 정한다."""
    d = Path(data_home) / RAW_DIR / lawd_cd / deal_ymd
    if not d.is_dir():
        return []
    first_mtime: dict[str, int] = {}
    for p in d.glob("p*.xml"):
        m = RUN_SUFFIX_RE.match(p.name)
        if not m:
            continue
        ns = p.stat().st_mtime_ns
        first_mtime[m.group(1)] = min(first_mtime.get(m.group(1), ns), ns)
    return sorted(first_mtime, key=lambda rid: (rid[:15], first_mtime[rid]))


def load_month_items(data_home: Path, lawd_cd: str, deal_ymd: str, run_id: str | None = None) -> tuple[list[dict], list[str]]:
    """저장된 원본 XML 에서 항목을 다시 읽는다 (네트워크 없음). 한 실행의 파일만 읽는다: run_id 가 없으면 가장 최근 실행.
    여러 실행을 합치면 같은 거래가 여러 번 세어지므로 합치지 않는다."""
    d = Path(data_home) / RAW_DIR / lawd_cd / deal_ymd
    items: list[dict] = []
    files: list[str] = []
    if not d.is_dir():
        return items, files
    if run_id is None:
        runs = list_runs(data_home, lawd_cd, deal_ymd)
        if not runs:
            return items, files
        run_id = runs[-1]
    for p in sorted(d.glob("p*.xml")):
        if not p.name.endswith(f"-{run_id}.xml"):
            continue
        try:
            parsed = parse_response(p.read_bytes())
        except CollectError:
            continue
        if parsed["result_code"] in OK_CODES:
            items.extend(parsed["items"])
            files.append(p.name)
    return items, files


def build_report(data_home: Path, run: RunResult) -> dict:
    months = []
    for m in run.months:
        items, files = load_month_items(data_home, m.lawd_cd, m.deal_ymd, run.run_id)
        months.append({"deal_ymd": m.deal_ymd, "outcome": m.outcome, "total_count": m.total_count, "files": files, **summarize_items(items)})
    return {"run_id": run.run_id, "provider": run.provider, "endpoint": run.endpoint, "lawd_cd": run.lawd_cd, "generated_at": _now(),
            "note": "표본 월 실측(R0). 원본 행은 raw/ 에 있고 이 요약은 필드 가용성·건수·마스킹만 담는다. 정규화·연결은 R3.", "months": months}


def report_from_raw(data_home: Path, lawd_cd: str, months: list[str], run_id: str | None = None) -> dict:
    """네트워크 없이 저장된 원본만으로 요약한다. 월마다 한 실행(지정한 실행 또는 가장 최근 실행)만 센다."""
    out = []
    for deal_ymd in months:
        runs = list_runs(data_home, lawd_cd, deal_ymd)
        chosen = run_id if run_id is not None else (runs[-1] if runs else None)
        items, files = load_month_items(data_home, lawd_cd, deal_ymd, chosen) if chosen else ([], [])
        out.append({"deal_ymd": deal_ymd, "outcome": "from_raw" if files else "no_raw", "run_id": chosen if files else None, "other_runs": [r for r in runs if r != chosen],
                    "total_count": None, "files": files, **summarize_items(items)})
    return {"run_id": run_id, "provider": PROVIDER, "endpoint": None, "lawd_cd": lawd_cd, "generated_at": _now(),
            "note": "저장된 원본에서 다시 요약 (네트워크 없음). 월마다 한 실행만 센다", "months": out}


def run_text(run: RunResult) -> str:
    lines = [f"실거래 표본 수집 [{run.run_id}] 시군구 {run.lawd_cd} · {run.endpoint}", f"인증키 출처: {run.key_source} (키는 기록하지 않음)"]
    for m in run.months:
        lines.append(f"  {m.deal_ymd[:4]}-{m.deal_ymd[4:]}: {m.outcome} · {m.message}")
        for p in m.pages:
            if p.outcome not in ("ok", "empty"):
                lines.append(f"    p{p.page_no}: {p.outcome} {p.error or ''} (HTTP {p.http_status})")
    lines.append(f"원본·기록: {run.run_path} · 요약: {run.report_path}")
    lines.append("이 수집은 정본 반영이 아니다. 요약(report)만 공유하고 원본 행·인증키는 보내지 않는다.")
    return "\n".join(lines) + "\n"


def report_text(report: dict) -> str:
    lines = [f"실거래 표본 요약 시군구 {report['lawd_cd']} ({report.get('run_id') or '원본 재요약'})"]
    for m in report["months"]:
        extra = f" · 실행 {m['run_id']}" + (f" (다른 실행 {len(m['other_runs'])}개는 제외)" if m.get("other_runs") else "") if m.get("run_id") else ""
        lines.append(f"  {m['deal_ymd'][:4]}-{m['deal_ymd'][4:]}: {m['outcome']} · 항목 {m['items']}건 (totalCount {m['total_count']}) · 마스킹 행 {m['masked_rows']} ({m['masked_rate']:.0%}) · 파일 {len(m['files'])}{extra}")
        for k, f in m["fields"].items():
            dist = ""
            if "distribution" in f:
                top = list(f["distribution"].items())[:5]
                dist = " · " + ", ".join(f"{v or '(빈값)'}:{c}" for v, c in top)
            lines.append(f"    {k}: 채움 {f['fill_rate']:.0%} · 마스킹 {f['masked']} · 값 종류 {f['distinct_seen']}{dist}")
    return "\n".join(lines) + "\n"
