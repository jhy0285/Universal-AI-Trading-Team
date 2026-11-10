# 인수인계

이 문서만 읽어도 이어서 작업할 수 있도록 현재 상태, 검증 범위, 다음 할 일, 함정, 바꾸면 안 되는 결정을 모았습니다.
새 AI 세션은 [시작 프롬프트](next-session-prompt.md)를 첫 메시지로 붙여 넣어 시작하세요. 세부 근거는 [검증 보고서](validation-report.md)에 날짜별로 있습니다. 이 문서의 수치는 2026-11-01 기준입니다.

## 한 줄 요약

**단일 LLM으로 판단하고 결정론적 Python이 집행하는 PAPER ONLY 모의 트레이딩 시스템**입니다. 수집·SCAN·VET·모의 체결·RISK·정산·측정이 자동으로 끝까지 돌지만,
**소형 로컬 모델은 VET 판단을 신뢰할 수 없고, Claude Sonnet 5.5(`claude_cli`)는 후보별 근거 있는 판단을 냅니다.** 실데이터 모의 체결은 아직 없으며, 막는 것은 후보의 자료(집중도·가격 선행)입니다.

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
| VET 판단 품질 | 4B·8B 신뢰 불가 / Claude Sonnet 5.5 동작 | 로컬은 `signal_priced_in`을 거의 항상 true. Sonnet은 같은 9건에 후보별 근거·추정치 |
| 모델이 PASS를 낸 뒤 주문까지 | Sonnet으로 합성 자료에서 확인 | 강신호 합성 사례에서 PASS → 600 USD 체결 → RISK 청산. 실데이터 체결은 아직 없음 |
| 미국 주식·ETF 백테스트([equity-backtest](equity-backtest.md)) | 구현·실데이터로 실행 | 목표 "SPY 보유와 비슷한 수익 + 낙폭 40% 이상 축소"를 사전 고정하고 후보 11개를 개발 2001–2019에서 비교, 선택된 `mom12_top5_def`(12-1개월 모멘텀 상위 5개, 빈 칸 SHY)를 검증 2020–2026에서 한 번 확인: **근소하게 미달**(연수익 13.4% 대 14.8%, 낙폭 −23.2% 대 −33.7%). 모의 운용·LLM 판단은 아직 연결 안 함 |
| 미국 ETF 실시간 모의 운용([equity-paper](equity-paper.md)) | 구현·실데이터로 시작(2026-11-10) | `mom12_top5_def`와 SPY 보유를 하루 1회 결정·기록(불변, 시각·데이터 해시·`timely`)하고 백테스트 엔진으로 모의 자산 계산. 시작 데이터 2026-11-09, 목표 EEM·QQQ·XLE·XLK·XLV 각 20%. **판정은 1년·SPY 낙폭 15% 이상일 때만.** 기록은 이 컨테이너 디스크에만 있어 회수되면 사라짐 |
| 국내 ETF 백테스트([equity-backtest-kr](equity-backtest-kr.md)) | 사전 등록 후 실행(2026-11-10) | 같은 후보 11개·같은 기준을 KODEX 200 벤치마크, 국내 ETF 20개 유니버스, 편도 10bps로 개발 2010–2019에서 비교: **통과 후보 0개 → 검증 구간은 열지 않음.** 모멘텀 계열은 수익이 벤치마크보다 높지만(8.2~8.3% 대 4.2%) 낙폭이 더 깊고(−35% 대 −27%), 추세 규칙은 손해. 튀는 값 59봉을 빼고 다시 해도 같은 결론. 실시간 모의 운용에 연결하지 않음 |
| BOOK(예측시장) | 대기 | World Monitor 교차 신호 0개. 실제 BOOK 진입 미검증 |
| 장기 무중단 운영 | 미검증 | 최장 약 90분(중간에 컨테이너 재시작이 있었고 같은 장부로 수동 조치 없이 재개). 2026-11-02 실데이터 90분: 결정 223건, 체결 0, 88%가 관측되지 않은 풀 |

## 이어서 할 일 (우선순위)

