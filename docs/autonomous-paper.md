# 자동 페이퍼 운영

자금은 모두 가상 USD입니다. 실제 주문·지갑·서명·출금 경로는 없습니다.
운영 초기 설정 후에는 매 거래마다 사람의 승인을 받지 않습니다. 데이터·모델 검증과 결정론적 게이트가 진입 여부를 정합니다.

## 현재 실행 범위

- 로컬 CPU LLM 하나로 CHIEF·SCAN·VET·BOOK을 실행합니다. 전역 모델 ID와 모델 파일 URL·SHA-256은 환경변수에 있습니다.
- 실제 로컬 추론의 간단한 schema probe는 네 역할 모두 통과했습니다. 일반 분석의 합성 재생에서는 VET가 `LLM_SCHEMA_INVALID`로 차단됐으며 출력 호환 문제는 미해결입니다.
- 시작 시 모델 점검·시장자료 수집·RISK·WM 점검을 실행합니다. 모델 준비 후 첫 SCAN을 시작합니다.
- 시장자료는 60초, SCAN은 15분, RISK는 5분, BOOK과 WM seed는 15분, 마감 보고는 서울 23시에 실행합니다.
- 학습 루프는 매시간 결정 결과를 측정하고 개선 제안을 갱신합니다. 규칙은 바꾸지 않습니다. [학습 루프](learning-loop.md)
- 모델과 데이터가 복구되면 다음 작업에서 다시 평가합니다. Kill Switch와 감독 중단은 자동으로 해제하지 않습니다.
- `AUTONOMOUS_ENABLED=false`는 예정된 신규 진입과 모델 보고를 중지합니다. 기존 포지션의 결정론적 RISK는 계속 실행합니다.

원본 밈코인 전략의 필수 보유자·whale·원천 시각·실행 깊이가 현재 확보되지 않았습니다.
해당 값은 unknown을 유지하며 **필수 자료가 없는 후보는 진입하지 않습니다**. Solana 집중도 조회에서도 실제 429를 관측했습니다.
`observing`은 자동 수집·감시 중이라는 뜻입니다. 체결이나 전체 데이터 준비를 의미하지 않습니다.

## 로컬 LLM 설치

Linux x86_64, 여유 디스크 3.5GB 이상을 사용합니다. 현재 모델은 약 2.5GB이고 CPU 추론을 사용합니다.
설치 도구는 공식 llama.cpp b11790 배포본과 모델 파일의 SHA-256을 확인합니다. 모델을 자동으로 다른 것으로 교체하지 않습니다.
모델 라이선스와 배포 출처를 확인한 GGUF 파일을 선택하고 `.env`에 아래 값을 설정합니다.

```dotenv
LLM_PROVIDER=llama_cpp
LLM_MODEL=
LLM_API_KEY=
LLM_BASE_URL=http://127.0.0.1:8081/v1
LOCAL_LLM_MODEL_URL=
LOCAL_LLM_MODEL_SHA256=
LOCAL_LLM_THREADS=3
LOCAL_LLM_CONTEXT_SIZE=8192
AUTONOMOUS_ENABLED=true
```

`LLM_MODEL`은 빈 값으로 실행할 수 없습니다. 현재 설치된 모델 ID와 다운로드 URL·해시는 기존 비공개 `.env`에 보존돼 있습니다.
URL은 고정 revision의 HTTPS 파일이어야 하며 키·인증 query를 포함하지 않습니다.

```bash
.venv/bin/python scripts/setup_local_llm.py
.venv/bin/trading-team init
.venv/bin/python scripts/services.py stop
.venv/bin/python scripts/services.py start --worldmonitor
```

서비스 관리자는 선택된 로컬 모델을 먼저 실행하고 health 응답을 확인합니다.
서버는 loopback에만 바인딩하고 CPU·단일 slot·offline·도구/MCP/agent 비활성 설정을 사용합니다.
모델 비용은 외부 API 비용 0원이라는 전체 운영비 가정으로 환산하지 않습니다. 로컬 토큰과 CPU·메모리 비용을 구별합니다.

## 로컬 World Monitor 데이터

```bash
bash scripts/setup-worldmonitor.sh
.venv/bin/python scripts/setup_worldmonitor_runtime.py
```

두 번째 명령은 고정 공식 Redis 이미지의 Linux 바이너리, 고정 npm Redis client, upstream REST proxy를 준비합니다.
처음 바이너리를 준비할 때 로컬 Docker daemon이 필요합니다. 설치 후에는 호스트 프로세스로 실행합니다.
upstream proxy의 별도 로컬 복사본은 bind 주소만 loopback으로 바꾸며 원본 checkout은 보존합니다.

```dotenv
WORLDMONITOR_SEED_ENABLED=true
```

