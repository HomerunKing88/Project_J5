// 최소 지도 (J5-005, ADR-12): 자체 SVG 점 지도. Web Mercator 투영으로 위치점(location_point)만 그린다.
// 배경 타일·외부 통신 없음. 위 순수 함수는 DOM 없이 단위 테스트하고, createMap 만 SVG 를 만진다.
// 화면 좌표 = 세계 좌표(0~1) × scale + (tx, ty). 마커는 픽셀 단위라 확대해도 크기가 변하지 않는다.

export const WORLD_METERS = 40075016.686; // WGS84 적도 둘레
export const MIN_SCALE = 256; // z0: 세계 전체 = 256px
export const MAX_SCALE = 256 * 2 ** 22; // z22
export const FIT_MAX_SCALE = 256 * 2 ** 17; // 점 하나·극소 범위일 때의 기본 확대 (37.6° 에서 약 0.95 m/px)
export const FIT_PAD = 40; // 전체 보기 여백(px). 라벨이 오른쪽으로 뻗는다.
export const MAX_LAT = 85.05112878;
export const SCALE_STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];
const DOT_R = 7, DOT_R_SEL = 10, HIT_R = 18, TAP_PX = 6, LABEL_MAX = 12;

export function mercator([lon, lat]) {
  const la = (Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) * Math.PI) / 180;
  const y = (1 - Math.log(Math.tan(la) + 1 / Math.cos(la)) / Math.PI) / 2;
  return { x: (lon + 180) / 360, y: Math.max(0, Math.min(1, y)) };
}

export function unmercator({ x, y }) {
  return [x * 360 - 180, (Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180) / Math.PI];
}

export function clampScale(s) {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, s));
}

/** 세계 좌표 점들을 여백 안에 모두 넣는 뷰. 점이 없으면 세계 중심, 범위가 0이면 FIT_MAX_SCALE. */
export function fitView(points, w, h, pad = FIT_PAD) {
  if (!points.length) return { scale: MIN_SCALE, tx: w / 2 - 0.5 * MIN_SCALE, ty: h / 2 - 0.5 * MIN_SCALE };
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of points) { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); }
  const dx = maxX - minX, dy = maxY - minY;
  const sx = dx > 0 ? (w - 2 * pad) / dx : Infinity;
  const sy = dy > 0 ? (h - 2 * pad) / dy : Infinity;
  const scale = clampScale(Math.min(sx, sy, FIT_MAX_SCALE));
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  return { scale, tx: w / 2 - cx * scale, ty: h / 2 - cy * scale };
}

export function toScreen(p, view) {
  return { x: p.x * view.scale + view.tx, y: p.y * view.scale + view.ty };
}

export function toWorld(px, py, view) {
  return { x: (px - view.tx) / view.scale, y: (py - view.ty) / view.scale };
}

/** 화면 점 (px, py) 를 고정한 채 factor 배 확대. */
export function zoomAt(view, factor, px, py) {
  const scale = clampScale(view.scale * factor);
  const f = scale / view.scale;
  return { scale, tx: px - (px - view.tx) * f, ty: py - (py - view.ty) * f };
}

export function pan(view, dx, dy) {
  return { scale: view.scale, tx: view.tx + dx, ty: view.ty + dy };
}

export function metersPerPixel(lat, view) {
  return (WORLD_METERS * Math.cos((lat * Math.PI) / 180)) / view.scale;
}

/** maxPx 이하로 들어가는 가장 긴 축척 막대. */
export function scaleBar(lat, view, maxPx = 120) {
  const mpp = metersPerPixel(lat, view);
  let meters = SCALE_STEPS[0];
  for (const m of SCALE_STEPS) if (m / mpp <= maxPx) meters = m;
  return { meters, px: meters / mpp };
}

export function locatedAssets(assets) {
  return assets.filter((a) => Array.isArray(a.location_point) && a.location_point.length === 2 && a.location_point.every((v) => Number.isFinite(v)));
}

export function shortLabel(label, max = LABEL_MAX) {
  const cps = Array.from(String(label));
  return cps.length > max ? cps.slice(0, max).join("") + "…" : String(label);
}

/**
 * SVG 점 지도. svgEl 은 index.html 의 정적 <svg>. onSelect(asset) 는 점을 탭했을 때.
 * 실패(예외)는 호출자가 잡아 목록만으로 동작하게 한다.
 */
