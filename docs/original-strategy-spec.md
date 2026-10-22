# 원본 전략 명세

상태: **본문 전체 확인·규칙 추출 완료 / 일부 실행 의미는 원문에 미정의**.
조사일: 2026-10-21, Asia/Seoul. 원문 표시 공개일: 2026-10-20.

## 1. 근거와 확인 범위

원문: [그록 에이전트 7개로 트레이딩 팀 만드는 법](https://lazyowen.com/guides/grok-bot-trading-team-setup).
Quick Start, STEP 1~4, 자주 막히는 곳, 공식 자료를 포함해 전달된 본문을 모두 읽었습니다.
응답 HTML SHA-256: `40514ff20c1d51c38a7b51898b044a4e4fcd6dc6f4648155eb9513b2c88f53c6`.

초기에는 프록시 CONNECT 403으로 실패했지만, 사용자가 전체 도메인 허용을 적용한 후 같은 URL에서 HTTP 200으로 다시 읽었습니다.
이 문서의 원문 위치는 아래 제목·역할별 지시문과 해당 짧은 인용으로 식별합니다. 이전의 접근 차단은 해결됐습니다.

운영 방식은 최신 사용자 지시가 우선합니다. CHIEF·SCAN·VET·BOOK은 동일한 전역 Provider·모델·환경변수 키를 사용하며,
SIZE·FILLS·RISK는 결정론적 Python입니다. 원문의 7개 역할을 유지하고 역할별 프롬프트를 분리합니다.
첨부 요청의 에이전트별 모델 설정·자동 대체 모델 구상은 적용하지 않습니다. PAPER ONLY는 원문과 사용자 요청 모두의 제약입니다.

## 2. 역할과 흐름

| 역할 | 원문 책임·권한 | 원문 위치 / 확인 |
| --- | --- | --- |
| CHIEF | 조율·감독·마감 보고. 포지션 개설·크기·청산을 결정하지 않고 RISK를 뒤집지 않음 | STEP 1 → 1. 총괄 봇 CHIEF / 확인 |
| SCAN | 변화 속도로 순위를 매긴 최대 10개 후보만 제출. 매수·목표가·수량 의견 금지 | STEP 3 → 2. SCAN / 확인 |
| VET | 싼 검사부터 순서대로 탈락시키고 첫 실패에서 중지. PASS/PASS_PARTIAL/REJECT | STEP 3 → 3. VET / 확인 |
| BOOK | 예측 시장의 결과·확률 판단. 금액은 SIZE에 요청만 함 | STEP 3 → 4. BOOK / 확인 |
| SIZE | 현금·Kelly·계좌 한도·유동성에 맞는 금액만 계산 | STEP 3 → 5. SIZE / 확인 |
| FILLS | 정해진 모의 주문·당시 가격·비용·부분 체결 기록 | STEP 3 → 6. FILLS / 확인 |
| RISK | 최종 청산 권한. 자체 타이머로 실행하며 다른 에이전트 뒤에서 기다리지 않음 | STEP 3 → 7. RISK 및 STEP 1 / 확인 |

CHIEF의 일반 인계 설명은 `SCAN → VET → BOOK → SIZE → FILLS → RISK`입니다.
실제 그룹·시작 지시문은 밈코인 `SCAN → VET → SIZE → FILLS → RISK`, 예측 시장 `CHIEF/BOOK/SIZE`로 분리합니다.
BOOK을 밈코인 진입의 필수 단계로 추가하지 않습니다. 예측 시장의 실제 체결·청산 상세는 원문에 별도로 완결되어 있지 않습니다.

## 3. SCAN 후보 조건

원문 위치: [STEP 3 → 2. SCAN](https://lazyowen.com/guides/grok-bot-trading-team-setup#2-scan-후보-찾기), 각 bullet 확인.

| ID | 확인된 규칙 | 인용·경계 |
| --- | --- | --- |
| SCAN-01 | DexScreener 런치 데이터에서만 후보 탐색. 검색·토큰 조회는 무키 GET | “후보는 덱스스크리너 런치 데이터에서만” |
| SCAN-02 | 절대 보유자 수보다 증가 속도 우선 | 30→90 증가와 400 정체의 비교는 예시이며 기간은 미정의 |
| SCAN-03 | 보유자가 가격보다 빠르게 늘면 가장 강한 신호. 가격 증가가 더 빠르면 경고·순위 상향 금지 | 증가율의 시간창·점수식은 미정의 |
| SCAN-04 | 최근 두 구간 모두 매도 수가 매수 수보다 많으면 제외 | “최근 두 구간” 길이는 미정의; 중첩 h1/h6를 연속 구간으로 단정하지 않음 |
| SCAN-05 | `liquidity.usd < 10000`이면 제외 | 초기값 10,000달러. 정확히 10,000은 이 조건만으로 제외되지 않음 |
| SCAN-06 | 보유자 수를 모르면 `holders: null`과 출처 표기 | 거래 횟수·주소 수로 보유자를 추정 생성하지 않음 |
| SCAN-07 | 최대 10개. 통과 후보가 3개 미만이어도 그대로 제출 | 후보를 채우기 위한 조건 완화 금지 |
| SCAN-08 | World Monitor 장애 시 `degraded: true`로 DexScreener 기반 탐색 계속 | 필수 온체인 사실까지 누락 허용한다는 뜻은 아님 |

## 4. VET 검증·거절 순서

원문 위치: [STEP 3 → 3. VET](https://lazyowen.com/guides/grok-bot-trading-team-setup#3-vet-걸러내기). 다음 순서를 보존합니다.

1. Solana `getTokenLargestAccounts(mint)`와 `getTokenSupply(mint)`로 최대 보유량/공급량을 계산합니다. **5% 초과** 또는 RPC 무응답이면 REJECT입니다.
2. `liquidity.usd < 10000` 또는 `liquidity.usd < proposed_order_usd × 50`이면 REJECT입니다.
3. 가격은 오르는데 보유자 수가 그대로이면 REJECT입니다.
4. 이미 1시간 가격 변화 `priceChange.h1`에 반영된 신호이면 REJECT입니다.
5. 세계 뉴스가 근거인 후보는 주장이 한 곳에만 있거나 확인할 수 없으면 REJECT입니다.
6. 왕복 수수료를 빼고 남는 것이 없으면 REJECT입니다.
7. `pairCreatedAt` 기준 **30분 미만**이면서 유동성이 이미 줄고 있으면 REJECT입니다. 나이가 짧다는 조건 하나만으로 제외하는 규칙이 아닙니다.

첫 탈락에서 멈춥니다. 결과는 PASS, PASS_PARTIAL, REJECT 중 하나입니다.
하루 탈락이 10개 미만이면 필터 설정 이상을 보고합니다. 임의로 탈락 건수를 만들거나 자동으로 기준을 올리지 않습니다.

원문은 “가장 큰 지갑”이라고 표현하지만 해당 RPC는 **token account** 상위 20개를 반환합니다.
동일 소유자의 여러 token account를 합산한 실소유 지갑 집중도와 같다고 주장할 수 없습니다.
원문식 지표는 `largest_token_account_amount / total_supply_amount`라는 proxy로 기록하고, 실제 지갑 집중도는 별도 데이터로 구별해야 합니다.

## 5. BOOK 예측 시장 조건

원문 위치: [STEP 3 → 4. BOOK](https://lazyowen.com/guides/grok-bot-trading-team-setup#4-book-예측-시장).

- 로컬 World Monitor `/api/prediction/v1/list-prediction-markets?page_size=100&category=crypto`에서 시장을 탐색합니다.
- `dataAvailable == false` 또는 `fetchedAt == 0`이면 중지·보고합니다.
- 가격은 Kalshi와 Polymarket Gamma에서 읽습니다. HTTP 200이 아닌 소스는 건너뛰고, 같은 질문인지 확실하지 않으면 매칭하지 않습니다.
- **가격을 보기 전에 자체 확률을 먼저 확정**합니다. 확률 산출 입력에는 비교용 시장 가격을 노출하지 않고 출력·시각·해시를 저장한 뒤 가격을 결합합니다.
- 자체 확률과 시장 가격의 차이가 **8%를 넘을 때만** 진입을 요청합니다. 부호·퍼센트포인트 해석의 정확한 수식은 원문에 없습니다.
- 시장 뒤의 근거 데이터가 **48시간보다 오래됐으면** 제외합니다. 조회 시각이 새롭다는 이유로 근거 데이터의 나이를 초기화하지 않습니다.
- 지는 베팅에 물타기하지 않으며 열린 베팅은 최대 **7개**입니다. 금액 결정은 SIZE만 수행합니다.

원문은 World Monitor 탐색 응답에 가격이 없다고 설명합니다. 조사한 현재 공식 OpenAPI에는 `yesPrice`가 있습니다.
원본 전략은 이를 자체 확률이나 체결가격으로 사용하지 않고 **탐색 전용**으로 유지합니다. 버전 차이를 감추지 않습니다.

## 6. SIZE와 Kelly

원문 위치: [STEP 3 → 5. SIZE](https://lazyowen.com/guides/grok-bot-trading-team-setup#5-size-얼마나), STEP 4 시작 잔고 설명.

- 묶이지 않은 현금만 사용합니다.
- Kelly로 계산하되 주문당 **계좌의 6%가 상한**입니다. 6%를 목표 비중으로 채우지 않습니다.
- 유동성이 줄면 금액도 줄입니다. **2% 슬리피지 예산으로 이탈 불가능한 크기는 부적합**합니다.
- 밈코인 최초 진입은 주문 하나·크기 하나이며 나눠 사지 않습니다.
- **+50% 상승 확인 후 되돌림에서 한 번만 추가**할 수 있습니다. 추가 횟수를 포지션 기록으로 추적해야 합니다.
- 최소 주문 초기값은 **50달러**입니다. 가용 현금이 미달하거나 수수료를 감당할 수 없으면 0과 이유를 반환합니다.
- 모의 시작 잔고는 **1,000달러**입니다. 6% 상한은 60달러이며 최소 50달러보다 큽니다.

**원문은 Kelly라는 이름과 6% 상한만 제시하며 수식 자체는 제시하지 않습니다.**
승률·배당 입력, 음수 Kelly, fractional Kelly 여부, 밈코인 손익 배당의 정의, 계좌 분모와 비용 포함 방식이 미정의입니다.
교과서 수식을 원문에 있는 것처럼 채워 넣지 않습니다. 이 부분을 명시적으로 정하기 전에는 실행 가능한 정확 복제라고 할 수 없습니다.

## 7. FILLS 모의 체결

원문 위치: [STEP 3 → 6. FILLS](https://lazyowen.com/guides/grok-bot-trading-team-setup#6-fills-모의-체결).

```text
fee_rate = 0.0045
fee_floor_usd = 0.95
fee_usd = max(fee_rate * order_usd, fee_floor_usd)
effective_fee = fee_usd / order_usd
slippage_limit = 0.02
```

20달러 주문은 `effective_fee = 0.0475 = 4.75%`이므로 원문 예시에서는 FEE_FLOOR로 돌려보냅니다.
50달러는 최소 수수료 기준 1.9%입니다. 모든 주문의 FEE_FLOOR 경계 수식까지 원문에 명시된 것은 아닙니다.

당시 가격과 보냈을 주문을 기록하며 실제 주문은 보내지 않습니다.
공급량 기준을 넘는 큰 지갑의 매도가 관측되면 그 물량에 팔지 않고 기다리며 보고합니다.
슬리피지가 2%를 넘으면 크게 표시하고, 부분 체결은 실제 부분 수량대로 기록합니다.
“표시”를 모든 경우의 자동 거래 거절이나 청산 규칙으로 바꾸지 않습니다. 부분 체결의 수수료 적용 단위와 체결 모델은 미정의입니다.

## 8. RISK 청산과 수식

원문 위치: [STEP 3 → 7. RISK](https://lazyowen.com/guides/grok-bot-trading-team-setup#7-risk-청산), STEP 4 검산 예시.

```text
avg_6h = volume.h24 / 4
ratio = volume.h6 / avg_6h
ratio < 0.20  => CLOSE_ALL
```

- **5분마다** 독립 검사합니다. `ratio == 0.20`은 이 조건으로 청산하지 않습니다.
- 응답이 없으면 **2회 재시도**, 그래도 없으면 청산 결정입니다. 재시도 간격은 미정의입니다.
- 시가총액 **+50%까지 보유**, **+60%에서 표시하고 계속 진행**합니다. +50%/+60%를 익절 청산으로 변환하지 않습니다.
- 기대·당일 성과를 이유로 기준을 완화하지 않습니다.

원문 예시: `h24=4717.70`, `h6=167.75` → 정확한 `avg_6h=1179.425`, 표시 반올림 `1179.43`, `ratio≈0.14223` → CLOSE.
중간 반올림 방식은 원문에 명시되지 않았습니다. 경계 오차를 줄이려면 유효한 양수 h24에서 `20 * h6 < h24`로 같은 조건을 비교할 수 있습니다.

미응답의 청산 **결정**은 유지하되 현재 가격이 없으면 FILLS는 가격을 만들어 체결하지 않습니다.
이때 `CLOSE_REQUESTED_UNPRICED`와 Kill Switch/신규 진입 차단으로 기록하고 유효 가격 확보 후 가상 체결합니다.
이 상태 표현은 PAPER ONLY를 구현하기 위한 시스템 설계이며 원문에 있는 체결 알고리즘은 아닙니다.

## 9. 감독·주기·World Monitor

| 항목 | 원문 규칙 | 원문 위치 / 확인 |
| --- | --- | --- |
| SCAN | 15분마다 탐색 후 VET→SIZE→FILLS; 체결된 계약 주소를 RISK에 전달 | STEP 4 시작 프롬프트 / 확인 |
| RISK | 5분마다 열린 포지션 검사 | STEP 3 RISK·STEP 4 루틴 / 확인 |
| 마감 보고 | 매일 23:00 Asia/Seoul, 즉 14:00 UTC | STEP 4 CHIEF 보고 프롬프트 / 확인 |
| 감독 | 2회 무응답이면 1회 재시작·기록; 같은 봇이 1시간 내 2회 죽으면 데스크 중단·강제 청산 없이 보고 | STEP 1 CHIEF / 확인 |
| 장중 알림 | 숫자가 10% 넘게 움직일 때만 메시지; 비교 기준은 미정의 | STEP 1 CHIEF / 확인 |
| World Monitor | 로컬 self-host; 3개 검사 모두 데이터가 있어야 READY. 비거나 오류면 degraded, BOOK 정지 | Quick Start·STEP 2 / 확인 |
| degraded 운영 | 밈코인 데스크는 DexScreener 자료로 계속; 기존 포지션은 RISK가 담당 | STEP 1 CHIEF·STEP 2 / 확인 |

원문은 READY를 만들기 위해 `api.worldmonitor.app`으로 바꾸는 것을 명시적으로 금지합니다.
[World Monitor 연동 설계](world-monitor-integration.md)는 원본용 **로컬 REST**를 기본으로 하고, 유료 hosted REST/MCP/SDK 경로를 자동 대체로 사용하지 않습니다.

## 10. 원문에 없는 실행 의미와 차이

| ID | 미정의·충돌 | Phase 2 이후 처리 원칙 |
| --- | --- | --- |
| OPEN-01 | Kelly 정확한 공식·입력·계좌 분모 | 승인된 해석을 별도 명세로 기록하기 전 SIZE 활성화 차단 |
| OPEN-02 | VET가 SIZE 이전인데 주문 금액의 50배 유동성을 요구 | 사전 quote/최종 재검사 구조를 검토하되 원문이라고 주장하지 않음 |
| OPEN-03 | PASS_PARTIAL의 의미·허용 크기 | PASS로 자동 승격하지 않음 |
| OPEN-04 | 보유자·가격 변화 시간창, 두 최근 구간, 가격 선반영 판정 | 사실 근거와 비교 창을 명시; DexScreener에 없는 holders는 별도 소스 필요 |
| OPEN-05 | 되돌림 정의, +50% 기준, 진입 시가총액 기준 | 최초/추가 진입 상태와 기준점을 명세하기 전 추가 매수 차단 |
| OPEN-06 | 체결 가능성·부분 체결·최소 수수료 적용 단위·큰 지갑 매도 감지 | 문서화된 모의 체결 정책 필요; 표시가격 즉시 전량 체결을 원문으로 가정하지 않음 |
| OPEN-07 | BOOK 8% 차이의 부호/단위, 예측 시장 청산·결제 | 원문 인용과 구현 해석을 분리; 불리한 방향을 임의 승인하지 않음 |
| OPEN-08 | RISK h24=0·결측·여러 페어 중 선택·재시도 간격 | 결측을 0으로 채우지 않고 재시도/청산 의도/미평가 상태를 구분 |
| OPEN-09 | 현재 WM schema의 가격, 보유 계정과 실소유 지갑 차이 | 데이터 버전과 proxy 의미를 공개; 사실과 다른 필드명을 쓰지 않음 |

추가로 컨트랙트 보안 검사, DB 고유키, 불변 스냅샷, Kill Switch는 플랫폼 요구입니다. 원문에 없는 세부 규칙을 원본에서 추출했다고 주장하지 않습니다.
Original Strategy의 확인된 기본값은 버전으로 고정하고 Experimental Strategy는 초기 비활성화합니다.
현재 실행 엔진은 없으며 `original_strategy_verified=true`를 선언하지 않습니다. Phase 1은 이 추적표와 미정의 항목을 포함한 분석 산출물 단계입니다.
