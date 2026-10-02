import test from "node:test";
import assert from "node:assert/strict";
import { migrationReadiness, migrationText, persistenceText } from "../../web/app/migrate.js";

const ctx = { studyId: "s1", dataMode: "synthetic" };
const rec = (status, study_id = "s1", data_mode = "synthetic") => ({ status, study_id, data_mode });

test("이전 준비 판정: 내보내지 않은 기록이 하나라도 있으면 준비가 아니다 (현재 설정·다른 설정 구분)", () => {
  assert.deepEqual(migrationReadiness([], ctx), { total: 0, exported: 0, unexported: 0, unexportedCurrent: 0, unexportedOther: 0, otherContexts: [], ready: true });
  const r = migrationReadiness([rec("saved"), rec("exported"), rec("saved", "s2", "private_real"), rec("saved", "s1", "private_real"), rec("exported", "s2")], ctx);
  assert.deepEqual(r, { total: 5, exported: 2, unexported: 3, unexportedCurrent: 1, unexportedOther: 2, otherContexts: ["s1/private_real", "s2/private_real"], ready: false });
  assert.equal(migrationReadiness([rec("exported"), rec("exported", "s2")], ctx).ready, true);
  assert.equal(migrationReadiness([rec("saved")], { studyId: null, dataMode: "synthetic" }).unexportedOther, 1, "study_id 미설정이면 전부 다른 설정");
});

test("판정 문구: 없음 / 준비 완료 / 내보내기 필요(현재·다른 설정 안내)", () => {
  assert.equal(migrationText(migrationReadiness([], ctx)).cls, "pencil");
  const ok = migrationText(migrationReadiness([rec("exported"), rec("exported")], ctx));
  assert.equal(ok.cls, "ok");
  assert.match(ok.text, /옮길 준비가 됐습니다: 기록 2건/);
  assert.match(ok.text, /PC 에 반영·백업됐는지는 이 폰에서 알 수 없습니다/, "내보냄은 PC 반영·백업이 아니다");
  const need = migrationText(migrationReadiness([rec("saved"), rec("saved", "s2"), rec("exported")], ctx));
  assert.equal(need.cls, "warn");
  assert.match(need.text, /보내야 할 기록 2건 \(보냄 1건, 전체 3건\)/);
  assert.match(need.text, /지금 설정의 1건/);
  assert.match(need.text, /다른 작업 공간·자료 종류의 1건\(s2\/synthetic\)/);
  const onlyCurrent = migrationText(migrationReadiness([rec("saved")], ctx));
  assert.doesNotMatch(onlyCurrent.text, /다른 작업 공간/);
});

test("지속 저장 문구: 미확인 / 허용 / 미허용 모두 '임장 뒤 바로 PC 로 보냅니다' 를 권한다", () => {
  for (const v of [null, undefined, true, false]) assert.match(persistenceText(v), /임장 뒤 바로 PC 로 보냅니다/);
  assert.match(persistenceText(null), /확인되지 않습니다/);
  assert.match(persistenceText(true), /허용됐습니다/);
  assert.match(persistenceText(false), /허용되지 않았습니다/);
});
