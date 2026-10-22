# 무료 시장 관측

DexScreener가 제공하지 않는 원천 관측 시각, 보유자 이력, 큰 지갑 매도, 실행 가능한 깊이를 무료 공개 자료로 채웁니다.
구현은 [observer.py](../src/trading_team/observer.py)와 [data.py](../src/trading_team/data.py)의 `SolanaOnchain`에 있습니다.
모두 읽기 전용 조회입니다. 스왑 거래 생성·서명·지갑 연결 경로는 없습니다.

## 자료 출처

| 필드 | 출처 | 키·비용 |
| --- | --- | --- |
| `observed_at`, `price`, `buy_quote` | Jupiter `GET /swap/v1/quote`를 크기별로 호출한 견적 사다리. 시각은 견적 slot의 Solana `getBlockTime` | 키 없이 30회/분. 무료 키 60회/분 |
| `holder_count`, `holder_observed_at` | Solana RPC `getProgramAccounts`로 mint의 모든 token account의 소유자·수량만 조회 | 공개 RPC 무료 |
| `largest_token_account_share`, `onchain_*` | 같은 조회의 소유자별 합계 중 유동성 풀이 아닌 최대 보유자 / 총 공급량 | 공개 RPC 무료 |
| `contract_safe`, `contract_source` | `getAccountInfo` mint 권한과 Token-2022 확장 | 공개 RPC 무료 |
| `previous_*` | 이 장부가 5–30분 전에 저장한 같은 pool의 관측 | 자체 DB |
| `windows` | GeckoTerminal pool trades의 체결 시각·방향 | 키 없이 30회/분 |
| `large_holder_selling` | 같은 trades의 매도자 ∩ 최대 보유자 상위 20 | 키 없이 30회/분 |
| `sell_quote` (RISK) | 보유 수량에 대한 Jupiter 매도 견적 사다리 | Jupiter와 동일 |

유동성·거래량·시가총액·pair 생성 시각은 기존대로 DexScreener에서 같은 수집 중에 읽습니다.
해당 값에는 공급자 시각이 없으므로 `fetched_at`과 Jupiter `observed_at`이 모두 300초 이내일 때만 SCAN 대상이 됩니다.

## 구현 해석

| 항목 | 이 구현의 선택 |
| --- | --- |
| 실행 깊이 | 최대 크기의 1/16·1/4·1/2·1배 누적 견적 차이를 한계 가격 단계로 사용. 가격은 보수적 방향으로 단조화. 실제 호가창이 아니라 Jupiter 경로 견적 |
| 최대 크기 | 매수는 `2 × max(50, PAPER_INITIAL_CASH × 6%)` USD. 매도는 보유 수량 |
| 가격 단위 | USDC를 USD로 간주. 디페그는 반영하지 않음 |
| 관측 시각 | 사다리 견적 중 가장 오래된 slot의 블록 시각. 수집 시각으로 대체하지 않음 |
| 보유자 | 수량이 0보다 큰 token account의 소유자 수. 유동성 풀은 지갑이 아니므로 제외 |
| 최대 보유자 | 원문의 “가장 큰 지갑”에 맞춰 같은 소유자의 token account를 합산하고 유동성 풀을 제외. 필드 이름은 호환을 위해 유지 |
| 유동성 풀 판정 | 이 mint의 DexScreener pair 주소, 그리고 실제 pool vault로 확인한 공용 권한 주소(Raydium AMM v4·CPMM, Meteora DAMM v2·DBC). 확인하지 않은 유형은 지갑으로 계산해 더 엄격하게 판정 |
| 계약 점검 | mint·freeze 권한이 없고, Token-2022는 `metadataPointer`·`tokenMetadata` 외 확장이 없을 때만 안전. 포괄적 감사가 아님 |
| 보유자 이력 | 같은 pool의 자체 관측 중 현재보다 5–30분 앞선 가장 최근 값. 없으면 unknown이며 VET가 차단 |
| 두 구간 | Jupiter 관측 시각까지의 연속된 5분 구간 두 개. 300건 한 페이지가 첫 구간 시작에 닿지 않으면 불완전으로 보고 unknown |
| 큰 지갑 매도 | 위 10분 안에 최대 보유자 상위 20 지갑이 매도자(`tx_from_address`)로 나타나면 true. 잔고 감소만으로 판단하지 않음 |
| 처리 예산 | 수집마다 유동성 1만 달러 이상인 Solana 자산 중 가장 유동성이 큰 pool 기준 상위 `MARKET_OBSERVER_MAX_ASSETS`개만 관측 |
| 큰 토큰 | token account 3만 개 또는 응답 10MB를 넘으면 보유자 조회를 중단하고 1시간 동안 재시도하지 않음. 보유자는 unknown |

한 출처가 실패해도 다른 출처의 관측은 저장합니다. 실패한 필드는 unknown으로 남고 해당 진입은 결정론적 게이트에서 차단됩니다.
GeckoTerminal 거래 목록의 색인 지연은 알 수 없으므로 마지막 구간의 최근 체결이 늦게 반영될 수 있습니다.

## 설정과 비용

```dotenv
MARKET_OBSERVER_ENABLED=true
MARKET_OBSERVER_MAX_ASSETS=3
# 선택: Jupiter 포털의 무료 키. 비워 두면 키 없이 동작합니다.
JUPITER_API_KEY=
```

기본 구성은 **비용 없이** 동작합니다. 모든 출처가 키 없는 공개 API이며 요금이 청구되는 호출은 없습니다.
자산 하나를 관측할 때 Jupiter 4회, Solana RPC 4회, DexScreener 1회, GeckoTerminal 1회를 호출합니다.
Jupiter와 GeckoTerminal 호출은 자체 간격(키 없이 2.2초)을 두고, Jupiter 429는 10초 뒤 두 번까지 재시도합니다.

| 상황 | 권장 조치 | 비용 |
| --- | --- | --- |
| Jupiter 429가 잦음 | Jupiter 포털 무료 키를 `JUPITER_API_KEY`에 설정 | 무료 |
| GeckoTerminal 429가 지속 | 데이터센터·공유 IP에서 흔함. 개인 PC에서 실행 | 무료 |
| 공개 Solana RPC 429·시간 초과 | 경로에 키가 있는 무료 RPC를 `SOLANA_RPC_URL`에 설정 (query 키는 허용하지 않음) | 무료 등급 |
| 더 많은 자산·짧은 주기 | 유료 RPC·CoinGecko 유료 플랜 | 유료, 선택 사항 |

공개 API의 무료 이용 조건과 한도는 공급자가 바꿀 수 있습니다. 장기 무료를 보장하지 않습니다.

## 검증 범위

조회 경로와 계산은 모의 HTTP 응답으로 테스트하고, 실제 공개 API로도 실행했습니다. 결과는 [검증 보고서](validation-report.md)에 있습니다.
이 클라우드 환경의 공유 IP에서는 GeckoTerminal이 계속 429를 반환했습니다. 응답 형식은 다른 네트워크 경로의 실제 응답으로 확인했습니다.