0. **사용자 결정(2026-11-10).** (a) 대상 시장을 넓히기로 했고 **미국 주식·ETF를 먼저** 합니다. 밈코인 신규 상장은 체결 0건이라 범위를 넓히는 것이 합리적이라고 판단했습니다.
   (b) 개선은 **반자동**입니다: 학습 루프가 제안하면 AI가 재생·백테스트로 검증해 구체적 변경안을 가져오고, **사용자가 승인한 뒤에만** 적용합니다. 자동 적용은 하지 않습니다.
   (c) 성공 기준(사용자가 선택): **SPY 보유와 비슷한 수익 + 더 작은 낙폭**. 숫자로 고정했습니다(연수익 ≥ SPY − 1%p, 최대 낙폭 ≤ SPY의 60%).
   결과는 위 표와 [equity-backtest](equity-backtest.md): 개발 구간에서는 통과, 검증 구간에서는 근소하게 미달. **검증 구간은 한 번 열렸으므로 더는 깨끗하지 않습니다.** 후보를 더 만들어 이 구간으로 고르지 마세요. 기준을 바꾸려면 결과를 본 뒤의 변경이라고 기록하고 사용자 승인을 받으세요.
   **2026-11-10 사용자 승인으로 `mom12_top5_def`와 SPY 보유를 일봉 실시간 모의 운용에 연결했습니다**([equity-paper](equity-paper.md)): 하루 1회 결정을 불변으로 기록하고 모의 자산은 백테스트 엔진으로 재생해 계산합니다. 이것이 새로운 깨끗한 검증입니다. 낙폭 기준은 폭락이 와야 평가되므로 판단까지 수개월~수년이 걸릴 수 있습니다.
   **해야 할 일:** 매일 `DATABASE_URL=… trading-team equity-paper run`을 돌립니다(이 세션에서는 예약 작업이 07:41 KST에 호출). 기록이 컨테이너 디스크에만 있으므로 **몇 달 가는 시험은 사용자 PC나 서버의 PostgreSQL/지속 디스크로 옮겨야** 합니다. LLM은 과거 백테스트가 아니라 **실시간 모의 운용에서만** 평가합니다(모델이 과거를 알아 미래 정보가 샙니다).
   **국내 ETF(2026-11-10, 사용자 요청):** 후보를 돌리기 전에 [equity-backtest-kr](equity-backtest-kr.md)에 사전 등록을 커밋한 뒤 한 번 실행했습니다. 통과 후보가 없어 검증 구간(2020~)은 **아직 열리지 않았고 깨끗합니다.** 후보를 더하거나 매개변수를 바꿔 다시 돌리려면 시험 횟수 증가로 적고 사용자 승인을 받으세요. 검증 구간의 큰 변동일(2026-03-04, 2026-07-31)은 열기 전에 사실인지 확인해야 합니다. 다음 시장 후보: BTC·ETH·SOL(24시간 거래라 달력·월말 규칙을 엔진에서 다시 정의해야 하고 이력이 짧음).
