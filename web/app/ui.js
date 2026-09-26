// 화면 전환·표시 전용 모듈 (J5-020). 저장소·이벤트·내보내기 로직은 건드리지 않는다.
// 화면(view)은 index.html 의 <section class="view" data-view="..."> 이고, 하단 내비게이션 버튼은 data-view 로 연결된다.
// 화면 전환 시 hidden 을 바꾸고 URL 해시(#home 등)를 맞추며 그 화면의 제목으로 포커스를 옮긴다. 외부 통신 없음.

export const VIEWS = ["home", "map", "records", "export", "settings"];
export const DEFAULT_VIEW = "home";
export const MODE_LABEL = { synthetic: "연습용 자료", private_real: "실제 자료 (비공개)" };
export const MODE_SHORT = { synthetic: "연습용", private_real: "실제" };

/** 해시(#map 등)를 화면 이름으로. 모르는 값은 기본 화면. */
export function viewFromHash(hash) {
  const name = String(hash || "").replace(/^#/, "");
  return VIEWS.includes(name) ? name : DEFAULT_VIEW;
}

/** 저장 시각(ISO)을 "오늘 14:05" / "9월 25일 14:05" 처럼 짧게. 값이 없으면 null. */
export function shortWhen(iso, now = new Date()) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (sameDay) return `오늘 ${hm}`;
  return `${d.getMonth() + 1}월 ${d.getDate()}일 ${hm}`;
}

/** 물건별 기록 요약: 마지막 기록 상태·시각, 기록 수, 사진 수, 미내보냄 수. */
export function assetSummary(assetId, events) {
  const mine = events.filter((r) => r.asset_id === assetId);
  let last = null;
  for (const r of mine) if (!last || r.saved_at > last.saved_at) last = r;
  return {
    count: mine.length,
    photos: mine.reduce((s, r) => s + (r.event?.attachment_refs?.length ?? 0), 0),
    unexported: mine.filter((r) => r.status !== "exported").length,
    lastStatus: last?.event?.payload?.change_status ?? null,
    lastAt: last?.saved_at ?? null,
    lastObservedAt: last?.event?.observed_at ?? null,
  };
}

/** 다음에 기록할 물건: 현재 물건 다음부터(현재 물건이 없으면 처음부터) 돌며 이 기기에 기록이 없는 것 → 없으면 다음 물건 → 하나뿐이면 null. */
export function nextAsset(assets, currentId, events) {
  if (!assets.length) return null;
  const i = assets.findIndex((a) => a.asset_id === currentId);
  const start = i < 0 ? 0 : i + 1;
  const recorded = new Set(events.map((r) => r.asset_id));
  for (let k = 0; k < assets.length; k++) {
    const a = assets[(start + k) % assets.length];
    if (a.asset_id !== currentId && !recorded.has(a.asset_id)) return a;
  }
  const n = assets[start % assets.length];
  return n.asset_id === currentId ? null : n;
}

/** 내보내기 단계: 1 확인 → 2 파일 만들기 → 3 파일 저장 → 4 저장 확인. */
export function exportStep({ hasPackage, attempted, done }) {
  if (done) return 4;
  if (attempted) return 4;
  if (hasPackage) return 3;
  return 2;
}

/**
 * 화면 전환기. sections: {name: HTMLElement}, navButtons: HTMLElement[] (data-view), onShow(name) 은 전환 뒤 호출.
 * 포커스는 화면의 첫 제목(h2, tabindex=-1)으로 옮긴다.
 */
export function createNavigator({ sections, navButtons, onShow, doc = document, win = window }) {
  let current = null;
  function show(name, { focus = true, updateHash = true } = {}) {
    if (!sections[name]) name = DEFAULT_VIEW;
    for (const [k, el] of Object.entries(sections)) el.hidden = k !== name;
    for (const b of navButtons) {
      const active = b.dataset.view === name;
      if (active) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    }
    const changed = current !== name;
    current = name;
    if (updateHash && win.location.hash !== `#${name}`) {
      try { win.history.replaceState(null, "", `#${name}`); } catch {}
    }
    if (focus && changed) {
      const h = sections[name].querySelector("h2");
      if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: false }); }
      try { win.scrollTo({ top: 0, behavior: "instant" }); } catch { win.scrollTo(0, 0); }
    }
    onShow?.(name, changed);
    return name;
  }
  for (const b of navButtons) b.addEventListener("click", () => show(b.dataset.view));
  win.addEventListener("hashchange", () => show(viewFromHash(win.location.hash), { updateHash: false }));
  return { show, get current() { return current; } };
}