로컬 Redis 인증값은 설치 도구가 생성해 비공개 환경 파일에 저장합니다. 값을 출력하거나 Git에 넣지 않습니다.
서비스 관리자가 Redis·REST proxy·WM을 시작하고 worker가 공식 지진·규제기관 RSS·공포탐욕·예측시장·교차신호 seed를 갱신합니다.
원본 외부 자료·서버 시각을 보존하며, 빈 자료를 READY로 바꾸거나 hosted WM으로 전환하지 않습니다.
Redis는 16379, REST proxy는 8079, WM은 3000의 loopback 주소만 사용합니다.

실제 fear/greed와 prediction 데이터는 수집됐습니다. 교차 신호는 현재 수집 범위에서 생성되지 않아 BOOK은 대기합니다.
WM 검사에는 producer 시각·갱신 주기와 실제 지표 입력이 있는지도 포함됩니다.

## 완전한 시장 관측 연결

DexScreener 탐색을 유지하며 운영자가 선택한 데이터 공급자의 관측을 `MARKET_DATA_FEED_URL`로 받을 수 있습니다.
인증이 필요하면 별도의 `MARKET_DATA_FEED_API_KEY`를 환경 설정에 넣습니다. LLM 키·API 관리 토큰과 공유하지 않습니다.

GET 요청의 `pairs` 인자는 Dex에서 발견한 pool ID 목록입니다. 응답 계약:

```json
{"snapshots": []}
```

각 항목은 `MarketSnapshot` schema 전체에 맞아야 하며 같은 asset·chain·pool ID와 `provenance=live`여야 합니다.
모델이 이 URL을 선택하거나 호출하지 않습니다. 신뢰하는 공급자가 보고한 관측을 앱이 읽는 경계입니다.
필드는 [domain.py](../src/trading_team/domain.py)의 `MarketSnapshot`, `ExecutionQuote`, `TransactionWindow`를 따릅니다.

- 보유자 수·이전 관측·시각·가격과 유동성 이력, 온체인 집중도·검증 시각·제한된 계약 점검 출처가 필요합니다.
- 겹치지 않는 두 거래 구간과 large-holder 매도 관측이 필요합니다. 모르는 값을 false/0으로 만들지 않습니다.
- buy/sell quote는 해당 자산·방향·원천 시각·당시 실행 깊이를 가져야 합니다.
- 앱은 자신의 수집 시각만 기록하며 공급자의 관측 시각을 새 시각으로 덮어쓰지 않습니다.
- 발견되지 않은 pool, 다른 chain/asset, 중복 pool, fixture 응답은 거절합니다. 공급 장애 시 원래의 불완전한 Dex 관측으로 남고 진입은 차단됩니다.

자료가 모두 와도 LLM 승인과 결정론적 VET·SIZE·FILLS 검사를 통과해야 체결됩니다.

## BOOK과 확정 결과

BOOK은 WM에서 발견한 정확한 시장 ID로 공식 venue의 제목·규칙·마감 시각을 읽고, 최근 관련 신호를 자동으로 조립합니다.
입력에는 예측시장 가격을 넣지 않습니다. 파생 신호임을 명시하고 직접 근거가 부족하면 abstain하도록 요청합니다.
확률을 보존한 뒤 별도 가격·깊이를 조회합니다. 시각이 없는 호가는 체결에 쓰지 않습니다.

RISK는 공개 sell quote로 예측 포지션을 평가합니다. Kalshi의 finalized 상태·yes/no 결과·일치하는 settlement value·정산 시각, Polymarket의 resolved + 정확한 0/1 결과만 가상 계약을 정산합니다.
불명확·분쟁·취소·비이진 결과는 정산하지 않습니다. 확인된 계약 정산은 모델과 독립적이며 Kill Switch 상태에서도 가능합니다.
이 구현의 해석은 계약당 0/1 USD, 모의 redemption fee 0입니다. 진입 때 반영한 거래 수수료는 유지합니다.
정산·현금·수량·거래 에피소드·감사 증거는 하나의 DB 트랜잭션으로 저장하고 반복 조회가 이중 입금되지 않도록 합니다.

## 상태 확인과 중단

- `GET /automation`: 실제 worker lease·설정 일치, 각 작업 결과와 필수 자료 누락 개수.
- `GET /readiness`: 모델·시장·WM 준비 상태. 자동 실행과 데이터 READY는 별개입니다.
- 대시보드 Trading Overview: 자동 모의운영과 데이터 대기 상태.
- `GET /audit?account_id=main`: 현금·수량·감사 체인 일치.
- 새 진입 정지: 기존 Kill Switch. 전체 프로세스 중지: `scripts/services.py stop`.

설정을 변경할 때는 모든 관련 프로세스를 재시작합니다. 시작 시 추가 `automation_jobs` 테이블을 만들며 기존 장부·계좌를 삭제하지 않습니다.
정상 종료에서는 실행 중인 작업의 취소·lease 정리를 기다립니다. 의도한 종료를 모델 무응답 장애로 누적하지 않습니다.
환경 snapshot은 실행 중인 프로세스를 보존하지 않습니다. 새 환경에서는 위 시작 명령이 필요합니다.
