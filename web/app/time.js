// 시각 형식. 시간대 있는 ISO 8601 (로컬 오프셋) 과 날짜 정밀도를 구분한다 (데이터 사전 §1).

const pad = (n, w = 2) => String(n).padStart(w, "0");

export function isoWithOffset(date = new Date()) {
  const off = -date.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const a = Math.abs(off);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}${sign}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
}

export function localDate(date = new Date()) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// <input type="datetime-local"> 값(YYYY-MM-DDTHH:MM) 을 로컬 시각으로 해석해 오프셋 있는 ISO 로 바꾼다.
export function fromDatetimeLocal(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value || "");
  if (!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  // Date 는 13월·40일을 다음 달로 넘기므로 구성요소가 그대로인지 확인한다.
  const same = d.getFullYear() === +m[1] && d.getMonth() === +m[2] - 1 && d.getDate() === +m[3]
    && d.getHours() === +m[4] && d.getMinutes() === +m[5];
  if (Number.isNaN(d.getTime()) || !same) return null;
  return isoWithOffset(d);
}

export function toDatetimeLocal(date = new Date()) {
  return `${localDate(date)}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
