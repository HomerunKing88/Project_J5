# web/

정적 앱. HTML·CSS·JavaScript ES Modules, 외부 통신 없음(각 페이지에 CSP `default-src 'self'`). 앱 실행에 Node.js·번들러를 요구하지 않는다. 외부 코드는 없다. 지도는 자체 SVG 점 지도(ADR-12, 배경 타일 없음)에 연속지적도 변환 번들의 필지 경계·지번을 겹친다(ADR-13). ZIP은 자체 작성기(ADR-11)다. MapLibre는 타일 이용조건이 확인되는 시점에 새 ADR로 검토한다. 실사진·실데이터·API 키는 넣지 않는다.

## 파일

- `index.html` + `app/main.js`: R1a 현장 기록 앱 (J5-006). 설정(study_id·자료 모드·경로 버전 ID) → 시드 불러오기(가상 5개 또는 기기의 파일) → 필지 번들 불러오기(선택) → 물건 선택(목록, 지도의 점, 또는 필지 탭 → 그 안의 물건) → 관측 저장(상태·시각/날짜·설명·사진·태그, 선택으로 촬영 지점·방향·이전 사진: 반복 촬영의 연차 비교용, J5-015B) → 저장 목록. 목록이 주 화면이고 지도가 실패해도 목록으로 동작한다.
- `app/map.js`: 최소 지도(J5-005, ADR-12). Web Mercator 투영으로 `location_point` 있는 물건만 SVG 점·라벨로 그린다(가상은 점선 빈 원, 실제·비공개는 채운 원). 드래그·핀치·휠·버튼으로 이동·확대, 점 탭으로 관측 시작, 축척 막대. 필지 번들이 있으면 점 아래 층에 경계 path(로컬 단위 + 그룹 transform, 가상은 점선)와 지번 라벨(픽셀 단위, 필지가 충분히 클 때만)을 그리고 필지 탭으로 필지 패널을 연다(J5-013B-1, ADR-13). 배경 타일·외부 통신 없음.
- `app/parcels.js`: 필지 번들(`.j5parcels.json`, 파생본 `parcels.geojson`) 검증(`schemas/parcels_bundle.schema.json` 핵심 규칙), 라벨 위치, 점-필지 포함 판정(구멍 제외), 필지의 물건 찾기(정본 연결 `asset_ids` + 위치점 포함, 근거 표시). PNU 는 물건 ID 가 아니고 폰은 연결을 편집하지 않는다.
- `app/db.js`: IndexedDB(`j5`, v2). 스토어 meta·assets·events(객체 + 고정 행 바이트 + 해시)·photos(sha256 → Blob)·parcels(필지 번들 한 벌). 관측과 사진은 한 트랜잭션으로 저장하고 실패 시 '저장됨'으로 표시하지 않는다. 자동 삭제 없음.
- `app/event.js`: 이벤트 작성·정규화(키 정렬·압축·비ASCII 보존 + LF)·검증. 정규화 바이트는 `tests/fixtures/make_packages.py`와 같다.
- `app/export.js`: 내보내기(J5-007). 현재 study_id·자료 모드의 기록을 골라 한도(§3.3)에 맞춰 묶음을 나누고, 고정 행 바이트·참조 사진·manifest 로 `.j5field.zip` 을 만든다. 파일 저장 뒤 "파일 저장 확인"을 눌러야 기록이 `내보냄`이 된다.
- `app/zip.js`: STORED 전용 ZIP 작성기(ADR-11). `app/limits.js`: §3.3 한도 상수.
- `app/sha256.js`, `app/uuid.js`, `app/time.js`: 순수 sha256 폴백, UUID v4, 오프셋 있는 ISO 시각.
- `sw.js`: 최소 앱 캐시. 앱 실행 파일만 캐시하고 시드·사진·데이터는 캐시하지 않는다. 보안 컨텍스트에서만 등록된다.
- `data/assets.seed.synthetic.json`: 가상 시드 (tests/fixtures 와 동일). `data/parcels.synthetic.j5parcels.json`: 가상 필지 6개 (tests/fixtures/parcels 와 동일, PC 도구 `j5 parcels convert` 출력 형식). 실제 필지 번들은 기기 파일로만 넣고 저장소에 두지 않는다.
- `devtest.html` + `app/devtest.js`: 기기 시험 페이지 (J5-004). 환경(보안 컨텍스트·crypto.subtle·IndexedDB·저장 용량), 사진 선택·형식 판정·해시, 파일 저장 흐름을 실기기에서 확인한다.
- `app/sniff.js`: 매직바이트 형식 판정. `j5/package/validate.py`의 `sniff_image`와 같은 규칙 + HEIC 감지.
- `app/hash.js`: WebCrypto sha256. 비보안 컨텍스트에서는 null.

## 시험용으로 열기

PC에서 저장소 루트 기준으로 정적 서버를 띄우고 같은 Wi-Fi의 폰에서 연다.

```text
python -m http.server 8000 --directory web
```

- `http://<PC IP>:8000/`은 비보안 컨텍스트라 `crypto.subtle`·서비스 워커가 없다. 해시는 순수 JS 폴백으로 계산하고, 앱 캐시(오프라인 실행)는 HTTPS 에서만 동작한다.
- 해시까지 확인하려면 HTTPS가 필요하다. 코드 전용 정적 HTTPS 배포는 사용자 승인 후 공급자를 정한다(ADR-01). 실데이터는 어떤 경우에도 올리지 않는다.

## 테스트

`node --test "tests/web/**/*.test.mjs"` (개발·CI 전용). 단위 테스트(sha256·정규화·검증·시각·지도 투영·필지 검증·기하)와 e2e(`app.e2e.test.mjs`: 헤드리스 Chromium 을 CDP 로 구동해 설정→시드→지도 마커 탭·확대→필지 경계·지번·필지 탭→관측 저장→재접속(필지 유지)→지도 실패 시 목록→내보내기→오프라인 재접속을 돌리고, IndexedDB 내용을 패키지 폴더로 꺼내 `python -m j5 inspect` 가 ok 를 내는지 확인. Chromium 이 없으면 건너뜀). `python -m pytest tests/test_web_static.py`는 외부 URL·인라인 스크립트·네트워크 API·HTML 문자열 삽입이 없고 서비스 워커가 앱 파일만 캐시하는지 검사한다.
