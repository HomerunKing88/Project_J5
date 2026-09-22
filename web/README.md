# web/

정적 앱. HTML·CSS·JavaScript ES Modules, 외부 통신 없음(각 페이지에 CSP `default-src 'self'`). 앱 실행에 Node.js·번들러를 요구하지 않는다. 고정 버전 MapLibre·fflate 배포본은 해당 단계(J5-005·007)에서 검증 후 `web/vendor/`에 넣는다. 실사진·실데이터·API 키는 넣지 않는다.

## 파일

- `index.html` + `app/main.js`: 앱 뼈대 (R1a).
- `devtest.html` + `app/devtest.js`: 기기 시험 페이지 (J5-004). 환경(보안 컨텍스트·crypto.subtle·IndexedDB·저장 용량), 사진 선택·형식 판정·해시, 파일 저장 흐름을 실기기에서 확인한다.
- `app/sniff.js`: 매직바이트 형식 판정. `j5/package/validate.py`의 `sniff_image`와 같은 규칙 + HEIC 감지.
- `app/hash.js`: WebCrypto sha256. 비보안 컨텍스트에서는 null.

## 시험용으로 열기

PC에서 저장소 루트 기준으로 정적 서버를 띄우고 같은 Wi-Fi의 폰에서 연다.

```text
python -m http.server 8000 --directory web
```

- `http://<PC IP>:8000/devtest.html`은 비보안 컨텍스트라 `crypto.subtle`이 없어 해시를 계산하지 않는다. 파일 선택·형식·저장 흐름은 확인할 수 있다.
- 해시까지 확인하려면 HTTPS가 필요하다. 코드 전용 정적 HTTPS 배포는 사용자 승인 후 공급자를 정한다(ADR-01). 실데이터는 어떤 경우에도 올리지 않는다.

## 테스트

`node --test "tests/web/**/*.test.mjs"` (개발·CI 전용). `python -m pytest tests/test_web_static.py`는 외부 URL·인라인 스크립트·네트워크 API가 없는지 검사한다.
