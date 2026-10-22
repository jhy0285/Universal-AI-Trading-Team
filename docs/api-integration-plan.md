# API 연동 계획

상태: Phase 1 공개 읽기 조사. 실제 주문·서명·자금 전송 API는 구현 대상이 아닙니다.

## 1. 공급자별 조사 결과

| 공급자 | 원문 또는 공식 인터페이스 | 실제 확인 / 제한 |
| --- | --- | --- |
| DexScreener | GET /latest/dex/search?q=BONK | HTTP 200, pairs 30개. 이름만으로 mint를 확정하지 않음 |
| DexScreener legacy token | GET /latest/dex/tokens/{contract} | 최초 generic client 요청 403. 앱 식별 User-Agent를 넣은 재요청은 HTTP 200, pairs 30개, h6/h24 존재 |
| DexScreener current token | 공식 GET /tokens/v1/{chainId}/{tokenAddresses} | Solana BONK 조회 HTTP 200, 1개 pair, h6/h24 존재. legacy와 결과 집합 동일성을 가정하지 않음 |
| World Monitor | 원문의 로컬 REST 3개 검사·prediction 탐색 | 공식 route/schema 확인. 로컬 서비스 설치·기동·live 검증은 미실행 |
| Kalshi | GET https://external-api.kalshi.com/trade-api/v2/markets?limit=100&status=open | HTTP 200, markets 100개 및 cursor 확인 |
| Polymarket | GET https://gamma-api.polymarket.com/markets?closed=false&order=volumeNum&limit=50 | HTTP 451, 응답 본문 error code: 1026. 유효 시장 스냅샷 없음 |
| Solana | JSON-RPC getTokenSupply / getTokenLargestAccounts | supply는 HTTP 200/result 존재; 최대 계정 조회는 HTTP 429 |

