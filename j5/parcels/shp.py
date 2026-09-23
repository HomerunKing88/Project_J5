"""ESRI Shapefile(.shp/.dbf/.prj/.cpg) 읽기 (J5-013B-1). 표준 라이브러리만 쓴다.

지원: 도형 Polygon(5)·PolygonZ(15)·PolygonM(25)·Null(0). 입력은 `.shp` 경로(같은 이름의 .dbf 등을 옆에서 찾음) 또는
ZIP(안에서 .shp 한 벌을 찾음). ZIP 은 디스크에 풀지 않고 메모리로 읽으며 항목 크기 상한을 둔다. 입력 파일은 수정하지 않는다.
"""

from __future__ import annotations

import hashlib
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

MAX_MEMBER_BYTES = 800 * 1024 * 1024  # ZIP 항목 하나의 상한 (시·군·구 연속지적도는 수십 MB 급)
MAX_TOTAL_BYTES = 1200 * 1024 * 1024  # 읽어 들이는 항목(.shp/.dbf/.prj/.cpg)의 압축 해제 합계 상한
MAX_RATIO = 200                       # 압축 해제 합계 / 압축 크기 합계 상한 (ZIP 폭탄 차단)
POLYGON_TYPES = {5, 15, 25}
NULL_SHAPE = 0


class ShapeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class DbfField:
    name: str
    type: str
    length: int
    decimals: int


@dataclass
class ShapeSource:
    name: str                 # .shp 의 기본 이름 (확장자 제외)
    origin: str               # 입력 경로 표시용
    shp: bytes
    dbf: bytes
    prj: str | None
    cpg: str | None
    members: dict = field(default_factory=dict)  # 확장자 → 실제 경로/항목 이름

    @property
    def shp_sha256(self) -> str:
        return hashlib.sha256(self.shp).hexdigest()

    @property
    def dbf_sha256(self) -> str:
        return hashlib.sha256(self.dbf).hexdigest()


def _safe_member(name: str) -> bool:
    p = Path(name)
    return not (p.is_absolute() or ".." in p.parts or name.startswith(("/", "\\")))


def open_source(path: Path, *, layer: str | None = None) -> ShapeSource:
    """`.shp` 파일 또는 ZIP 을 읽는다. ZIP 에 .shp 가 여럿이면 --layer(기본 이름)로 고른다."""
    if not path.exists():
        raise ShapeError("not_found", f"입력이 없음: {path}")
    if path.is_file() and zipfile.is_zipfile(path):
        return _open_zip(path, layer)
    if path.suffix.lower() != ".shp":
        raise ShapeError("bad_input", f".shp 파일 또는 ZIP 이어야 한다: {path}")
    base = path.with_suffix("")
    sib = {}
    for ext in (".shp", ".dbf", ".prj", ".cpg"):
        for cand in (base.with_suffix(ext), base.with_suffix(ext.upper())):
            if cand.is_file():
                sib[ext] = cand
                break
    if ".dbf" not in sib:
        raise ShapeError("dbf_missing", f"{path.name} 옆에 .dbf 가 없다")
    return ShapeSource(
        name=base.name, origin=str(path), shp=sib[".shp"].read_bytes(), dbf=sib[".dbf"].read_bytes(),
        prj=sib[".prj"].read_text(encoding="utf-8", errors="replace") if ".prj" in sib else None,
        cpg=sib[".cpg"].read_text(encoding="ascii", errors="replace").strip() if ".cpg" in sib else None,
        members={k: str(v) for k, v in sib.items()},
    )


