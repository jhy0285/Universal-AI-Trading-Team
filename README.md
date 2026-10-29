# Universal AI Trading Team

**단일 LLM을 사용하는 PAPER ONLY 트레이딩 연구 앱**입니다. FastAPI, Streamlit, 독립 스케줄러, Decimal 모의 장부와 재생 도구를 제공합니다.

## 현재 상태

**로컬 CPU LLM과 자동 페이퍼 수집·감시를 실행합니다.** 네 AI 역할의 간단한 실제 추론·출력 스키마 점검은 통과했습니다.
실제 모델을 연결한 합성 재생에서 VET가 `LLM_SCHEMA_INVALID`로 차단되던 문제는 원인(REJECT 시 손익비 0 출력)을 수정했습니다.
이제 사이클이 정상 완료되며, 해당 모델은 합성 후보를 REJECT해 주문은 없습니다. PASS 경로의 실제 모델 호환은 미검증입니다.
전역 `LLM_PROVIDER=llama_cpp`와 환경변수의 모델 ID를 사용하며 API 키가 필요 없습니다.
로컬 World Monitor의 실제 Redis/seed도 구성했고 fear/greed·prediction 데이터를 수집했습니다. 교차 신호가 없어 BOOK은 대기합니다.
DexScreener에 없는 원천 시각·보유자 이력·큰 지갑 매도·실행 깊이는 무료 공개 API로 채웁니다([무료 시장 관측](docs/market-observer.md)).
이 자료가 하나라도 없거나 오래되면 해당 진입은 계속 차단합니다. 후보는 기본적으로 DexScreener 신규 상장 피드에서 찾습니다(`DEX_DISCOVERY`).
자동 수집이 실행 중이라는 사실은 체결 조건이 충족됐다는 의미가 아닙니다. [자동 페이퍼 운영](docs/autonomous-paper.md)을 참고하세요.
합성 데모 자료를 이 결측의 대체물로 사용하지 않습니다. 구체적인 결과는 [검증 보고서](docs/validation-report.md)를 확인하세요.

## 운영 원칙

- CHIEF·SCAN·VET·BOOK은 **같은 전역 Provider·모델·키**를 사용합니다. 프로세스별 공용 클라이언트와 DB 설정 버전 검사를 둡니다.
- 설정은 `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` 환경변수로 관리합니다. 에이전트별 override와 자동 모델 fallback은 없습니다.
- 학습 루프는 결과를 측정해 개선안을 **제안**만 합니다. 전략 규칙은 자동으로 바꾸지 않습니다. [학습 루프](docs/learning-loop.md)를 참고하세요.
- SIZE·FILLS·RISK는 결정론적 Python입니다. RISK는 LLM 대기열과 독립적으로 실행됩니다.
- 실제 주문 제출, 지갑·서명, 실거래 전환 기능은 없습니다. `TRADING_MODE=paper`만 허용합니다.
- 모든 자금은 가상 USD입니다. `main` 장부의 시작 금액은 `PAPER_INITIAL_CASH`(기본 1000)로 정하며 **장부를 처음 만들 때만** 적용합니다.
  기존 장부의 잔고·이력은 바꾸지 않습니다. 다른 금액으로 새로 시작하려면 새 `DATABASE_URL`을 지정하세요. `trading-team init`이 실제 적용된 잔고를 표시합니다.
- 원문에서 정하지 않은 수식·체결 의미는 버전 고정된 [구현 해석](docs/implementation-decisions.md)에 공개했습니다.

## 빠른 시작