1. **실데이터 모의 체결 1건을 확인합니다.** 판단 모델은 Claude Sonnet 5.5(`claude_cli`)로 정했습니다([CLI 호환](cli-providers.md)). 후보별 추정치와 합성 자료의 PASS → 체결은 확인했습니다.
   남은 완료 기준은 결정론적 게이트를 통과한 **실데이터** 모의 체결 1건입니다. 1,000 USD에서는 왕복 수수료가 약 3.2%라 진입이 드뭅니다([자동 페이퍼](autonomous-paper.md)).
   후보가 모델에 닿는 비율은 보유자 선검사로 높였습니다. 더 넓히려면 무료 Jupiter 키와 함께 `MARKET_OBSERVER_MAX_ASSETS`를 올리세요.
   2026-11-02의 10,000 USD 실행에서도 체결은 0건이었습니다. 관측 풀 3개 90분은 모델 호출 0건(결정의 88%가 관측되지 않은 풀)이었고,
   `MARKET_OBSERVER_MAX_ASSETS=6` 125분은 관측 풀 15개, 모델 호출 SCAN 5·VET 8로 후보가 처음 모델에 닿았지만 Sonnet이 4개 풀을 모두 음의 기대수익으로 REJECT했습니다.
   이제 병목은 살아남는 후보의 수와 질입니다. 키 없이 8풀·수집 주기 120초 120분도 체결 0이었고, 모델에 닿은 풀은 6개로 늘었지만 Sonnet이 전부 REJECT했습니다(수집 건너뜀은 0).
   무료 Jupiter 키(개인 환경 `AI trading`에 `request_credential`로 저장, 앱에는 자리표시 `JUPITER_API_KEY=proxy-injected`만 넘김)로 10풀·60초 120분: 관측 풀 18개, 수집 중앙값 34.6초, 모델에 닿은 풀 3개를 Sonnet이 전부 REJECT, 체결 0.
   그때의 병목은 공용 Solana RPC 429(수집 112회 중 97회)였습니다. **Helius 무료 Solana RPC로 바꾼 10풀·60초 120분(2026-11-09)에서는 수집 오류 0, `ONCHAIN_UNAVAILABLE` 0, 모델 심사 VET 19회(7개 풀)로 병목이 풀렸지만 Sonnet이 19건을 모두 REJECT(승률 0.30~0.44, 기대수익 −5%~−1%)해 체결은 0**입니다. 체결 0건은 이제 자료 문제가 아니라 시장·전략 결과로 보입니다.
   Alchemy 무료는 홀더 스캔(`getProgramAccounts`)이 429로 막혀 쓸 수 없었습니다([검증 보고서](validation-report.md)). Helius 키는 이 세션 환경에 `request_credential`로 저장되어 있고(값은 모름), 앱에는 키 없는 `SOLANA_RPC_URL=https://mainnet.helius-rpc.com`만 주면 프록시가 `x-api-key` 헤더를 붙입니다.
   **알려진 한계:** 앱은 RPC에 키 헤더를 보내지 않고 URL의 키도 거부하므로 이 결과는 프록시가 키를 붙여 주는 환경에서만 재현됩니다. 사용자 PC·서버에서 쓰려면 앱에 RPC 키 헤더 설정을 추가해야 합니다(미구현). Helius 무료 월 1M 크레딧: 2시간 실행의 대시보드 값이 호출 수(4,287)와 같아서 호출당 1크레딧으로 보이며, 그러면 상시 가동 약 19일치입니다(문서 단가 10크레딧이면 5~6일. 24시간 실행의 대시보드 값으로 확정할 것).
   다음 시험: 공유되지 않은 IP에서 24시간 이상 실행, 그리고 체결이 없는 이유를 게이트별로 보고 사용자와 방향을 정하기(게이트 변경은 재생 시험 + 정책 버전 상승 + 사용자 승인).
   학습 루프의 1시간 뒤 가격: 신규 상장 풀은 평균적으로 크게 하락(표본 작음, 결론 아님). 체결이 드문 것이 결함이 아닐 수 있습니다.
   실행 결과는 `.venv/bin/python scripts/run_summary.py`(읽기 전용)로 한 번에 봅니다.
2. **worker를 24시간 이상 연속 실행해 점검합니다.** 저장량(14일 보존), lease, 타임아웃, 모델 지연, 학습 루프 제안이 쌓이는 양을 봅니다.
3. **학습 루프의 제안을 실제 데이터로 읽습니다.** 표본(24h 가격 30건·자산 5개, 확정 20건)이 쌓이기 전에는 결론을 내지 마세요. 임계값을 바꾸려면 반드시 재생으로 먼저 시험하고 정책 버전을 올립니다.
4. **BOOK을 살립니다.** 원인 사슬은 upstream 코드 읽기로 정리했습니다([World Monitor](world-monitor-integration.md)): 집계기 입력 23개 중 2개만 채움 → 신호는 드문 사건에 의존 → 0건이면 발행하지 않음 → 503 → BOOK 대기.
   남은 일은 실행 확인입니다. 승인된 환경에서 `node scripts/seed-cross-source-signals.mjs` 로그의 `Found N/23`·`Extracted K`를 보고, 규제 `high` 항목이 있는데 0건인 이유를 가리세요. 이 컨테이너는 Node 22·Docker 없음이고 제3자 코드 실행은 권한이 막았습니다.
5. **작은 보강(완료):** 새 개선 제안 알림, 보존 1~7일 거부(학습 창 보호), 대시보드 제안 상태 필터.
6. **정리:** PR #1·#2는 병합됐습니다. `claude/nice-einstein-fz8wua`는 삭제해도 됩니다(이 세션의 git 권한으로는 원격 삭제가 403으로 거부되어 웹에서 지워야 합니다).
   `claude/beautiful-faraday-wwthsb`에는 2026-11-02 후속 커밋(CLI 진단 스크립트, 90분 실데이터 실행 기록)이 있으며 PR은 사용자가 요청할 때 만듭니다.

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
- **Claude Code 세션 안에서 `claude_cli`를 시험할 때는 깨끗한 환경을 쓰세요.** 앱은 `CLAUDECODE`·`CLAUDE_CODE_*`를 지우지만 세션의 `ANTHROPIC_BASE_URL` 같은 값은 그대로 전달합니다.
  `env -i HOME=… PATH=… HTTPS_PROXY=… NODE_EXTRA_CA_CERTS=… SSL_CERT_FILE=…`처럼 로그인·프록시에 필요한 값만 넘겨 실행했습니다.
