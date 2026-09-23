# tests/

단위·통합·기기 시험 절차와 가상 fixture. GitHub Actions에서는 가상자료만 사용하며 실제 정본·사진을 올리지 않는다.

- `test_repo_allowlist.py`: 저장소에 실데이터·비밀키 형식 파일이 들어오지 않았는지 검사한다(J5-002).
- `test_j5_003_schemas.py`: 스키마 3종과 fixture 검증, 관측↔시드 asset_id 연결 확인.
- `test_j5_008_inspect.py` / `test_j5_008_copy.py`: PC 검사 도구. fixture 9종(폴더·ZIP)과 적대적 ZIP, CLI 종료 코드, 독립 사본.
- `test_web_static.py`: web/ 정적 앱 규칙(외부 URL·인라인 스크립트·네트워크 API 금지, CSP).
- `web/*.test.mjs`: 웹 모듈 단위 테스트(ZIP 작성기·내보내기는 Python zipfile·j5 inspect 를 오라클로 씀, 지도 투영·맞춤·축척)와 브라우저 e2e(`app.e2e.test.mjs`, 헤드리스 Chromium + CDP, 지도 마커 탭·지도 실패 시 목록, 다운로드한 ZIP 을 j5 inspect 로 검사. 브라우저 없으면 건너뜀). `node --test "tests/web/**/*.test.mjs"` (Node는 테스트 전용).
- `conftest.py`: fixture 폴더를 결정적으로 ZIP으로 묶는 헬퍼.
- `fixtures/`: 정상·중복·손상·미지원 입력 예제. 목록과 기대 결과는 `fixtures/README.md`.
