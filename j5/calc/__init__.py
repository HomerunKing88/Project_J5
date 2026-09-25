"""계산기 (R5, J5-016). 데이터 사전 §9~§10, ADR-08. 입력은 schemas/calc_inputs.schema.json.

원칙: 미확인 입력은 null 과 사유이며 0 으로 채우지 않는다. 어느 입력이든 미확인이면 결과는 null 이고 알려진 항목만 보인다. 계산식 버전을 결과에 남긴다.
"""

CALCULATION_VERSION = "1.0.0"
