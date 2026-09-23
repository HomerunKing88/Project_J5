"""좌표계 (J5-013B-1). 한국 평면직각좌표계(횡축 메르카토르, TM)의 WGS84 경위도 역변환을 표준 라이브러리로 계산한다.

- TM 정·역변환: Krüger 급수(n 의 6차, Karney 2011). 한반도 원점대에서 mm 이하 정밀도.
- KGD2002(GRS80) 계열은 WGS84 와 같은 타원체·데이텀으로 취급한다(EPSG 의 "KGD2002 to WGS 84 (1)" 은 항등).
- Korean 1985(Bessel) 계열은 EPSG "Korean 1985 to WGS 84 (1)"(Molodensky-Badekas, coordinate frame) 매개변수로 옮긴다.
  이 변환의 공식 정확도는 수 m 급이며, 실제 자료에서 GRS80 계열이 아닌 파일은 `j5 parcels inspect` 결과를 사용자가 확인한다.

.prj(ESRI WKT1) 를 읽어 매개변수를 뽑고, 알려진 EPSG 표와 대조한다. 대조에 실패하면 `--crs EPSG:xxxx` 를 요구한다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# 타원체: (장반경 a, 편평률 역수 1/f)
ELLIPSOIDS = {
    "GRS80": (6378137.0, 298.257222101),
    "WGS84": (6378137.0, 298.257223563),
    "Bessel": (6377397.155, 299.1528128),
}

# EPSG "Korean 1985 to WGS 84 (1)" (Molodensky-Badekas, coordinate frame 회전, m·초·ppm, 회전 중심 m)
KOREAN_1985_TO_WGS84 = {
    "tx": -145.907, "ty": 505.034, "tz": 685.756,
    "rx": -1.162, "ry": 2.347, "rz": 1.592, "ds": 6.342,
    "px": -3159521.31, "py": 4068151.32, "pz": 3748113.85,
}


class CrsError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class TmCrs:
    name: str
    epsg: int | None
    ellipsoid: str           # ELLIPSOIDS 의 키
    lat0: float              # 도
    lon0: float              # 도
    k0: float
    fe: float
    fn: float
    datum_shift: str | None  # None(항등) 또는 "korean1985"

    @property
    def geographic(self) -> bool:
        return False


@dataclass(frozen=True)
class GeographicCrs:
    name: str
    epsg: int | None
    ellipsoid: str
    datum_shift: str | None

    @property
    def geographic(self) -> bool:
        return True


def _tm(name: str, epsg: int, ellps: str, lon0: float, fn: float, *, lat0: float = 38.0, k0: float = 1.0, fe: float = 200000.0) -> TmCrs:
    return TmCrs(name, epsg, ellps, lat0, lon0, k0, fe, fn, "korean1985" if ellps == "Bessel" else None)


_MOD = 127.002890277778 - 127.0  # Korean 1985 수정 원점 경도 보정 (10.405")
KNOWN: dict[int, TmCrs | GeographicCrs] = {
    4326: GeographicCrs("WGS 84", 4326, "WGS84", None),
    4737: GeographicCrs("KGD2002", 4737, "GRS80", None),
    4162: GeographicCrs("Korean 1985", 4162, "Bessel", "korean1985"),
    5173: _tm("Korean 1985 / Modified West Belt", 5173, "Bessel", 125.0 + _MOD, 500000.0),
    5174: _tm("Korean 1985 / Modified Central Belt", 5174, "Bessel", 127.0 + _MOD, 500000.0),
    5175: _tm("Korean 1985 / Modified Central Belt Jeju", 5175, "Bessel", 127.0 + _MOD, 550000.0),
    5176: _tm("Korean 1985 / Modified East Belt", 5176, "Bessel", 129.0 + _MOD, 500000.0),
    5177: _tm("Korean 1985 / Modified East Sea Belt", 5177, "Bessel", 131.0 + _MOD, 500000.0),
    5178: _tm("Korean 1985 / Unified CS", 5178, "Bessel", 127.5, 2000000.0, k0=0.9996, fe=1000000.0),
    5179: _tm("KGD2002 / Unified CS", 5179, "GRS80", 127.5, 2000000.0, k0=0.9996, fe=1000000.0),
    5180: _tm("KGD2002 / West Belt", 5180, "GRS80", 125.0, 500000.0),
    5181: _tm("KGD2002 / Central Belt", 5181, "GRS80", 127.0, 500000.0),
    5182: _tm("KGD2002 / Central Belt Jeju", 5182, "GRS80", 127.0, 550000.0),
    5183: _tm("KGD2002 / East Belt", 5183, "GRS80", 129.0, 500000.0),
    5184: _tm("KGD2002 / East Sea Belt", 5184, "GRS80", 131.0, 500000.0),
    5185: _tm("KGD2002 / West Belt 2010", 5185, "GRS80", 125.0, 600000.0),
    5186: _tm("KGD2002 / Central Belt 2010", 5186, "GRS80", 127.0, 600000.0),
    5187: _tm("KGD2002 / East Belt 2010", 5187, "GRS80", 129.0, 600000.0),
    5188: _tm("KGD2002 / East Sea Belt 2010", 5188, "GRS80", 131.0, 600000.0),
}


# ---------------------------------------------------------------- TM (Krüger 급수)

class _Tm:
    def __init__(self, crs: TmCrs):
        a, rf = ELLIPSOIDS[crs.ellipsoid]
        f = 1.0 / rf
        n = f / (2.0 - f)
        self.n = n
        self.e2 = f * (2.0 - f)
        self.A = a / (1.0 + n) * (1.0 + n ** 2 / 4.0 + n ** 4 / 64.0 + n ** 6 / 256.0)
        self.alpha = (
            n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16 + 41 * n ** 4 / 180 - 127 * n ** 5 / 288 + 7891 * n ** 6 / 37800,
            13 * n ** 2 / 48 - 3 * n ** 3 / 5 + 557 * n ** 4 / 1440 + 281 * n ** 5 / 630 - 1983433 * n ** 6 / 1935360,
            61 * n ** 3 / 240 - 103 * n ** 4 / 140 + 15061 * n ** 5 / 26880 + 167603 * n ** 6 / 181440,
            49561 * n ** 4 / 161280 - 179 * n ** 5 / 168 + 6601661 * n ** 6 / 7257600,
            34729 * n ** 5 / 80640 - 3418889 * n ** 6 / 1995840,
            212378941 * n ** 6 / 319334400,
        )
        self.beta = (
            n / 2 - 2 * n ** 2 / 3 + 37 * n ** 3 / 96 - n ** 4 / 360 - 81 * n ** 5 / 512 + 96199 * n ** 6 / 604800,
            n ** 2 / 48 + n ** 3 / 15 - 437 * n ** 4 / 1440 + 46 * n ** 5 / 105 - 1118711 * n ** 6 / 3870720,
            17 * n ** 3 / 480 - 37 * n ** 4 / 840 - 209 * n ** 5 / 4480 + 5569 * n ** 6 / 90720,
            4397 * n ** 4 / 161280 - 11 * n ** 5 / 504 - 830251 * n ** 6 / 7257600,
            4583 * n ** 5 / 161280 - 108847 * n ** 6 / 3991680,
            20648693 * n ** 6 / 638668800,
        )
        self.delta = (
            2 * n - 2 * n ** 2 / 3 - 2 * n ** 3 + 116 * n ** 4 / 45 + 26 * n ** 5 / 45 - 2854 * n ** 6 / 675,
            7 * n ** 2 / 3 - 8 * n ** 3 / 5 - 227 * n ** 4 / 45 + 2704 * n ** 5 / 315 + 2323 * n ** 6 / 945,
            56 * n ** 3 / 15 - 136 * n ** 4 / 35 - 1262 * n ** 5 / 105 + 73814 * n ** 6 / 2835,
            4279 * n ** 4 / 630 - 332 * n ** 5 / 35 - 399572 * n ** 6 / 14175,
            4174 * n ** 5 / 315 - 144838 * n ** 6 / 6237,
            601676 * n ** 6 / 22275,
        )
        self.crs = crs
        self.lon0 = math.radians(crs.lon0)
        # 원점 위도의 자오선 호장 (k0·A·xi)
        self.m0 = self._xi_eta(math.radians(crs.lat0), 0.0)[0] * self.A

    def _xi_eta(self, lat: float, dlon: float) -> tuple[float, float]:
        n = self.n
        c = 2.0 * math.sqrt(n) / (1.0 + n)
        s = math.sin(lat)
        t = math.sinh(math.atanh(s) - c * math.atanh(c * s))
        xi_p = math.atan2(t, math.cos(dlon))
        eta_p = math.atanh(math.sin(dlon) / math.sqrt(1.0 + t * t))
        xi, eta = xi_p, eta_p
        for j, a in enumerate(self.alpha, start=1):
            xi += a * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
            eta += a * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
        return xi, eta

    def forward(self, lon: float, lat: float) -> tuple[float, float]:
        xi, eta = self._xi_eta(math.radians(lat), math.radians(lon) - self.lon0)
        k0 = self.crs.k0
        return self.crs.fe + k0 * self.A * eta, self.crs.fn + k0 * (self.A * xi - self.m0)

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        k0 = self.crs.k0
        xi = (y - self.crs.fn + k0 * self.m0) / (k0 * self.A)
        eta = (x - self.crs.fe) / (k0 * self.A)
        xi_p, eta_p = xi, eta
        for j, b in enumerate(self.beta, start=1):
            xi_p -= b * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
            eta_p -= b * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
        chi = math.asin(math.sin(xi_p) / math.cosh(eta_p))
        lat = chi
        for j, d in enumerate(self.delta, start=1):
            lat += d * math.sin(2 * j * chi)
        lon = self.lon0 + math.atan2(math.sinh(eta_p), math.cos(xi_p))
        return math.degrees(lon), math.degrees(lat)


# ---------------------------------------------------------------- 데이텀 (Bessel → WGS84)

def _geodetic_to_cart(lon: float, lat: float, ellps: str) -> tuple[float, float, float]:
    a, rf = ELLIPSOIDS[ellps]
    f = 1.0 / rf
    e2 = f * (2.0 - f)
    la, lo = math.radians(lat), math.radians(lon)
    nu = a / math.sqrt(1.0 - e2 * math.sin(la) ** 2)
    return nu * math.cos(la) * math.cos(lo), nu * math.cos(la) * math.sin(lo), nu * (1.0 - e2) * math.sin(la)


def _cart_to_geodetic(x: float, y: float, z: float, ellps: str) -> tuple[float, float]:
    a, rf = ELLIPSOIDS[ellps]
    f = 1.0 / rf
    e2 = f * (2.0 - f)
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - e2))
    for _ in range(10):
        nu = a / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
        new = math.atan2(z + e2 * nu * math.sin(lat), p)
        if abs(new - lat) < 1e-14:
            lat = new
            break
        lat = new
    return math.degrees(lon), math.degrees(lat)


def korean1985_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """Molodensky-Badekas (coordinate frame 회전). 높이 0 으로 취급한다."""
    p = KOREAN_1985_TO_WGS84
    x, y, z = _geodetic_to_cart(lon, lat, "Bessel")
    dx, dy, dz = x - p["px"], y - p["py"], z - p["pz"]
    rx, ry, rz = (math.radians(v / 3600.0) for v in (p["rx"], p["ry"], p["rz"]))
    m = 1.0 + p["ds"] * 1e-6
    # coordinate frame: X' = P + T + m·R·(X − P), R = [[1, rz, −ry], [−rz, 1, rx], [ry, −rx, 1]]
    xo = p["px"] + p["tx"] + m * (dx + rz * dy - ry * dz)
    yo = p["py"] + p["ty"] + m * (-rz * dx + dy + rx * dz)
    zo = p["pz"] + p["tz"] + m * (ry * dx - rx * dy + dz)
    return _cart_to_geodetic(xo, yo, zo, "WGS84")


# ---------------------------------------------------------------- 공개 API

class Transformer:
    """원본 좌표계 → WGS84 경위도. `to_wgs84(x, y)` 는 (lon, lat) 도 단위."""

    def __init__(self, crs: TmCrs | GeographicCrs):
        self.crs = crs
        self._tm = None if crs.geographic else _Tm(crs)

    def to_wgs84(self, x: float, y: float) -> tuple[float, float]:
        lon, lat = (x, y) if self._tm is None else self._tm.inverse(x, y)
        if self.crs.datum_shift == "korean1985":
            lon, lat = korean1985_to_wgs84(lon, lat)
        return lon, lat

    def describe(self) -> dict:
        c = self.crs
        return {"epsg": c.epsg, "name": c.name, "ellipsoid": c.ellipsoid, "datum_shift": c.datum_shift, "geographic": c.geographic}


def forward_tm(crs: TmCrs, lon: float, lat: float) -> tuple[float, float]:
    """WGS84 경위도(데이텀 변환 없음, GRS80 계열용) → 평면 좌표. 시험·가상 fixture 생성용."""
    return _Tm(crs).forward(lon, lat)


def from_epsg(code: int) -> TmCrs | GeographicCrs:
    if code not in KNOWN:
        raise CrsError("unknown_epsg", f"EPSG:{code} 는 이 도구가 아는 한국 좌표계 목록에 없다: {sorted(KNOWN)}")
    return KNOWN[code]


def parse_crs_arg(text: str) -> TmCrs | GeographicCrs:
    m = re.fullmatch(r"(?i)\s*(?:epsg:)?\s*(\d{4,5})\s*", text or "")
    if not m:
        raise CrsError("bad_crs_arg", f"--crs 는 EPSG:5186 형식이어야 한다: {text!r}")
    return from_epsg(int(m.group(1)))


_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


def parse_prj(wkt: str) -> TmCrs | GeographicCrs:
    """ESRI WKT1(.prj) 에서 TM 매개변수·타원체를 읽고 알려진 EPSG 와 대조한다. 대조 실패 시 CrsError."""
    text = wkt.strip()
    if not text:
        raise CrsError("prj_empty", ".prj 가 비어 있다")
    sph = re.search(r'SPHEROID\[\s*"([^"]*)"\s*,\s*(' + _NUM + r")\s*,\s*(" + _NUM + r")", text)
    if not sph:
        raise CrsError("prj_no_spheroid", ".prj 에서 SPHEROID 를 찾지 못했다")
    a, rf = float(sph.group(2)), float(sph.group(3))
    ellps = None
    for name, (ea, erf) in ELLIPSOIDS.items():
        if abs(ea - a) < 0.01 and abs(erf - rf) < 1e-6:
            ellps = name
            break
    if ellps is None:
        raise CrsError("prj_unknown_ellipsoid", f"모르는 타원체 (a={a}, 1/f={rf}): {sph.group(1)}")
    proj = re.search(r'PROJECTION\[\s*"([^"]*)"', text)
    if proj is None:
        # 지리좌표계
        if ellps == "Bessel":
            return KNOWN[4162]
        return KNOWN[4326] if ellps == "WGS84" else KNOWN[4737]
    if proj.group(1).lower().replace(" ", "_") not in ("transverse_mercator", "gauss_kruger"):
        raise CrsError("prj_not_tm", f"횡축 메르카토르가 아닌 투영: {proj.group(1)}")
    params = {m.group(1).lower(): float(m.group(2)) for m in re.finditer(r'PARAMETER\[\s*"([^"]*)"\s*,\s*(' + _NUM + r")", text)}
    try:
        lon0, lat0 = params["central_meridian"], params["latitude_of_origin"]
        k0, fe, fn = params["scale_factor"], params["false_easting"], params["false_northing"]
    except KeyError as e:
        raise CrsError("prj_missing_parameter", f".prj 에 {e.args[0]} 가 없다") from None
    for crs in KNOWN.values():
        if crs.geographic or crs.ellipsoid != ellps:
            continue
        if (abs(crs.lon0 - lon0) < 1e-6 and abs(crs.lat0 - lat0) < 1e-9 and abs(crs.k0 - k0) < 1e-9
                and abs(crs.fe - fe) < 0.5 and abs(crs.fn - fn) < 0.5):
            return crs
    raise CrsError("prj_unmatched", f".prj 의 매개변수(타원체 {ellps}, 중앙자오선 {lon0}, 원점위도 {lat0}, 축척 {k0}, FE {fe}, FN {fn})가 "
                   f"알려진 EPSG 표와 맞지 않는다. --crs EPSG:xxxx 로 지정한다")
