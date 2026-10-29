# 인수인계

이 문서만 읽어도 이어서 작업할 수 있도록 현재 상태, 검증 범위, 다음 할 일, 함정, 바꾸면 안 되는 결정을 모았습니다.
세부 근거는 [검증 보고서](validation-report.md)에 날짜별로 있습니다. 이 문서의 수치는 2026-10-29 기준입니다.

## 한 줄 요약

**단일 LLM으로 판단하고 결정론적 Python이 집행하는 PAPER ONLY 모의 트레이딩 시스템**입니다. 수집·SCAN·VET·모의 체결·RISK·정산·측정이 자동으로 끝까지 돌지만,
**지금 쓰는 소형 로컬 모델의 VET 판단이 신뢰할 수 없어 진입은 아직 한 번도 없습니다.** 가장 큰 남은 일은 판단 품질입니다.

## 절대 규칙

- **PAPER ONLY.** 실제 주문·지갑·서명·출금 경로를 추가하지 않습니다. `TRADING_MODE=paper`만 허용합니다.
- **전역 모델 하나.** CHIEF·SCAN·VET·BOOK은 같은 Provider·모델·키를 씁니다. 에이전트별 override와 자동 fallback을 만들지 않습니다.
- **LLM은 권한이 없습니다.** 수량·가격·한도는 SIZE·FILLS·RISK(결정론적 Python)가 정합니다. 모델 출력은 게이트가 다시 검사합니다.
- **자료가 없으면 진입하지 않습니다.** 모르는 값을 0이나 false로 바꾸지 않습니다. 합성 자료를 결측의 대체물로 쓰지 않습니다.
- **증거 ID는 앱이 계산한 값과 정확히 일치**해야 합니다. 라벨이 붙었거나 지어낸 값은 보정하지 않고 차단합니다.
- **학습 루프는 측정과 제안만 합니다.** 전략 규칙·한도·모델을 자동으로 바꾸지 않습니다. 정책 해석은 버전으로 고정됩니다([구현 해석](implementation-decisions.md)).
- Kill Switch와 감독 중단은 자동 해제하지 않습니다.
- 기존 테이블을 바꾸지 않습니다(마이그레이션 도구가 없음). 필요하면 새 테이블만 추가하세요(`create_all`이 만듭니다).

## 지금 되는 것과 안 되는 것

| 영역 | 상태 | 근거 |
| --- | --- | --- |
| 결정론적 집행(SIZE·FILLS·RISK), 장부, 감사 체인, Kill Switch | 구현·테스트 | pytest, SQLite·PostgreSQL 16 |
| 로컬 CPU 모델 연결(Qwen3-4B Q4_K_M, llama.cpp b11790) | 동작 | 4역할 probe, 합성 재생, 실데이터 사이클 |
| 무료 시장 관측([observer](market-observer.md)) | 실데이터로 동작 | 풀 3개씩 보유자·집중도·계약·거래 구간·매수 호가 채움. 이전 값은 5~30분 뒤 두 번째 관측부터 |
| VET `LLM_SCHEMA_INVALID` | 원인 확정·수정 | 거절 시 `win_payoff_ratio: 0`을 쓰는 출력. 비율 0 허용 + SIZE 0 |
| 학습 루프([learning-loop](learning-loop.md)) | 구현·합성 자료로 검증 | 실제 진입 데이터로는 검증 못 함(진입 0건) |
| **VET 판단 품질** | **신뢰 불가** | 4B·8B 모두 `signal_priced_in`을 거의 항상 true로 답하고 추정치가 근거와 무관 |
| 모델이 PASS를 낸 뒤 주문까지 | 실제 모델로 미검증 | 합성 재생은 모두 REJECT. PASS 형태 숫자는 디코딩·검증만 통과 확인 |
| BOOK(예측시장) | 대기 | World Monitor 교차 신호 0개. 실제 BOOK 진입 미검증 |
| 장기 무중단 운영 | 미검증 | 최장 45분 연속 실행 기록 |

## 이어서 할 일 (우선순위)

