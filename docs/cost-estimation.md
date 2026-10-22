# 비용 추정과 기록 계획

상태: Phase 1 계산 방법 + 로컬 CPU 모델 사용량 기록. 현재 `local_inference_usage`를 수집하며 외부 API 단가를 적용하지 않습니다.
CPU·메모리·서버 비용은 별도이고 확정 월 비용이나 수익성 수치는 제시하지 않습니다.

## 1. 단일 모델과 비용 경계

모든 AI 역할이 같은 Provider·모델·키를 쓰지만 하나의 요청만 하는 것은 아닙니다.
SCAN 후보 수, VET 호출 수, BOOK 실행 횟수, CHIEF 보고, 재시도가 총 요청량을 결정합니다.
SIZE·FILLS·RISK의 자체 처리는 LLM 토큰 비용이 없습니다.

원문은 Grok Bot 구독으로 봇을 실행합니다. 범용 시스템은 그 구독을 필수로 요구하지 않으며 선택한 모델 API 또는 로컬 추론 자원을 사용합니다.
챗봇/앱 구독에 API 크레딧이 포함된다고 가정하지 않습니다.
CLI의 기존 로그인으로 추론할 수 있어도 무료 사용을 의미하지 않습니다. 구독/API/로컬 과금의 경계를 확인해야 합니다.
native CLI가 토큰을 보고해도 과금 방식이 미확인인 `cli_usage_unknown_billing`은 비용을 N/A로 유지합니다.
전역 키와 단가를 명시한 `cli_api_usage`만 API 비용 추정에 사용하며, 실제 청구 내역과 구별합니다.

| 모델/서비스 | 가능한 비용 | 현재 상태 |
| --- | --- | --- |
| OpenAI / Anthropic / Google / xAI | 입력·출력·Provider별 reasoning/cache/tool 비용, 무료 허용량 초과 | 실제 모델·계정 요금표 확인 전 금액 미확정 |
| OpenAI-compatible 원격 | 해당 운영자의 모델 단가·최소 과금·추가 기능 비용 | 공급자별 확인 필요 |
| 로컬 Ollama | 외부 API 단가는 없을 수 있으나 CPU/GPU·RAM·디스크·전력·운영비 발생 | 모델 크기·호스트 자원 미선정 |
| World Monitor self-host | 코드 이용 자체와 별개로 서버·Redis·seed 실행·데이터 보관 자원 | Original 기본 경로; full stack 미기동 |
| WM hosted REST/MCP | 플랜별 허용·quota·이용권 비용 | 조사한 옵션, Original 자동 대체로 사용하지 않음 |
| DexScreener / Kalshi 공개 조회 | 원문은 무키 조회 사용. 현재 endpoint 접근과 앞으로의 이용 조건은 별개 | 공개 조회 성공; 장기 SLA/무료 보장은 하지 않음 |
| holder-history·지갑 인덱서·보안 자료 | plan·chain·retention·요청 단가 | 공급자 미선정; 비용 미확정 |
| 저장/운영 | PostgreSQL·snapshot storage·백업·로그·네트워크·대시보드 | 규모와 보관 기간에 따라 산정 |

모델명과 현재 사용 가능 여부·가격은 환경변수/공식 요금표/probe로 확정합니다.
무료 tier, subscription, provider reasoning 기능을 전 모델에 일반화하지 않습니다.

## 2. 비용식

```text
request_cost =
    uncached_input_tokens / 1_000_000 * input_price_per_million
  + cached_input_tokens   / 1_000_000 * cache_price_per_million
  + billable_output_tokens / 1_000_000 * output_price_per_million
  + provider_specific_fees

llm_cost = sum(request_cost for all attempts)
operating_cost = llm_cost + data_service_cost + compute_cost + storage_cost + network_cost
paper_trading_net_pnl = gross_realized_pnl + gross_priced_unrealized_pnl - simulated_trading_fees
research_net_after_operating_cost = paper_trading_net_pnl - operating_cost
```

