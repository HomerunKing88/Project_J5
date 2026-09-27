// 외부 지도 앱으로 열기 (J5-021, ADR-15). 사용자가 링크를 누를 때만 좌표가 그 서비스로 간다.
// 이 파일은 저장소 안에서 외부 URL 을 가진 유일한 앱 모듈이다 (tests/test_web_static.py 가 호스트 허용목록으로 검사).
// 여기서는 절대 통신하지 않고 페이지를 옮기지도 않는다. href 문자열만 만든다.
// 전달하는 값은 좌표(소수 6자리, 약 0.1 m)와 고정 문구 "선택 위치" 뿐이다. 물건 이름·지번·기록은 보내지 않는다.

const LABEL = "선택 위치";
const ZOOM = 17;

/** 외부 지도 목록. verified: 공식 문서로 형식을 확인한 날짜, 아니면 null (실기기에서 확인할 항목). */
export const EXTERNAL_MAPS = Object.freeze([
  { id: "apple", name: "Apple 지도", host: "maps.apple.com", verified: "2026-09-27",
    build: (lat, lng) => `https://maps.apple.com/?ll=${lat},${lng}&q=${encodeURIComponent(LABEL)}&z=${ZOOM}` },
  { id: "naver", name: "네이버 지도", host: "map.naver.com", verified: null,
    build: (lat, lng) => `https://map.naver.com/v5/?c=${lng},${lat},${ZOOM},0,0,0,dh` },
  { id: "kakao", name: "카카오맵", host: "map.kakao.com", verified: null,
    build: (lat, lng) => `https://map.kakao.com/link/map/${encodeURIComponent(LABEL)},${lat},${lng}` },
  { id: "google", name: "Google 지도", host: "www.google.com", verified: null,
    build: (lat, lng) => `https://www.google.com/maps/search/?api=1&query=${lat}%2C${lng}` },
]);

/** 위치점([경도, 위도]) 이 유효한 좌표인지. 유효하면 소수 6자리로 잘라 {lat, lng} 문자열, 아니면 null. */
export function roundPoint(point) {
  if (!Array.isArray(point) || point.length !== 2) return null;
  const [lng, lat] = point;
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return null;
  if (lng < -180 || lng > 180 || lat < -90 || lat > 90) return null;
  return { lat: lat.toFixed(6), lng: lng.toFixed(6) };
}

/** 필지 bbox([minlon, minlat, maxlon, maxlat]) 의 가운데 점 [경도, 위도]. 형식이 아니면 null. */
export function bboxCenter(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(Number.isFinite)) return null;
  return [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2];
}

/** 점 하나에 대한 외부 지도 링크 목록 [{id, name, href, verified}]. 좌표가 없거나 틀리면 빈 배열. */
export function externalMapLinks(point) {
  const p = roundPoint(point);
  if (!p) return [];
  return EXTERNAL_MAPS.map((m) => ({ id: m.id, name: m.name, href: m.build(p.lat, p.lng), verified: m.verified }));
}

/** 링크 a 요소가 가져야 할 속성. 새 창, opener 차단, 참조 주소(우리 앱 주소) 미전달. */
export const LINK_ATTRS = Object.freeze({ target: "_blank", rel: "noopener noreferrer external", referrerPolicy: "no-referrer" });
