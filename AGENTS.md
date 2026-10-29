# Agent guide

PAPER ONLY 모의 트레이딩 시스템입니다. 작업 전에 [docs/handoff.md](docs/handoff.md)를 읽으세요. 상태, 다음 할 일, 환경 함정이 있습니다.

## 어기면 안 되는 것

- 실제 주문·지갑·서명·출금 경로를 만들지 않습니다. `TRADING_MODE=paper`만 허용합니다.
- 전역 모델 하나만 씁니다. 에이전트별 모델 override나 자동 fallback을 만들지 않습니다.
- LLM 출력은 수량·가격·한도에 대한 권한이 없습니다. 결정론적 게이트가 다시 검사합니다.
- 모르는 값을 0이나 false로 바꾸지 않습니다. 합성 자료를 결측의 대체물로 쓰지 않습니다.
- 증거 ID는 앱이 계산한 값과 정확히 일치해야 합니다. 보정하지 말고 차단합니다.
- 학습 루프는 제안만 합니다. 전략 규칙을 자동으로 바꾸지 않습니다.
- 기존 DB 테이블을 바꾸지 않습니다. 새 테이블만 추가합니다.
- `.env`와 비밀 값을 출력하거나 커밋하지 않습니다. 외부 호스트가 정책으로 막히면 우회하지 말고 사용자에게 허용을 요청합니다.

## 변경 후 확인

```bash
.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts
.venv/bin/python scripts/check_docs.py
.venv/bin/pytest -q
```

DB를 건드렸다면 PostgreSQL로도 실행합니다(`--postgres`, [handoff](docs/handoff.md) 참고). 검증하지 못한 것은 검증했다고 쓰지 말고 [검증 보고서](docs/validation-report.md)에 그대로 기록하세요.