응답 개수는 해당 조회의 관측 결과이며 전체 시장 개수나 데이터 완전성의 보장이 아닙니다.
구형 Polymarket `py-clob-client` 공식 README는 archived/nonfunctional로 선언하고 [py-sdk](https://github.com/Polymarket/py-sdk)를 안내합니다.
구형 패키지는 채택하지 않습니다. 새 SDK도 지갑·주문 기능이 함께 있어 무심코 통째로 Adapter 권한을 주지 않습니다.
초기 구현은 검증된 공개 데이터 route만 가진 읽기 전용 HTTP Adapter를 우선하며, 공식 SDK는 schema·공개 조회 계약의 참고 자료로 사용합니다.

## 2. Adapter 계약

모든 데이터 Adapter는 정규화된 다음 상태를 반환하도록 설계합니다.

```text
provider, endpoint_id, source_id, chain_id / market_id,
observed_at | null, fetched_at, raw_payload_hash, schema_version,
quality = valid | stale | unavailable | partial | invalid,
missing_fields, coverage, original_source_refs, payload
```

HTTP 200, JSON 파싱 성공, 의미상 유효 자료를 별도 단계로 검증합니다.
0, null, 빈 목록, 누락, 잘못된 단위와 공급자 오류를 서로 바꾸지 않습니다.
모든 신호는 필요한 필드·자료 시각·coverage를 선언하며, 충족되지 않은 후보는 거래 불가입니다.

timeout·연결 오류·429는 공급자 지침과 Retry-After를 존중하고 제한적으로 재시도합니다.
지속되는 401/403/451에 키를 발명하거나 주소·프록시를 바꿔 우회하지 않습니다.
이번 451은 응답만으로 지역 차단·원격 정책·중간 프록시 중 어느 원인인지 확정하지 않았습니다.
BOOK은 원문에 따라 HTTP 200이 아닌 가격 소스를 건너뜁니다. 단일 소스가 응답해도 시장 매칭·기초자료·가격 신선도 검증은 별도로 필요합니다.

## 3. DexScreener와 SCAN/RISK

[공식 API 문서](https://docs.dexscreener.com/api/reference)의 Pair schema와 실제 응답에서 priceUsd, txns, volume,
priceChange, liquidity, marketCap, pairCreatedAt 등을 확인했습니다. nullable/누락 가능성을 보존합니다.
공식 Pair schema와 조회 결과에는 보유자 수가 없으므로 `holders`를 거래 횟수에서 만들지 않습니다.

- 자산의 키는 chain + mint/address이고 시장의 키는 chain + pairAddress입니다. ticker 이름은 식별자가 아닙니다.
- 가격 string은 검증된 Decimal로 처리하고 volume.h6/h24의 단위와 원래 필드명을 함께 저장합니다.
- m5/h1/h6/h24는 서로 겹치는 창입니다. SCAN의 “최근 두 구간”과 자동으로 같다고 하지 않습니다.
- RISK는 진입 때 선택한 pair와 source의 연속성을 추적해야 합니다. 여러 pair 중 선택 방식은 원문 미정의 사항입니다.
- 거래 가능한 깊이·체결 확률은 24시간 거래량이나 liquidity.usd 하나에서 추측하지 않습니다.
- original legacy route와 새 route의 호환성은 고정 샘플로 검증하고, API 변화가 전략 파라미터 변경을 숨기지 않도록 합니다.

## 4. 온체인·보유자·컨트랙트 데이터

원문의 VET는 Solana를 명시하므로 첫 체인의 RPC 계약은 Solana로 잡습니다.
[공식 getTokenLargestAccounts 문서](https://solana.com/docs/rpc/http/gettokenlargestaccounts)는 **상위 20 token accounts**를 반환한다고 명시합니다.
`getTokenSupply`의 raw amount와 같은 mint의 account amount를 같은 정수 단위로 비교해 원문 proxy의 5% 초과를 검사합니다.
RPC 무응답은 원문상 REJECT입니다. 현재 최대 계정 조회 429를 정상 집중도 검사 통과로 기록할 수 없습니다.

필수 추가 자료는 다음과 같습니다.

- 보유자 수의 과거·현재 관측값과 동일 시간창. 과거 스냅샷이 없으면 성장률 unknown.
- token account 소유자 매핑과 실제 지갑 집계. top-20 account의 합을 전체 holder 수로 사용하지 않음.
- 큰 지갑의 매도 관측: 계정·DEX 거래·관측 시각 근거. 단순 잔고 감소만으로 매도라고 단정하지 않음.
- 컨트랙트/토큰 프로그램 보안 사실과 출처. 확인하지 않은 위험을 safe=false/true로 임의 생성하지 않음.

이 요구를 만족할 인덱서·과거 자료 공급자는 아직 선정하지 않았습니다.
인증이 필요한 공급자를 선택한다면 실제 설정의 변수 이름·바인딩 준비 상태부터 확인하고 남은 요구만 환경 설정으로 선언합니다.
비밀값을 채팅이나 저장소에서 요청하지 않습니다.

## 5. 예측 시장 가격·매칭

원문 경로는 World Monitor 탐색 → 자체 확률 확정 → Kalshi/Gamma 가격 결합입니다.
모델의 확률 입력에서 WM yesPrice와 외부 시장가격을 제거하고, 확률 결과를 저장한 뒤 후속 가격 수집을 수행합니다.

Kalshi 실제 응답에는 `yes_ask_dollars`, `yes_bid_dollars`, `no_ask_dollars`, `last_price_dollars` 등의 필드가 있었습니다.
현행 달러 문자열과 구형 센트 필드를 혼동해 100으로 두 번 나누지 않습니다. 의미·단위는 schema 버전에 고정합니다.
Gamma의 outcomePrices/token IDs 등의 계약은 공식 문서·새 SDK를 참조하되 이번 환경에서 유효한 live 응답은 확보하지 못했습니다.

동일 질문은 제목 유사도만으로 매칭하지 않습니다. outcome, 마감 시각, 판정 기준, venue market ID를 확인합니다.
체결 가능한 bid/ask와 마지막 가격·중간값·확률을 구별하고, 수수료·깊이 없이 표시가격을 가상 체결가격으로 확정하지 않습니다.
원문의 8% 조건은 부호·확률단위 해석을 명시한 뒤 적용하며 열린 베팅 7개·48시간 근거·손실 베팅 물타기 금지를 Python이 검증합니다.
실제 order creation, API credential derivation, 지갑 서명 endpoint는 노출하지 않습니다.

## 6. 스냅샷·신선도·보안

응답 원본과 정규화 스키마 버전, 요청 인자, source timestamp, 수집 시각, 해시를 저장합니다.
secret이 있는 header·query는 원본 요청 로그에 넣지 않습니다. 뉴스 문구와 자산 description은 명령이 아닌 외부 데이터입니다.
Adapter는 endpoint allowlist·인자 스키마·출력 크기·호출 예산을 강제합니다.

신선도는 자료 종류별 계약입니다. BOOK의 원문 48시간 기준과 가격 quote TTL, WM seed의 stale 기준을 별도로 관리합니다.
빠르게 다시 조회한 오래된 사건을 새 데이터로 취급하지 않습니다. 완전한 과거 자료를 공급자가 제공한다고 가정하지 않고 지금부터 저장한 스냅샷의 coverage를 공개합니다.

향후 통합 테스트: schema drift, nullable 필드, 429/451, 단위 오류, 중복 페이지, 잘못된 시장 매칭,
누락 holder 자료, WM degraded, 오래된 사건, 뉴스 prompt injection, 스냅샷 재생의 미래 자료 혼입을 검증합니다.
현재 실행 결과는 [검증 보고서](validation-report.md)에 한정합니다.