def _open_zip(path: Path, layer: str | None) -> ShapeSource:
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        for i in infos:
            if not _safe_member(i.filename):
                raise ShapeError("zip_unsafe_path", f"ZIP 항목 경로가 안전하지 않다: {i.filename}")
        shps = [i for i in infos if i.filename.lower().endswith(".shp")]
        if not shps:
            raise ShapeError("shp_missing", "ZIP 안에 .shp 가 없다")
        if layer is not None:
            shps = [i for i in shps if Path(i.filename).stem == layer]
            if not shps:
                raise ShapeError("layer_missing", f"ZIP 안에 --layer {layer!r} 의 .shp 가 없다")
        if len(shps) > 1:
            raise ShapeError("multiple_shp", "ZIP 안에 .shp 가 여럿이다. --layer 로 고른다: " + ", ".join(Path(i.filename).stem for i in shps))
        shp_info = shps[0]
        stem = shp_info.filename[:-4]
        by_ext = {}
        for i in infos:
            low = i.filename.lower()
            if low.startswith(stem.lower() + ".") and low[len(stem):] in (".shp", ".dbf", ".prj", ".cpg"):
                by_ext[low[len(stem):]] = i
        if ".dbf" not in by_ext:
            raise ShapeError("dbf_missing", f"ZIP 안에 {Path(stem).name}.dbf 가 없다")
        # 읽기 전에 선언된 크기로 항목별·누적·압축비 상한을 검사한다 (실제 크기는 읽은 뒤 대조)
        total = sum(i.file_size for i in by_ext.values())
        compressed = sum(i.compress_size for i in by_ext.values())
        if total > MAX_TOTAL_BYTES:
            raise ShapeError("zip_too_big", f"ZIP 의 SHP 항목 합계가 상한을 넘는다: {total} 바이트 > {MAX_TOTAL_BYTES}")
        if total > 1024 * 1024 and total > MAX_RATIO * max(compressed, 1):
            raise ShapeError("zip_ratio", f"ZIP 압축비가 비정상이다 (해제 {total} / 압축 {compressed} 바이트). 과도한 압축 해제를 차단한다")

        def read(info: zipfile.ZipInfo) -> bytes:
            if info.file_size > MAX_MEMBER_BYTES:
                raise ShapeError("zip_member_too_big", f"ZIP 항목이 너무 크다: {info.filename} ({info.file_size} 바이트)")
            data = zf.read(info)
            if len(data) != info.file_size:
                raise ShapeError("zip_size_mismatch", f"ZIP 항목 크기가 헤더와 다르다: {info.filename}")
            return data

        return ShapeSource(
            name=Path(stem).name, origin=f"{path}!{shp_info.filename}", shp=read(by_ext[".shp"]), dbf=read(by_ext[".dbf"]),
            prj=read(by_ext[".prj"]).decode("utf-8", errors="replace") if ".prj" in by_ext else None,
            cpg=read(by_ext[".cpg"]).decode("ascii", errors="replace").strip() if ".cpg" in by_ext else None,
            members={k: v.filename for k, v in by_ext.items()},
        )


# ---------------------------------------------------------------- .shp

@dataclass
class ShpHeader:
    shape_type: int
    bbox: tuple[float, float, float, float]
    file_length: int  # 바이트


def read_shp_header(data: bytes) -> ShpHeader:
    if len(data) < 100:
        raise ShapeError("shp_short", ".shp 가 100 바이트 헤더보다 짧다")
    code, = struct.unpack(">i", data[0:4])
    if code != 9994:
        raise ShapeError("shp_bad_magic", f".shp 파일 코드가 9994 가 아니다: {code}")
    length_words, = struct.unpack(">i", data[24:28])
    version, shape_type = struct.unpack("<ii", data[28:36])
    if version != 1000:
        raise ShapeError("shp_bad_version", f".shp 버전이 1000 이 아니다: {version}")
    xmin, ymin, xmax, ymax = struct.unpack("<4d", data[36:68])
    return ShpHeader(shape_type, (xmin, ymin, xmax, ymax), length_words * 2)


def iter_shapes(data: bytes) -> Iterator[tuple[int, list[list[tuple[float, float]]] | None]]:
    """(레코드 번호, 링 목록 또는 None) 을 순서대로. 링은 (x, y) 목록이며 도형 순서·방향은 원본 그대로다."""
    header = read_shp_header(data)
    if header.shape_type not in POLYGON_TYPES and header.shape_type != NULL_SHAPE:
        raise ShapeError("shp_not_polygon", f"폴리곤 파일이 아니다 (shape type {header.shape_type})")
    end = min(len(data), header.file_length)
    off = 100
    while off + 8 <= end:
        rec_no, content_words = struct.unpack(">ii", data[off:off + 8])
        off += 8
        content_end = off + content_words * 2
        if content_end > len(data):
            raise ShapeError("shp_truncated", f".shp 레코드 {rec_no} 가 파일 끝을 넘는다")
        stype, = struct.unpack("<i", data[off:off + 4])
        if stype == NULL_SHAPE:
            yield rec_no, None
        elif stype in POLYGON_TYPES:
            num_parts, num_points = struct.unpack("<ii", data[off + 36:off + 44])
            p = off + 44
            parts = list(struct.unpack(f"<{num_parts}i", data[p:p + 4 * num_parts]))
            p += 4 * num_parts
            need = p + 16 * num_points
            if need > content_end:
                raise ShapeError("shp_truncated", f".shp 레코드 {rec_no} 의 점 개수가 내용 길이를 넘는다")
            flat = struct.unpack(f"<{2 * num_points}d", data[p:need])
            pts = [(flat[i], flat[i + 1]) for i in range(0, 2 * num_points, 2)]
            bounds = parts + [num_points]
            rings = [pts[bounds[i]:bounds[i + 1]] for i in range(num_parts)]
            yield rec_no, rings
        else:
            raise ShapeError("shp_mixed_type", f".shp 레코드 {rec_no} 의 도형 종류가 폴리곤이 아니다: {stype}")
        off = content_end