Python 3.12, [uv](https://docs.astral.sh/uv/), Linux/macOS 환경을 사용합니다. 관리형 클라우드의 프록시·CA 설정을 유지하세요.

```bash
bash scripts/bootstrap.sh
.venv/bin/trading-team demo
.venv/bin/python scripts/services.py start
```

`bootstrap.sh`는 lockfile대로 설치하고 `.env`가 없을 때 로컬 관리 토큰과 DB 비밀번호를 생성합니다.
기존 `.env`와 장부는 보존합니다. 토큰·키는 화면이나 Git에 출력하지 않습니다.
`demo`는 새 `fixture` 계정에서 기록된 합성 분석을 재생하며 API 키와 LLM 호출이 필요 없습니다.

- 대시보드: http://127.0.0.1:8501
- API 문서: http://127.0.0.1:8000/docs
- 상태: `GET /health`, 자료·모델 준비도: `GET /readiness`
- 프로세스 관리: `.venv/bin/python scripts/services.py status` / `stop`
- 로그: `.state/services/`, 기본 SQLite 장부: `.state/trading.sqlite3`

클라우드에서는 해당 포트를 포워딩해 접근합니다. `services.py`는 Linux `/proc`를 사용합니다.
다른 OS에서는 아래 명령을 각 터미널에서 실행하세요.

```bash
.venv/bin/trading-team serve
.venv/bin/streamlit run src/trading_team/dashboard.py --server.address 127.0.0.1 --browser.gatherUsageStats false
.venv/bin/trading-team worker
```

## API 키 없이 Codex CLI 사용

**CLI 확장:** Claude Code·Gemini CLI·Qwen Code·OpenCode·Copilot·Goose·Ollama CLI도 연결할 수 있습니다.
기타 비대화형 도구는 공통 JSON 브리지로 확장합니다. [CLI 호환 가이드](docs/cli-providers.md)에 설정·인증·검증 범위를 정리했습니다.

```bash
bash scripts/install-clis.sh
.venv/bin/trading-team providers
```

선택 설치는 Claude/Gemini/Qwen/OpenCode/Copilot 5종의 고정 버전만 저장소 내부에 추가합니다. 실제 모델 검증은 별도로 `probe`해야 합니다.

기존 **정상 동작하는 로컬 Codex 로그인**을 `codex exec`로 호출하는 Provider를 구현했습니다.
Codex 앱의 구독을 일반 OpenAI API 키로 변환하는 방식이 아닙니다. 별도 API 키를 요구하지 않으며 CLI 사용 한도는 적용됩니다.

```dotenv
LLM_PROVIDER=codex_cli
LLM_MODEL=
LLM_API_KEY=
CODEX_RUNNER=local
```

`.env`의 `LLM_MODEL`에 자신의 Codex CLI에서 사용 가능한 모델 ID를 입력하세요. 코드에는 특정 모델을 고정하지 않습니다.
사용자 PC의 터미널에서 `codex login status`와 실제 `codex exec`가 동작해야 합니다.
다음 명령으로 전역 구성을 반영하고 네 역할의 JSON 출력을 점검합니다.

```bash
.venv/bin/trading-team init
.venv/bin/trading-team probe
```

API와 worker를 함께 재시작하세요. 설정 변경 전 버전으로 실행 중인 프로세스의 LLM 호출과 신규 체결은 차단합니다.
이 클라우드의 로그인 상태 표시는 실제 추론 성공을 보장하지 않았습니다. 읽기 전용 CLI 상태 경로와 401 오류가 관측됐습니다.
선택적인 `CODEX_RUNNER=docker`는 기존 인증 파일을 읽기 전용으로 연결하며 인증 성공을 만들어 주지는 않습니다.
일반 Compose 앱 이미지에는 Codex CLI나 사용자의 로그인 파일을 포함하지 않습니다. Codex 사용에는 로컬 실행을 권장합니다.

다른 Provider는 `openai`, `anthropic`, `google`, `xai`, `ollama`, `openai_compatible`을 지원합니다.
동일한 전역 모델만 사용하며, 실제 모델의 구조화 출력 지원 여부는 `probe`로 검증해야 합니다.
HTTP Provider는 Ollama 이외에 `LLM_API_KEY`가 필요합니다. CLI Provider는 기존 정상 로그인으로 키 생략을 허용합니다.
Compatible endpoint에는 `LLM_BASE_URL`도 설정합니다.

## 로컬 World Monitor

Node 24와 npm 11 이상을 사용합니다. 고정된 upstream 소스와 package lock을 설치합니다.

```bash
bash scripts/setup-worldmonitor.sh
.venv/bin/python scripts/services.py start --worldmonitor
```

REST 주소는 `http://127.0.0.1:3000`입니다. hosted World Monitor로 전환하지 않습니다.
기본 dev 서버만 설치하면 fear/greed와 prediction은 빈 데이터, signals는 503이 될 수 있습니다.
**BOOK READY에는 실제 Redis/seed 및 유효한 원천 근거가 필요합니다.** 세 경로의 HTTP 200만으로 READY를 선언하지 않습니다.
[연동 문서](docs/world-monitor-integration.md)와 [운영 절차](docs/runbook.md)를 참고하세요.

## PostgreSQL / Docker Compose

```bash
bash scripts/bootstrap.sh
docker compose build
docker compose up -d
```

DB·API·대시보드·worker를 실행합니다. World Monitor도 컨테이너에서 실행하려면 먼저 설치 스크립트를 실행한 뒤
`docker compose --profile worldmonitor up -d`를 사용하세요. 호스트에서 같은 포트를 사용하는 개발 서비스를 먼저 중지합니다.
Compose는 PostgreSQL을 사용하고 호스트 개발 명령은 `.env`의 DB URL을 사용합니다. 두 장부는 별개입니다.
관리형 클라우드 Docker 빌드의 읽기 전용 경로 문제는 [운영 절차](docs/runbook.md)에 해결 방법을 기록했습니다.

## 재생·검증

```bash
.venv/bin/trading-team fixture --output /tmp/paper-case.json
.venv/bin/trading-team replay /tmp/paper-case.json --account experiment-01
.venv/bin/pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

동일한 자료와 기록된 모델 출력으로 결과를 재현합니다. `--use-model`은 현재 전역 모델로만 실제 추론하며 비용/사용량이 발생할 수 있습니다.
모델을 바꿀 때는 프로세스를 재시작하고 새 계정으로 **순차 실행**하세요. `Model Comparison`은 자료 해시·정책·초기 자금 그룹을 표시합니다.
장기 수익성, 모델 우월성, 확률 보정은 검증하지 않았습니다.

## 문서

| 문서 | 내용 |
| --- | --- |
| [원본 전략](docs/original-strategy-spec.md) | 전체 원문에서 확인한 규칙·수치·미정의 항목 |
| [구현 해석](docs/implementation-decisions.md) | Kelly, 부분 체결, 추가 매수, 데이터·시간 처리 |
| [운영 절차](docs/runbook.md) | 환경, 인증, 프로세스, Docker, 재생 및 장애 대응 |
| [검증 보고서](docs/validation-report.md) | 실행한 테스트와 남은 외부 조건 |
| [아키텍처](docs/architecture.md) | 역할·데이터·결정론적 경계 |
| [LLM 설계](docs/llm-provider-design.md) | 전역 설정, 기능 probe, 실패 처리 |
| [CLI 호환](docs/cli-providers.md) | CLI 8종과 공통 JSON 브리지, 선택 설치와 전역 설정 |
| [학습 루프](docs/learning-loop.md) | 결정 결과 측정, 모델 신뢰도·보정, 사람이 검토하는 개선 제안 |
| [자동 페이퍼](docs/autonomous-paper.md) | 로컬 CPU 모델, 자동 작업·자료 공급·BOOK·정산·상태 확인 |
| [무료 시장 관측](docs/market-observer.md) | Jupiter 깊이·Solana 보유자·GeckoTerminal 거래로 채우는 필드, 해석, 비용 |
| [World Monitor](docs/world-monitor-integration.md) | REST/MCP/SDK 조사와 self-host 조건 |
| [API 조사](docs/api-integration-plan.md) | DexScreener·Kalshi·Polymarket·Solana의 계약과 제한 |
| [로드맵](docs/development-roadmap.md) | 구현 범위와 후속 장기 검증 |
| [비용](docs/cost-estimation.md) | 추정식과 미확인 비용 처리 |