- **Provider를 바꿀 때 `.env`의 `LLM_BASE_URL`을 비우세요.** `claude_cli`는 이 값을 `ANTHROPIC_BASE_URL`로 덮어씁니다. 로컬 모델 시험 뒤 `http://127.0.0.1:8081/v1`이 남아 있으면
  Claude CLI가 꺼진 서버로 접속해 출력 없이 `CLI_TIMEOUT`·`CLI_EXEC_FAILED`가 납니다(2026-11-02에 겪음. 인증과 네트워크는 정상이었음).
  앱은 자식 CLI의 stderr를 일부러 숨기므로 `.venv/bin/python scripts/debug_cli_provider.py`로 앱이 만드는 **정확한 명령**을 직접 실행해 종료 상태와 출력을 보세요(경고와 환경 변수 *이름*만 출력).
- **VET의 기대수익은 수수료 전 값입니다.** 게이트가 왕복 수수료를 직접 뺍니다. 지시문에 수수료를 언급하면 모델이 미리 빼서 두 번 빠집니다(2026-11-01 발견·수정).
- **프로세스를 이름으로 찾아 죽이지 마세요.** `pkill -f`·`pgrep -f`는 물론 `/proc/*/cmdline` 스캔도, 명령줄에 같은 문자열(스크립트 본문 포함)이 있으면 자기 셸을 죽입니다(이 작업에서 4번 겪음).
  서버는 시작할 때 PID를 저장해 두거나, 실행 파일 경로 `os.readlink(f"/proc/{pid}/exe")`가 `.../llama-server`로 끝나는지로 찾으세요.
- **이 클라우드 VM은 세션이 유휴가 되면 약 5분 뒤 통째로 꺼집니다**(디스크는 남고 프로세스는 사라짐, 사용자 메시지나 예약 호출이 오면 다시 켜짐; 2026-11-10에 두 번 겪음, `/proc/uptime`이 작으면 방금 부팅). 분리 실행(`setsid nohup`)한 워커는 같이 죽었습니다.
  이틀 전 2시간 실행은 **추적되는 배경 작업**(`run_in_background`)으로 띄워서 유휴 중에도 살아 있었습니다. 추적되는 작업은 최대 2시간이므로 이어서 띄우는 스크립트(남은 시간만 계산해 같은 장부로 재개)와 1시간 점검이 필요합니다. 이 가설의 최종 확인은 24시간 실행 결과를 보세요.
- `.env`, `.services/`(모델 2.5GB), `.cache/`, `.state/`(SQLite 장부)는 git에 없습니다. 비밀 값을 출력하거나 커밋하지 마세요.
- 실제 모델 출력을 회귀 테스트로 고정하는 방식이 효과적이었습니다(`tests/test_llm.py`의 `REAL_QWEN_VET_REJECT`).

## 설계 결정과 이유

- **학습 루프는 제안만.** 모의 표본이 작아 자동 조정은 우연에 과적합하고 정책 버전 고정을 흐립니다. 사람이 `acknowledged`·`dismissed`·`applied`로 표시합니다. 2026-11-10에 사용자가 이 **반자동** 방식(제안 → AI 검증 → 사용자 승인)을 유지하기로 확인했습니다.
- **백테스트는 규칙만, LLM 없이.** 모델은 과거 가격·사건을 알고 있어 과거 구간 평가에 미래 정보가 샙니다. 매개변수는 문헌 값으로 미리 고정하고 조정하지 않습니다(여러 값을 시험해 최고를 고르면 과적합).
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
| `equity_backtest.py`, `equity_markets.py`, `scripts/backtest_equities.py`, `scripts/equity_candidates.py` | 주식·ETF 일봉 백테스트와 사전 등록 기준으로 후보 고르기. `equity_markets.py`가 미국·국내 시장별 고정 설정(`--market us|kr`)(주문·장부 없음, 연구 전용) |
| `equity_paper.py` | 미국 ETF 실시간 모의 운용: 결정 기록·모의 자산 재생(`trading-team equity-paper`, 새 테이블 3개) |
| `worker.py`, `autonomy.py`, `operations.py` | 스케줄, 자동 운영 상태, lease·감독 |
| `api.py`, `dashboard.py`, `cli.py` | FastAPI, Streamlit 13개 화면, `trading-team` |
| `notify.py` | 선택적 Discord·Slack·Telegram 알림 |
| `tests/` | 305건(2026-11-10). `conftest.py`의 `db` 픽스처가 SQLite 또는 PostgreSQL 스키마를 제공 |