# ---------------------------------------------------------------- .dbf

def read_dbf_fields(data: bytes) -> tuple[list[DbfField], int, int, int]:
    """(필드 목록, 레코드 수, 헤더 길이, 레코드 길이)."""
    if len(data) < 32:
        raise ShapeError("dbf_short", ".dbf 가 헤더보다 짧다")
    num_records, header_len, record_len = struct.unpack("<IHH", data[4:12])
    fields = []
    off = 32
    while off + 32 <= header_len and data[off] != 0x0D:
        raw = data[off:off + 32]
        name = raw[0:11].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        fields.append(DbfField(name, chr(raw[11]), raw[16], raw[17]))
        off += 32
    if not fields:
        raise ShapeError("dbf_no_fields", ".dbf 에 필드가 없다")
    if 1 + sum(f.length for f in fields) != record_len:
        raise ShapeError("dbf_bad_record_len", f".dbf 레코드 길이({record_len})가 필드 길이 합과 다르다")
    return fields, num_records, header_len, record_len


def _decode(raw: bytes, encoding: str) -> str:
    return raw.rstrip(b"\x00 ").decode(encoding).strip()


def iter_dbf_records(data: bytes, encoding: str) -> Iterator[dict | None]:
    """레코드를 dict 로. 삭제 표시('*') 레코드는 None. 문자 필드 디코딩 실패는 ShapeError(dbf_decode)."""
    fields, num_records, header_len, record_len = read_dbf_fields(data)
    off = header_len
    for idx in range(num_records):
        rec = data[off:off + record_len]
        if len(rec) < record_len:
            raise ShapeError("dbf_truncated", f".dbf 레코드 {idx + 1} 이 파일 끝을 넘는다")
        off += record_len
        if rec[0:1] == b"*":
            yield None
            continue
        row = {}
        p = 1
        for f in fields:
            raw = rec[p:p + f.length]
            p += f.length
            if f.type == "C":
                try:
                    row[f.name] = _decode(raw, encoding)
                except UnicodeDecodeError as e:
                    raise ShapeError("dbf_decode", f".dbf 레코드 {idx + 1} 필드 {f.name} 을 {encoding} 로 읽지 못했다 ({e.reason}). --encoding 으로 지정한다") from None
            elif f.type in ("N", "F"):
                s = raw.strip().decode("ascii", errors="replace")
                if s in ("", "*" * len(s)):
                    row[f.name] = None
                else:
                    try:
                        row[f.name] = int(s) if f.decimals == 0 and f.type == "N" and "." not in s else float(s)
                    except ValueError:
                        row[f.name] = s
            elif f.type == "L":
                row[f.name] = {b"T": True, b"t": True, b"Y": True, b"y": True, b"F": False, b"f": False, b"N": False, b"n": False}.get(raw[:1])
            elif f.type == "D":
                s = raw.strip().decode("ascii", errors="replace")
                row[f.name] = s or None
            else:
                row[f.name] = raw.hex()
        yield row


def guess_encoding(src: ShapeSource, override: str | None) -> str:
    """--encoding > .cpg > .dbf 언어 구동기 바이트 > cp949(국내 배포 기본)."""
    if override:
        return override
    if src.cpg:
        c = src.cpg.upper().replace("-", "").replace("_", "")
        if c in ("UTF8",):
            return "utf-8"
        if c in ("EUCKR", "CP949", "KSC5601", "949"):
            return "cp949"
        return src.cpg
    ldid = src.dbf[29] if len(src.dbf) > 29 else 0
    if ldid == 0x4D:
        return "cp936"
    if ldid == 0x4E:
        return "cp949"
    if ldid == 0x4F:
        return "cp950"
    return "cp949"