Provider별 reasoning token과 cache write/read 과금 방식은 정규화할 때 개별 처리합니다.
위 입력·출력 구분이 중복되지 않도록 하고 Provider가 출력에 포함한 reasoning 비용을 다시 더하지 않습니다.
모의 슬리피지가 체결가격에 이미 반영됐다면 손익에서 이중으로 빼지 않습니다.

미평가 포지션이 있으면 전체 equity/PnL을 계산할 수 없으므로 확인된 부분·unpriced 부분·전체 산정 불가를 구별합니다.
API usage가 빠졌다면 비용을 0으로 처리하지 않고 unknown/estimated로 저장해 실제 청구액과 구별합니다.
위 gross 손익은 수수료 차감 전 값입니다. 장부 손익이 이미 수수료를 포함했다면 이를 다시 빼지 않습니다.
매매 손익과 연구 운영비는 별도 항목으로 표시하고 합산 지표에는 정의를 붙입니다.

## 3. 원문 주기에 따른 호출량 예시

SCAN 15분 주기는 24시간 운영 시 하루 96사이클, RISK 5분 주기는 하루 288회입니다.
후자는 Python 처리이므로 LLM 요청에 포함하지 않습니다. 원문에는 BOOK의 고정 실행 주기가 없습니다.

SCAN이 사이클마다 1회, 최대 10후보의 VET가 각각 1회, CHIEF 정시 보고가 1회이고 BOOK이 degraded로 쉬는 상황을 가정하면,
`96 + 96 × 10 + 1 = 1057` requests/day입니다.
이는 호출 설계를 설명하는 **가정하의 상한 예시**이며 실측값이나 권장 예산이 아닙니다.
Python 검사에서 먼저 탈락한 후보, 후보 없음, 캐시, BOOK, 재시도, 추가 보고에 따라 실제 수량은 달라집니다.

토큰 추정에는 역할별 실측 입력·출력 분포, 후보 수, 재시도율, 운영 시간과 현재 단가가 필요합니다.
단가가 미지정이면 0원 추정치를 표시하지 않고 수량과 계산식만 제공합니다.

## 4. 원문의 거래 비용

FILLS의 확인된 규칙은 `fee_usd = max(order_usd × 0.0045, 0.95)`입니다.
20달러 주문은 4.75%, 50달러 주문은 1.9%이며 원문은 20달러 예시를 FEE_FLOOR로 차단합니다.
거래 최소 크기·fee floor와 LLM API 단가는 다른 비용입니다.

1,000달러 모의 계좌의 6% 상한은 60달러입니다. 비용을 상한에 포함하는지, 부분 체결마다 최소 수수료를 부과하는지는 원문에 정의돼 있지 않습니다.
슬리피지 예산 2%는 실측값을 언제나 2%로 설정하라는 의미가 아닙니다.
유동성·부분 체결 모델을 확정하지 않은 상태에서 정확한 순이익을 계산했다고 보고하지 않습니다.

## 5. 예산 관리와 비교 실험

각 시도에 cycle/role, Provider·모델, request ID, usage 출처, 단가 버전·통화, 지연, 실패 분류를 저장합니다.
호출 전 토큰 상한×단가로 예산을 예약하고 응답 후 확정값 또는 unknown으로 갱신합니다.
초과·단가 불명 시 운영 방침을 명시하며 예산 중단이 Python RISK나 Kill Switch까지 멈추지 않게 합니다.

비교 실험은 같은 스냅샷과 경제 조건의 순차 실행입니다. 모델별 실패율·재시도 비용·표본 수·동일 판단율·확률 보정을 함께 표시합니다.
성능이 나쁜 모델의 실패 사이클을 지워서 수익률이 높아 보이게 만들지 않습니다.

이번에는 과금되는 모델 생성이나 유료 데이터 구독을 실행하지 않았습니다. 단가 확인·실모델 probe·장기 운영비 계측은 향후 작업입니다.
