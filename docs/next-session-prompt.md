# 다음 세션 시작 프롬프트

새 AI 코딩 세션을 시작할 때 아래 블록 전체를 첫 메시지로 붙여 넣으세요. 2026-11-02 상태 기준입니다.
상태가 바뀌면 이 문서와 [인수인계](handoff.md)를 함께 갱신하세요.

```text
너는 GitHub 저장소 jhy0285/Universal-AI-Trading-Team 의 작업을 이어받는 코딩 에이전트다.
이 저장소는 단일 LLM이 판단하고 결정론적 Python이 집행하는 PAPER ONLY 모의 트레이딩 시스템이다. 실제 돈은 쓰지 않는다.

## 시작 전에 반드시 읽을 것 (이 순서로)
1. AGENTS.md — 어기면 안 되는 규칙과 변경 후 확인 명령
2. docs/handoff.md — 현재 상태, 다음 할 일, 환경 함정, 설계 결정
3. docs/validation-report.md 맨 위 섹션 3개 — 최신 검증 수치
4. 필요할 때: docs/market-observer.md(무료 시장 관측), docs/cli-providers.md(Claude CLI 연결),
   docs/implementation-decisions.md(원문에 없는 실행 의미), docs/learning-loop.md(학습 루프)

## 현재 상태 (2026-11-02)
- main: PR #1·PR #2까지 병합됨(2026-11-02). PR #1: 무료 시장 관측, 핸들 인용, 실패 진단·교정 재시도, 학습 루프, 인수인계 문서.
  PR #2: Claude Sonnet 5.5 판단 모델 연결(`LLM_PROVIDER=claude_cli`, `LLM_MODEL=claude-sonnet-5-5`), VET 기대수익 수수료 이중 차감 수정,
  VET 판정 의미 정의, 보유자 선검사(Jupiter 견적 전 집중도·계약 검사), Claude Code 부모 세션 변수 제거,
  체결 없는 사이클의 CHIEF 모델 호출 생략, 학습 제안 알림, 보존 1~7일 거부, 문서.
- 브랜치: claude/nice-einstein-fz8wua는 병합돼 삭제해도 된다(원격 삭제는 세션 권한에서 403, 사용자가 웹에서). claude/beautiful-faraday-wwthsb에는
  이후 커밋(scripts/debug_cli_provider.py, 90분 실행 기록)이 있다. PR은 사용자가 요청할 때만 만든다.
- 테스트: pytest 257건. 2026-11-09 문서 변경 후 SQLite에서 통과를 확인했고, PostgreSQL 16은 CI(`pytest --postgres`)가 돌린다.
- 판단 모델: 로컬 Qwen3-4B·8B는 VET를 신뢰할 수 없음(거의 항상 "이미 반영", 추정치 고정·null).
  Claude Sonnet 5.5는 같은 실제 관측 9건에 후보별 근거 있는 판단(모두 REJECT, 기대수익 음수).
- 합성 강신호(10,000 USD)에서는 Sonnet PASS → 600 USD 모의 체결 → RISK 청산 +54.33 USD까지 확인.
- 실데이터: 자동 운영 5회(23·90·75분, 2026-11-02의 10,000 USD 90분 등) 동안 실데이터 모의 체결 0건. 막는 것은 모델이 아니라 후보 자료와 관측 범위다.
  관측 풀 3개 90분: 결정 223건 중 88%가 관측되지 않은 풀, 관측된 풀 8개는 거래 구간 부족·집중도·가격 선행으로 차단, 모델 호출 0건.
  관측 풀 6개 125분: 관측 풀 15개, SCAN 5·VET 8회가 모두 첫 응답 통과. Sonnet이 4개 풀을 승률 0.30~0.42, 기대수익 −2~−5%로 모두 REJECT. 체결 0.
  6풀에서는 1분 수집이 60초를 넘어 120분 중 30회 건너뛴다.
  관측 풀 8개·수집 주기 120초(AUTONOMOUS_COLLECT_SECONDS=120) 120분: 건너뜀 0, 수집 중앙값 64.5초·최대 110초, 모델에 닿은 풀 6개(VET 9회)를 Sonnet이 전부 REJECT, 체결 0.
  무료 Jupiter 키로 관측 풀 10개·수집 주기 60초 120분: 관측 풀 18개, 수집 중앙값 34.6초(키 없는 8풀은 64.5초), 모델에 닿은 풀 3개(VET 6회)를 Sonnet이 전부 REJECT, 체결 0.
  그때의 병목은 공용 Solana RPC 429(수집 112회 중 97회). Helius 무료 Solana RPC(SOLANA_RPC_URL=https://mainnet.helius-rpc.com)로 같은 10풀·60초 120분을 돌리자
  수집 오류 0, ONCHAIN_UNAVAILABLE 0, 수집 중앙값 30.7초(건너뜀 0), 모델에 닿은 풀 7개(VET 19회)였고 Sonnet이 19건을 모두 REJECT(승률 0.30~0.44, 기대수익 −5%~−1%), 체결 0.
  체결 0은 이제 자료가 아니라 시장·전략 결과로 보인다. Alchemy 무료는 getProgramAccounts가 429로 막혀 쓸 수 없었다(자세한 표는 docs/validation-report.md).
  남은 병목: GeckoTerminal 거래 구간(클라우드 공유 IP). 다음 시험은 공유되지 않은 IP에서 24시간 이상. 결과는 scripts/run_summary.py 로 본다.
  Jupiter·Helius 키는 이 세션 환경에 request_credential로 저장돼 있다(값은 모른다). 앱에는 JUPITER_API_KEY=proxy-injected 자리표시와 키 없는 SOLANA_RPC_URL만 넘기면 프록시가 진짜 키를 헤더로 붙인다.
  한계: 앱은 RPC에 키 헤더를 보내지 않고 URL의 키도 거부한다. 프록시가 없는 환경에서 Helius를 쓰려면 앱에 RPC 키 헤더 설정(미구현)이 필요하다.
  주된 차단 사유: 최대 보유자 5% 초과, 보유자보다 가격이 빨리 오름, 어린 pair 유동성 감소, 상위 보유자 매도,
  GeckoTerminal 429로 거래 구간 없음(클라우드 공유 IP). Helius 실행에서는 관측된 풀 결정 74건 중 자료 가용성 21·시장 규칙 34·모델 19.
- 1,000 USD 원문 자금에서는 주문 60 USD, 최소 수수료 0.95 USD로 왕복 약 3.2%가 들어 진입이 매우 드물다.
  10,000 USD(PAPER_INITIAL_CASH)면 왕복 약 0.9%. 게이트는 바꾸지 않았다.

## 바꾸면 안 되는 것 (AGENTS.md 요약)
- 실제 주문·지갑·서명·출금 경로 금지. TRADING_MODE=paper만.
- 전역 모델 하나. 에이전트별 모델·자동 fallback 금지.
- LLM은 수량·가격·한도 권한이 없다. 결정론적 게이트를 느슨하게 만들어 체결을 억지로 만들지 마라.
- 모르는 값을 0/false로 바꾸지 마라. 합성 자료를 실데이터 결측의 대체물로 쓰지 마라.
- 증거 ID는 앱이 계산한 값과 정확히 일치해야 한다(모델에는 A1·E1 핸들, Python이 원래 ID로 되돌림).
- 학습 루프는 제안만 한다. 기존 DB 테이블은 바꾸지 말고 새 테이블만 추가한다.
- .env·토큰·프록시 주소를 출력하거나 파일·커밋에 남기지 마라.

## 할 일 (우선순위)
1. 최신 main에서 시작한다(PR #1·#2는 병합됨). main이 앞서 나가면 rebase·force push 대신 main을 merge한다. 병합은 사용자가 승인했을 때만 한다.
2. 실데이터 모의 체결 1건 확인(인수인계 완료 기준).
   - Sonnet 전역 모델로 worker를 장시간(가능하면 24시간) 돌린다. PAPER_INITIAL_CASH=10000을 쓸지는 사용자와 정한다.
   - 사용자 PC처럼 공유되지 않은 IP에서 돌리면 GeckoTerminal 429가 줄어든다. 무료 JUPITER_API_KEY가 있으면
     MARKET_OBSERVER_MAX_ASSETS를 5~6으로 올려 모델까지 가는 후보를 늘릴 수 있다.
   - 결정 사유 분포(GET /decisions, 대시보드 Meme Coin Scanner), 모델 호출 수, 체결·RISK 청산·장부 무결성을 기록한다.
   - 체결이 없으면 어느 게이트가 막았는지 숫자로 보고한다. 게이트를 바꾸고 싶다면 재생으로 먼저 시험하고 정책 버전을 올리며,
     반드시 사용자 승인을 받는다.
3. 24시간 연속 운영 점검: 저장량(1분 수집당 약 59KB, 14일 보존), lease, 타임아웃, 모델 지연·사용량, 학습 제안 누적.
4. 학습 루프 제안을 실제 데이터로 읽는다. 표본(24h 가격 30건·자산 5개, 확정 20건)이 쌓이기 전에는 결론 금지.
5. BOOK(예측시장): World Monitor 교차 신호가 0개인 원인(upstream seed)을 조사한다.

## 확인 명령 (변경 후 항상)
.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts
.venv/bin/python scripts/check_docs.py
.venv/bin/pytest -q
DB를 건드렸으면 PostgreSQL로도: 임시 클러스터를 띄워
UAT_TEST_DATABASE_URL=postgresql+psycopg://test@127.0.0.1:55432/test .venv/bin/pytest -q --postgres

## 실데이터 실행 방법
사용자 PC(.env):
  LLM_PROVIDER=claude_cli
  LLM_MODEL=claude-sonnet-5-5
  AUTONOMOUS_ENABLED=true
  DEX_DISCOVERY=launches
  PAPER_INITIAL_CASH=10000   # 새 장부를 만들 때만 적용된다
그다음: .venv/bin/trading-team init && .venv/bin/trading-team probe && .venv/bin/python scripts/services.py start

Claude Code 클라우드 세션 안에서 시험할 때는 부모 세션 변수가 자식 CLI로 넘어가지 않게 깨끗한 환경으로 실행한다.
프록시 값은 파일에 쓰지 말고(분류기가 막는다) 명령줄에서 그때그때 넘긴다. 세션이 재시작되면 프록시 포트가 바뀐다:
  env -i HOME="$HOME" PATH="$PATH" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY=127.0.0.1,localhost \
    NODE_EXTRA_CA_CERTS="$NODE_EXTRA_CA_CERTS" SSL_CERT_FILE="$SSL_CERT_FILE" REQUESTS_CA_BUNDLE="$REQUESTS_CA_BUNDLE" \
    LLM_PROVIDER=claude_cli LLM_MODEL=claude-sonnet-5-5 DATABASE_URL=sqlite:///<스크래치>/worker.sqlite3 \
    STATE_DIR=<스크래치>/state AUTONOMOUS_ENABLED=true DEX_DISCOVERY=launches PAPER_INITIAL_CASH=10000 \
    .venv/bin/trading-team worker

## 환경 함정 (자세한 것은 handoff)
- 프로세스를 이름(pkill -f, pgrep -f, cmdline 문자열)으로 찾아 죽이지 마라. 자기 셸이 죽는다. 정확한 argv로 찾거나 PID를 저장하라.
- force push는 막혀 있다. 히스토리를 고쳐 쓰지 말고 merge로 해결하라.
- 클라우드 공유 IP에서 Jupiter 키 없는 한도(약 10초당 5회)와 GeckoTerminal(30회/분)이 다른 사용자 때문에 자주 찬다.
- 로컬 CPU 모델은 느리다(8B VET 1회 약 90초). LLM_TIMEOUT_SECONDS=300이 최대다.
- llama.cpp는 JSON 스키마를 디코딩 문법으로만 쓴다. 규칙은 task 문장이나 enum으로 표현하라.
- Provider를 바꾸면 .env의 LLM_BASE_URL을 비워라. claude_cli는 이것을 ANTHROPIC_BASE_URL로 덮어써서, 로컬 모델 설정이 남아 있으면 Claude CLI가 출력 없이 멈춘다.
  멈추면 .venv/bin/python scripts/debug_cli_provider.py 로 앱이 만드는 정확한 명령을 직접 실행해 보라(위 env -i 예시는 그대로 동작함을 2026-11-02에 확인).
- VET 기대수익은 수수료 전 값이다. 지시문을 고칠 때 수수료를 모델이 빼게 만들지 마라(이중 차감).
- 사용자 메시지는 항상 유효한 JSON으로 유지하라(교정 피드백 키는 validation_feedback).

## 작업 방식
- 검증한 것만 검증했다고 쓴다. 결과는 docs/validation-report.md 맨 위에 날짜별 섹션으로 기록하고,
  상태가 바뀌면 docs/handoff.md와 이 프롬프트(docs/next-session-prompt.md)를 갱신한다.
- 작은 단위로 커밋·푸시한다(세션이 자주 재시작된다). PR은 사용자가 요청할 때만 만든다.
- Claude 모델 호출은 사용자 계정의 사용량을 쓴다. 긴 실행 전에 사용자에게 알린다.
```