export function createMap(svgEl, { onSelect } = {}) {
  if (!svgEl || svgEl.namespaceURI == null || svgEl.tagName?.toLowerCase() !== "svg") throw new Error("SVG 요소가 아님");
  const NS = svgEl.namespaceURI;
  const node = (tag, attrs = {}) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    return n;
  };
  const layerPts = node("g", { class: "layer-pts" });
  const layerScale = node("g", { class: "layer-scale" });
  const scaleLine = node("line", { x1: 10, y1: 0, x2: 10, y2: 0 });
  const scaleText = node("text", { x: 10, y: 0 });
  layerScale.append(scaleLine, scaleText);
  svgEl.replaceChildren(layerPts, layerScale);

  let view = fitView([], 320, 280);
  let size = { w: 320, h: 280 };
  const markers = new Map(); // asset_id → { g, dot, world, asset }
  let selectedId = null;
  const pointers = new Map();
  let downTarget = null, moved = 0;

  const measure = () => {
    const r = svgEl.getBoundingClientRect();
    size = { w: r.width || 320, h: r.height || 280 };
    return size;
  };
  const local = (e) => { const r = svgEl.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };

  const render = () => {
    for (const m of markers.values()) {
      const p = toScreen(m.world, view);
      m.g.setAttribute("transform", `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})`);
    }
    const lat = unmercator(toWorld(size.w / 2, size.h / 2, view))[1];
    const bar = scaleBar(lat, view);
    const y = size.h - 10;
    scaleLine.setAttribute("y1", y); scaleLine.setAttribute("y2", y); scaleLine.setAttribute("x2", 10 + bar.px);
    scaleText.setAttribute("y", y - 6);
    scaleText.textContent = bar.meters >= 1000 ? `${bar.meters / 1000} km` : `${bar.meters} m`;
  };

  const setClass = (m) => m.g.setAttribute("class", `pt ${m.asset.data_mode}${m.asset.asset_id === selectedId ? " sel" : ""}`);

  const api = {
    /** 마커를 전부 다시 만들고 전체 보기. 위치점 없는 물건은 그리지 않는다. */
    setAssets(assets) {
      markers.clear();
      layerPts.replaceChildren();
      const located = locatedAssets(assets);
      for (const asset of located) {
        const g = node("g", { "data-asset-id": asset.asset_id });
        const hit = node("circle", { class: "hit", r: HIT_R });
        const dot = node("circle", { class: "dot", r: DOT_R });
        const label = node("text", { x: 11, y: 4 });
        label.textContent = shortLabel(asset.label);
        const title = node("title");
        title.textContent = `${asset.label} (${asset.data_mode})`;
        g.append(title, hit, dot, label);
        layerPts.append(g);
        markers.set(asset.asset_id, { g, dot, world: mercator(asset.location_point), asset });
      }
      api.select(selectedId);
      api.fit();
      return {
        total: assets.length, located: located.length, unlocated: assets.length - located.length,
        synthetic: located.filter((a) => a.data_mode === "synthetic").length,
        privateReal: located.filter((a) => a.data_mode === "private_real").length,
      };
    },
    select(assetId) {
      selectedId = assetId ?? null;
      for (const m of markers.values()) {
        setClass(m);
        m.dot.setAttribute("r", m.asset.asset_id === selectedId ? DOT_R_SEL : DOT_R);
      }
    },
    fit() {
      measure();
      view = fitView([...markers.values()].map((m) => m.world), size.w, size.h);
      render();
    },
    zoomBy(factor) {
      measure();
      view = zoomAt(view, factor, size.w / 2, size.h / 2);
      render();
    },
    resize() {
      // 크기가 바뀌어도 화면 중심의 세계 좌표를 유지한다 (기기 회전).
      const c = toWorld(size.w / 2, size.h / 2, view);
      measure();
      view = { scale: view.scale, tx: size.w / 2 - c.x * view.scale, ty: size.h / 2 - c.y * view.scale };
      render();
    },
    getView() { return { ...view }; },
    destroy() {
      for (const [type, fn, opts] of listeners) svgEl.removeEventListener(type, fn, opts);
      window.removeEventListener("resize", api.resize);
      svgEl.replaceChildren();
      markers.clear();
    },
  };

  // ---- 포인터: 드래그(1개), 핀치(2개), 탭(이동 6px 미만) ----
  const onDown = (e) => {
    if (e.button != null && e.button !== 0 && e.pointerType === "mouse") return;
    const p = local(e);
    if (pointers.size === 0) { downTarget = e.target.closest ? e.target.closest("g.pt") : null; moved = 0; }
    else downTarget = null;
    pointers.set(e.pointerId, p);
    try { svgEl.setPointerCapture(e.pointerId); } catch {}
    e.preventDefault();
  };
  const onMove = (e) => {
    if (!pointers.has(e.pointerId)) return;
    const prev = pointers.get(e.pointerId);
    const p = local(e);
    if (pointers.size === 1) {
      view = pan(view, p.x - prev.x, p.y - prev.y);
      moved += Math.hypot(p.x - prev.x, p.y - prev.y);
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.entries()];
      const other = a[0] === e.pointerId ? b[1] : a[1];
      const dOld = Math.hypot(prev.x - other.x, prev.y - other.y) || 1;
      const dNew = Math.hypot(p.x - other.x, p.y - other.y) || 1;
      const midOld = { x: (prev.x + other.x) / 2, y: (prev.y + other.y) / 2 };
      const midNew = { x: (p.x + other.x) / 2, y: (p.y + other.y) / 2 };
      view = pan(zoomAt(view, dNew / dOld, midOld.x, midOld.y), midNew.x - midOld.x, midNew.y - midOld.y);
      moved += 1e3;
    }
    pointers.set(e.pointerId, p);
    render();
  };
  const onUp = (e) => {
    if (!pointers.has(e.pointerId)) return;
    const wasSingle = pointers.size === 1;
    pointers.delete(e.pointerId);
    try { svgEl.releasePointerCapture(e.pointerId); } catch {}
    if (wasSingle && e.type === "pointerup" && moved < TAP_PX && downTarget) {
      const m = markers.get(downTarget.getAttribute("data-asset-id"));
      if (m && onSelect) onSelect(m.asset);
    }
    if (pointers.size === 0) downTarget = null;
  };
  const onWheel = (e) => {
    e.preventDefault();
    const p = local(e);
    view = zoomAt(view, e.deltaY < 0 ? 1.25 : 0.8, p.x, p.y);
    render();
  };
  const listeners = [
    ["pointerdown", onDown], ["pointermove", onMove], ["pointerup", onUp], ["pointercancel", onUp],
    ["wheel", onWheel, { passive: false }], ["gesturestart", (e) => e.preventDefault()],
  ];
  for (const [type, fn, opts] of listeners) svgEl.addEventListener(type, fn, opts);
  window.addEventListener("resize", api.resize);
  api.fit();
  return api;
}