1. **판단 품질이 되는 전역 모델을 정합니다.** 4B·8B 로컬 모델로는 게이트가 모든 진입을 막습니다.
   API 모델이나 CLI Provider를 `LLM_PROVIDER`로 선택하고(키가 필요하면 사용자에게 요청), 같은 관측으로 `replay --use-model`을 돌려 비교하세요(새 계정으로 순차 실행, `Model Comparison` 화면).
   완료 기준: 서로 다른 후보에 서로 다른 추정치가 나오고, 근거가 있는 후보에서 PASS가 나오며, 결정론적 게이트를 통과한 모의 체결이 최소 1건 기록됨.
2. **worker를 24시간 이상 연속 실행해 점검합니다.** 저장량(14일 보존), lease, 타임아웃, 모델 지연, 학습 루프 제안이 쌓이는 양을 봅니다.
3. **학습 루프의 제안을 실제 데이터로 읽습니다.** 표본(24h 가격 30건·자산 5개, 확정 20건)이 쌓이기 전에는 결론을 내지 마세요. 임계값을 바꾸려면 반드시 재생으로 먼저 시험하고 정책 버전을 올립니다.
4. **BOOK을 살립니다.** World Monitor 교차 신호가 0개인 이유(upstream seed)를 조사하세요([World Monitor](world-monitor-integration.md)).
5. **작은 보강:** 새 개선 제안이 생길 때 `notify.py`로 알림, 학습 분석 창(7일)과 스냅샷 보존(14일)의 상호작용 문서화, 대시보드에서 제안 필터.
6. **정리:** `main`에 병합된 브랜치(`claude/beautiful-faraday-wwthsb`, `claude/nice-einstein-fz8wua`)는 삭제해도 됩니다.

## 시작하는 법

```bash
bash scripts/bootstrap.sh            # .venv, 로컬 토큰이 든 .env. 기존 .env는 보존
.venv/bin/pytest -q                  # 전체 테스트
.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts
.venv/bin/python scripts/check_docs.py
```

로컬 모델(Linux x86_64, 디스크 3.5GB 이상). `.env`에 아래를 넣습니다. 모델은 공식 `Qwen/Qwen3-4B-GGUF`의 고정 revision이며 설치 도구가 SHA-256을 확인합니다.

```dotenv
LLM_PROVIDER=llama_cpp
LLM_MODEL=qwen3-4b-q4_k_m
LLM_BASE_URL=http://127.0.0.1:8081/v1
LOCAL_LLM_MODEL_URL=https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/bc640142c66e1fdd12af0bd68f40445458f3869b/Qwen3-4B-Q4_K_M.gguf
LOCAL_LLM_MODEL_SHA256=7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5
LOCAL_LLM_THREADS=3
LLM_TIMEOUT_SECONDS=300
```

```bash
.venv/bin/python scripts/setup_local_llm.py     # 런타임 + 모델 다운로드·검증
.venv/bin/trading-team init                      # 전역 설정 반영
.venv/bin/python scripts/services.py start       # 모델 서버·API·대시보드·worker
.venv/bin/trading-team probe                     # 4역할 출력 스키마 점검
.venv/bin/trading-team fixture --output /tmp/case.json
.venv/bin/trading-team replay /tmp/case.json --account my-run-1 --use-model
```

PostgreSQL로 테스트하려면 임시 클러스터를 띄워 `UAT_TEST_DATABASE_URL=postgresql+psycopg://test@127.0.0.1:55432/test .venv/bin/pytest -q --postgres`를 실행하세요(CI와 같음).

## 환경 함정

- **네트워크 허용 목록.** 클라우드 환경이 외부 호스트를 정책으로 막으면 우회하지 말고 사용자에게 환경 설정의 Allowed domains 추가를 요청하세요.
  필요한 호스트: `huggingface.co`(와 CDN 도메인), `github.com`(llama.cpp 릴리스), `api.dexscreener.com`, `api.geckoterminal.com`, `api.jup.ag`, `api.mainnet-beta.solana.com`.
