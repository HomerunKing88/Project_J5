// 기기·도메인 이전 준비 (J5-017D, 데이터 사전 §12 "브라우저 저장은 사이트 origin에 연결된다. 도메인·기기 이전 전에 기록을 파일로 내보내고,
// 새 기기에서는 검증한 조회 패키지를 불러온다"). 순수 함수: DOM·저장소를 만지지 않는다. 판정은 이 기기의 기록 상태만 본다.
// 내보냄(exported)은 파일 저장 확인까지를 뜻하며 PC 반영·백업 완료를 뜻하지 않는다(J5-007). 앱은 기록을 지우지 않는다.

/**
 * @param {Array<{status: string, study_id: string, data_mode: string}>} records 이 기기의 관측 기록 전부
 * @param {{studyId: string|null, dataMode: string}} ctx 현재 설정
 */
export function migrationReadiness(records, { studyId, dataMode }) {
  const isCurrent = (r) => r.study_id === studyId && r.data_mode === dataMode;
  const unexported = records.filter((r) => r.status !== "exported");
  const unexportedCurrent = unexported.filter(isCurrent).length;
  const others = new Set(unexported.filter((r) => !isCurrent(r)).map((r) => `${r.study_id}/${r.data_mode}`));
  return {
    total: records.length,
    exported: records.length - unexported.length,
    unexported: unexported.length,
    unexportedCurrent,
    unexportedOther: unexported.length - unexportedCurrent,
    otherContexts: Array.from(others).sort(),
    ready: unexported.length === 0,
  };
}

/** 판정 문구와 강조 종류. 내보내기 대상 기록이 남아 있으면 이전 준비가 아니다. */
export function migrationText(r) {
  if (r.total === 0) return { text: "저장한 기록이 없어 옮길 기록이 없습니다. 새 폰에서는 설정과 자료만 다시 넣습니다.", cls: "pencil" };
  if (r.ready) return { text: `옮길 준비가 됐습니다: 기록 ${r.total}건을 모두 보냈습니다 (파일 저장 확인 기준). PC 에 반영·백업됐는지는 이 폰에서 알 수 없습니다. PC 에서 확인한 뒤 이 폰의 사이트 데이터를 지웁니다.`, cls: "ok" };
  let text = `옮기기 전에 보내야 할 기록 ${r.unexported}건 (보냄 ${r.exported}건, 전체 ${r.total}건).`;
  if (r.unexportedCurrent) text += ` 지금 설정의 ${r.unexportedCurrent}건은 기록 화면의 PC 로 보내기로 보냅니다.`;
  if (r.unexportedOther) text += ` 다른 작업 공간·자료 종류의 ${r.unexportedOther}건(${r.otherContexts.join(", ")})은 설정을 그 값으로 바꿔 따로 보냅니다.`;
  return { text, cls: "warn" };
}

/** 저장 지속성 표시. Storage API 가 없으면 '확인 안 됨'. 지속이 허용되지 않은 저장은 브라우저가 지울 수 있다(ADR E08). */
export function persistenceText(persisted) {
  if (persisted === null || persisted === undefined) return "이 브라우저가 기록을 오래 보관해 줄지 확인되지 않습니다. 브라우저가 사이트 데이터를 지울 수 있으니 임장 뒤 바로 PC 로 보냅니다.";
  return persisted ? "이 브라우저가 기록을 오래 보관하도록 허용됐습니다. 그래도 직접 지우거나 폰을 잃으면 사라지니 임장 뒤 바로 PC 로 보냅니다." : "이 브라우저가 기록을 오래 보관하도록 허용되지 않았습니다. 저장 공간이 부족하면 지울 수 있으니 임장 뒤 바로 PC 로 보냅니다.";
}
