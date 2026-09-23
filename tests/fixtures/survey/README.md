# tests/fixtures/survey

조사 경로·점포·표본틀·세션 수동 입력 파일의 가상 예제 (J5-013A, `schemas/survey_input.schema.json`). 실제 경로·점포는 J5-006A 에서 사전 답사 후 이 형식으로 만든다. 모든 ID·이름·좌표는 가상값이다.

| 파일 | 내용 |
|---|---|
| `route_v1.json` | 경로 A v1 (구간 S1·S2·S3). `route_version_id` 는 관측 fixture(`packages/valid`) 가 적은 값과 같다 |
| `route_v2.json` | 경로 A v2 (S3 공사로 S3b 우회, previous_version_id = v1) |
| `units_v1.json` | 점포 6개 (가상로 1 건물 좌·우는 물건 1 에 연결, 가상로 2 는 두 구간에 걸침) |
| `units_v2.json` | 가상로 3·4 종료 + 통합 점포 신규, merge 링크 2건 |
| `frame_v1.json` | 표본틀 v1: 점포 6개 |
| `frame_v2.json` | 표본틀 v2: 통합 반영 (3·4 탈락, 통합 점포 추가), previous_version_id = v1 |
| `session_1.json` | 2026-09-22 세션 (v1 경로·표본틀): 확인 4 (점유 2·공실 2), 당일 휴무 1, 임대 광고만 1 |
| `session_2.json` | 2026-10-20 세션 (v2): S3b 건너뜀, 확인 4 (점유 3·공실 1), 가상로 5 미조사 |

기대 집계: 세션 1 N 6·K 4·V 2 (비율 0.5), 세션 2 N 5·K 4·V 1 (비율 0.25), 공통 표본 비교는 점포 1·2·3 (통합된 3·4 는 단절) 에서 A 공실 1 → B 공실 1.
