# World Monitor 연동 설계

상태: Phase 1 설계 기록 + 로컬 Redis/REST proxy·공식 seed 실행 구현.
현재 fear/greed·prediction은 실제 데이터가 있고, 교차 신호 부재로 전체는 degraded입니다.
아래 Phase 1의 미기동 설명은 당시 조사 범위이며 현재 운영은 [자동 페이퍼 운영](autonomous-paper.md)을 따릅니다.

## 1. 선택: Original Strategy는 로컬 REST

원문 STEP 2는 봇 컴퓨터의 World Monitor를 정보 서버로 사용하고, CHIEF는 READY를 만들려고 hosted `api.worldmonitor.app`으로 옮기는 것을 금지합니다.
따라서 **원본 전략의 기본 경로는 로컬 self-host REST Adapter**입니다. Provider나 모델을 바꿔도 데이터 Adapter는 바뀌지 않습니다.

조사한 공식 소스는 [koala73/worldmonitor](https://github.com/koala73/worldmonitor/tree/c0f783620dcdae713a6d28c239844c12311e2bf6),
커밋 `c0f783620dcdae713a6d28c239844c12311e2bf6`입니다. README, OpenAPI, MCP 문서, Python SDK 소스를 읽었습니다.

| 인터페이스 | 공식 근거와 기능 | 원본 전략에서의 선택 |
| --- | --- | --- |
| 로컬 REST | 원문 STEP 2의 3개 endpoint; 공식 `docs/api/*.openapi.json`의 route 계약 | **기본**. 타입 검증하는 읽기 전용 HTTP Adapter |
| Hosted REST | README의 `https://api.worldmonitor.app`, OpenAPI | 원문 자동 대체로 사용하지 않음. 별도 운영 모드 검토 대상 |
| Hosted MCP | `https://worldmonitor.app/mcp`, Streamable HTTP, public tools/list, 인증된 데이터 도구 | 존재 확인. Original의 대체 경로로 활성화하지 않음 |
| 공식 Python SDK | `worldmonitor-sdk` 0.1.1, stdlib-only, MCP-first, REST get 보조 | 오프라인 계약 검토만 수행. Original 경로에 필수 의존성으로 넣지 않음 |

Python SDK의 `base_url`은 REST `get()`에 적용되며 MCP `call_tool()`은 별도 `mcp_url`을 사용합니다.
`base_url`만 로컬로 바꾸고 `call_tool()`을 실행하면 hosted MCP로 갈 수 있으므로 이를 로컬 REST Wrapper처럼 사용하지 않습니다.

## 2. 원문의 준비 검사

| 로컬 endpoint | 원문상 기본 설치의 예상 상태 | 정책 |
| --- | --- | --- |
| GET /api/market/v1/get-fear-greed-index | HTTP 200이어도 unavailable 등의 빈 데이터 | degraded; HTTP 상태만으로 READY 금지 |
| GET /api/prediction/v1/list-prediction-markets?page_size=1 | HTTP 200, dataAvailable=false 또는 fetchedAt=0 | BOOK 중지 |
| GET /api/intelligence/v1/list-cross-source-signals | 기본 seed 부재 시 HTTP 503 / Internal server error | degraded, 오류 원인 기록 |

이 표는 **원문에 적힌 예상값**입니다. 현재 환경에서 이 서버를 기동해 관측한 결과로 보고하지 않습니다.
READY는 세 검사 모두 유효한 데이터·스키마·신선도 요건을 만족할 때만 표시합니다.
기본 Vite 실행만으로 full data service가 준비된다고 가정하지 않습니다. 원문은 전체 구성을 위해 Docker·Redis·수집 스크립트를 안내합니다.

World Monitor가 degraded이면 BOOK을 멈추고 CHIEF가 보고합니다.
밈코인 SCAN은 DexScreener로 계속할 수 있으나, VET의 필수 온체인·보유자·유동성 사실이 없으면 후보 거래를 승인하지 않습니다.
세계 뉴스를 매수 근거로 든 후보는 World Monitor/원출처의 교차 확인 없이 통과시키지 않습니다.

## 3. 수집 계약과 데이터 범위

Data Adapter는 모델 바깥에서 고정된 읽기 작업을 수행합니다. 모델에 임의 URL·임의 endpoint 호출 권한을 주지 않습니다.

| 필요한 정보 | 확인한 공식 surface / 주의점 |
| --- | --- |
| 사건·뉴스·지정학 정보 | News/Intelligence OpenAPI와 MCP get_conflict_events, get_news_clusters, get_news_intelligence; AI 파생 요약과 원본 자료를 구별 |
| 예측 시장 탐색 | 로컬 GET /api/prediction/v1/list-prediction-markets; BOOK은 발견된 질문의 동일성을 별도 검증 |
| 시장 맥락 | MarketService, 원문의 fear-greed endpoint; 이를 체결 가능한 호가로 쓰지 않음 |
| 시장 심리 | 공식 get_market_data의 sentiment 분류와 fear-greed; 모든 자산별·매시각 심리값을 제공한다고 가정하지 않음 |
| 출처·시각·갱신 | 원출처 URL/ID, 관측·수집·cache 시각, coverage·stale 상태를 보존 |

필드가 공급자에 없으면 null/unknown으로 남깁니다. 수집 시각으로 사건 발생 시각이나 원출처 갱신 시각을 덮어쓰지 않습니다.
World Monitor가 다루는 모든 source가 매매에 충분한 빈도·정밀도·이용권을 제공한다는 가정도 하지 않습니다.

## 4. 버전 차이·단위·결측

공식 PredictionService OpenAPI에서 확인한 필드는 다음과 같습니다.

- `dataAvailable`: seed 스냅샷에 대해 평가했는지 여부.
- `fetchedAt`: Unix milliseconds. 0은 unavailable/degraded이며 정상적인 빈 결과가 아닙니다.
- `PredictionMarket.yesPrice`: REST에서는 0~1. MCP get_prediction_markets 문서는 yesPrice를 0~100 스케일로 설명합니다.
- REST의 `yesPriceChange24h/7d`는 확률 스케일 변화이며 MCP change24h/7d의 percentage-point 스케일과 다릅니다.
- REST cursor는 문서상 현재 accepted but ignored입니다. cursor가 있다고 전체 과거 자료를 조회할 수 있다고 주장하지 않습니다.

원문은 탐색 응답에 가격이 없다고 설명하므로, 현재 schema에 `yesPrice`가 있어도 **Original BOOK에선 탐색 전용**으로 필터링합니다.
BOOK 자체 확률을 먼저 기록한 뒤 Kalshi/Gamma 가격을 별도로 결합해 가격 유출에 의한 추정 편향을 줄입니다.

MCP 도구 문서의 `cached_at`, `stale`, slice별 freshness 기준은 **그 도구에 대한 계약**입니다.
예를 들어 prediction seed는 90분을 넘으면 stale이고 news intelligence는 slice별 30/45/60분을 설명합니다.
이를 모든 로컬 REST 데이터에 일괄 적용하거나 원문의 48시간 BOOK 기준과 같은 규칙이라고 취급하지 않습니다.
필수 시각이나 freshness 평가가 없으면 unknown으로 남기고 해당 거래 신호를 차단합니다.

## 5. 단일 LLM과 독립된 Tool Adapter

앱이 관리하는 CHIEF·SCAN·VET·BOOK만 동일한 전역 LLM을 사용합니다.
World Monitor는 독립 데이터 서비스이며 자체 AI 분류·요약을 만들 수도 있어 그 내부 모델을 이 설정으로 통제할 수는 없습니다.
파생 AI 요약·예측은 원본 사건 자료와 분리하고 기본 판단 입력에는 출처·시각이 검증된 사실을 우선 사용합니다.
외부 확률을 BOOK의 독립 확률 추정으로 복사하지 않습니다.

Tool Adapter는 허용 route와 인자 스키마를 고정하고 응답 크기·timeout·동시 요청·호출 예산을 제한합니다.
뉴스·description·도구 응답 속 명령은 신뢰할 수 없는 데이터입니다. 시스템 지시·자격증명 요청·임의 코드 실행으로 승격하지 않습니다.
원문 payload, 정규화 버전, content hash, cycle별 snapshot ID를 저장해 모든 모델이 같은 자료를 재생할 수 있도록 합니다.

## 6. Hosted 옵션의 인증·비용·라이선스

Hosted 인터페이스는 비교 조사했으나 Original에 활성화하지 않습니다.
공식 api-keys 문서는 수동 API 키 발급을 API Starter/Business 플랜으로 한정하고 Pro의 MCP OAuth와 구별합니다.
SDK README의 “구독 키 필요” 요약과 최신 MCP 문서의 free-account 도구 권한은 상세 수준이 다릅니다.
get_sources는 익명 데이터 도구지만 다른 도구는 사용자 인증·플랜·quota 검증이 필요하다고 보수적으로 처리합니다.
public tools/list 성공은 데이터 tools/call 권한의 증거가 아닙니다.

플랫폼 코드는 AGPL-3.0-only, thin Python SDK는 MIT입니다. Hosted 출력 이용권·구독 조건은 코드 라이선스와 별개입니다.
[공식 license 문서](https://github.com/koala73/worldmonitor/blob/c0f783620dcdae713a6d28c239844c12311e2bf6/docs/license.mdx)를 참고해 self-host와 hosted 비용을 구분합니다.

## 7. 실제 검증과 다음 단계

공식 Python SDK 소스의 fake transport 기반 기존 테스트 **20개가 통과**했습니다.
JSON/SSE 파싱, API 키 header 구성, RPC 결과·오류, REST query 생성 등의 오프라인 계약 검사입니다.
로컬 REST 서비스, 데이터 seed, live MCP 인증이나 앱의 World Monitor Adapter가 검증된 것은 아닙니다.

Phase 3에서 고정 버전의 로컬 stack을 준비하고 세 endpoint의 유효 데이터·degraded 전이·BOOK 중지·복구를 검증합니다.
시작 프로세스와 seed 갱신은 snapshot만으로 유지된다고 가정하지 않고 재시작 절차를 구성합니다.

공식 자료: [README](https://github.com/koala73/worldmonitor/blob/c0f783620dcdae713a6d28c239844c12311e2bf6/README.md),
[Python SDK](https://github.com/koala73/worldmonitor/tree/c0f783620dcdae713a6d28c239844c12311e2bf6/sdk/python),
[MCP reference](https://github.com/koala73/worldmonitor/blob/c0f783620dcdae713a6d28c239844c12311e2bf6/docs/mcp-tools-reference.mdx),
[Prediction OpenAPI](https://github.com/koala73/worldmonitor/blob/c0f783620dcdae713a6d28c239844c12311e2bf6/docs/api/PredictionService.openapi.json).