- **llama.cpp는 JSON 스키마를 디코딩 문법으로만 쓰고 모델에게 보여 주지 않습니다.** 필드 설명은 효과가 없고 `gt=0` 같은 수치 하한은 강제되지 않습니다. 규칙은 task 문장이나 enum(`cited()`)으로 표현하세요.
- **CPU 모델은 느립니다.** 이 컨테이너(4코어, 3스레드)에서 콜드 800토큰 프롬프트가 120초를 넘겨 유일한 재시도를 소진했습니다. 로컬 모델은 `LLM_TIMEOUT_SECONDS=300`을 쓰세요(최대값).
- **사용자 메시지는 항상 유효한 JSON으로 유지하세요.** 테스트 더블과 일부 소비자가 `json.loads(messages[-1])`을 합니다. 교정 재시도 피드백은 `validation_feedback` 키입니다. 이어 붙이면 사유가 `INPUT_OR_PROVENANCE_INVALID`로 바뀝니다.
- **SQLite 트랜잭션은 모두 `BEGIN IMMEDIATE`입니다.** 긴 분석은 짧은 트랜잭션으로 나누세요(`learning.gate_regret` 참고). 아니면 수집이 최대 30초 기다립니다.
- **`pkill -f`·`pgrep -f`는 명령줄에 같은 문자열이 있으면 자기 셸도 죽입니다.** 대괄호 요령(`[l]lama`)도 같은 명령 안에 원래 문자열이 있으면 소용없습니다.
  PID를 시작할 때 저장하거나, `/proc/*/cmdline`을 읽는 작은 Python 스크립트로 자기 자신을 제외하고 찾으세요.
- `.env`, `.services/`(모델 2.5GB), `.cache/`, `.state/`(SQLite 장부)는 git에 없습니다. 비밀 값을 출력하거나 커밋하지 마세요.
- 실제 모델 출력을 회귀 테스트로 고정하는 방식이 효과적이었습니다(`tests/test_llm.py`의 `REAL_QWEN_VET_REJECT`).

## 설계 결정과 이유

- **학습 루프는 제안만.** 모의 표본이 작아 자동 조정은 우연에 과적합하고 정책 버전 고정을 흐립니다. 사람이 `acknowledged`·`dismissed`·`applied`로 표시합니다.
- **자료 결측은 임계값 문제가 아닙니다.** 결측 사유로 막힌 후보는 `GATE_REVIEW`에서 제외하고 `DATA_COVERAGE`("자료를 공급하라")로만 제안합니다.
- **교정 재시도는 1회, 호출당 감독 결과 1건.** 실패한 첫 시도는 `model_usage`에 남지만 `Attempt.final=false`라 감독기가 세지 않습니다. 그렇지 않으면 Kill Switch 허용치가 절반이 됩니다.
- **실패 진단은 필드 경로와 오류 유형만.** 모델 출력은 기록하지 않고, 모델이 만든 키 이름은 영숫자·밑줄만 남깁니다(`schema_detail`).
- **스키마 실패는 후보 단위로 격리합니다.** 한 후보의 모델 오류는 그 후보만 거절합니다.

## 파일 지도

| 경로 | 역할 |
| --- | --- |
| `src/trading_team/engine.py` | `Desk`: 사이클(SCAN→VET→SIZE→체결), RISK, BOOK |
| `policy.py`, `broker.py` | 순수 규칙과 모의 체결·정산·감사 |
| `observer.py`, `data.py` | 무료 시장 관측, 외부 어댑터 |
| `llm.py`, `llm_types.py`, `cli_providers.py` | Provider, 재시도·교정, `LLMError.detail` |
| `learning.py` | 측정과 제안(매시간, 읽기 전용) |
| `worker.py`, `autonomy.py`, `operations.py` | 스케줄, 자동 운영 상태, lease·감독 |
| `api.py`, `dashboard.py`, `cli.py` | FastAPI, Streamlit 13개 화면, `trading-team` |
| `notify.py` | 선택적 Discord·Slack·Telegram 알림 |
| `tests/` | 251건. `conftest.py`의 `db` 픽스처가 SQLite 또는 PostgreSQL 스키마를 제공 |
