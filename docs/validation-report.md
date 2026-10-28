# 검증 보고서

## VET 스키마 대응·학습 루프 검증 — 2026-10-28, UTC

| 검사 | 실제 관측과 범위 |
| --- | --- |
| SQLite | pytest **222건 통과**(기존 204건 + 신규 18건). Ruff check/format 통과 |
| PostgreSQL | 로컬 PostgreSQL 16 임시 클러스터에서 `--postgres` 전체 **222건 통과** 후 클러스터 삭제 |
| 스키마 실패 진단 | 0~100 척도 확률(`win_probability: 65`)과 예상치 못한 키를 가진 응답이 `win_probability:less_than_equal`처럼 필드·오류 유형만 남기는지 검사. 응답 값은 detail에 들어가지 않고, 모델이 만든 키 이름은 영숫자·밑줄 외 문자를 `?`로 바꿔 40자까지만 포함 |
| 교정 재시도 | 스키마 실패 뒤 필드 피드백을 붙여 정확히 1회 재시도하고, 계속 실패하면 멈춤. 첫 실패는 `final=false`로 사용량에 남지만 감독기는 호출당 결과 1건만 봄 |
| 학습 루프 | 정답을 손으로 계산한 합성 자료로 Brier(0.22/0.25/0.41), 가격 변화 평균, 표본 부족 시 제안 없음, 풀이 다른 스냅샷 제외, 미성숙 horizon 제외, 제안 생명주기와 감사 체인 검사 |
| API·worker | 관리 토큰 없이는 실행·검토 401, 알 수 없는 제안 404. 자동 모드에서만 매시간 작업이 등록되고 실행 기록에 `automatic_policy_changes: false` 저장 |
| 대시보드 | 실제 API를 띄운 AppTest로 13개 화면 × 2개 계정 = **26개 화면**, 예외 0건. 제안 표와 검토 버튼 렌더링 확인 |

**검증하지 못한 것:** 이 환경의 프록시가 `huggingface.co`와 DexScreener를 정책상 차단(HTTP 403)해 실제 로컬 모델을 받거나 실시장 자료를 읽지 못했습니다.
따라서 VET의 `LLM_SCHEMA_INVALID`가 이번 변경으로 **해결됐는지는 모릅니다.** 원인은 0~1 범위를 넘는 확률 같은 단위 오류로 추정하지만 재현하지 못했습니다.
실제 모델을 설치한 환경에서 `trading-team replay <file> --use-model`을 실행하면 실패 시 `detail`에 필드가 나옵니다.
학습 루프의 가격·보정 지표는 합성 자료로만 검증했고, 운영 계정에는 진입이 없어 보정 표본이 0입니다.

## 자동 페이퍼·로컬 CPU 모델 검증 — 2026-10-21, Asia/Seoul

사용자는 페이퍼머니로 자동 운영을 요청하고 API 키 없는 로컬 LLM 사용을 선택했습니다.
전역 모델은 환경변수의 `llama_cpp`/모델 ID로 전환했습니다. [자동 페이퍼 운영](autonomous-paper.md)에 현재 실행 계약을 기록합니다.

| 검사 | 실제 관측과 범위 |
| --- | --- |
| 로컬 모델 설치 | 공식 Qwen3-4B Q4_K_M GGUF 2,497,280,256 bytes. 고정 HF revision과 SHA-256 확인. llama.cpp b11790 공식 CPU 배포본 SHA-256 확인 |
| 실제 모델 추론 | SCAN·VET·BOOK·CHIEF 네 역할 모두 실제 로컬 schema probe 통과. 한 번의 4역할 검사 약 17.76초. 각 응답의 모델 ID가 전역 설정과 일치 |
| 실제 모델을 연결한 합성 재생 | SCAN은 통과했으나 VET 응답이 `LLM_SCHEMA_INVALID`로 차단됨. 주문·체결 0, 합성 계정 현금 1000 USD·장부 무결성 유지. 일반 VET 분석의 출력 호환 문제는 미해결이며 간단한 probe 통과와 구분해야 함 |
| 자금·경계 | PAPER ONLY, API 키 없음, CPU·loopback·단일 모델. 도구·MCP·agent 비활성. 실제 주문·지갑·서명·출금 경로 없음 |
| SQLite | pytest **204건 통과**. 자동 점검·수집 분리, 설정 변경 감지, 의도한 취소 처리, 데이터 feed의 ID/provenance, BOOK 자동 입력, 이진 정산·중복 방지·원장 검사 포함 |
| PostgreSQL | 별도 PostgreSQL 16.13의 임의 schema에서 전체 pytest **204건 통과** |
| 실제 자동 실행 | worker lease·현재 설정 일치 확인. 모델 점검·60초 시장 수집·SCAN·5분 RISK·BOOK·WM/seed 작업 기록 확인. 실제 Dex 관측 30개씩 수집 |
| 실제 운영 계정 | main 가상 현금 1000 USD, 열린 포지션 0, 검증 자료가 완전한 시장 관측 0. 자동 수집·관측 상태이며 신규 체결 없음 |
| World Monitor | 고정 Redis 7.4.11 바이너리·REST proxy와 공식 seed 구성. 지진 109개·규제 RSS 90개 및 실제 fear/greed·prediction 자료를 수집한 실행 관측. 수량은 당시 값 |
| WM 교차 신호 | 실제 수집 범위에서 신호 0개. upstream의 0 records/last-good 부재로 canonical publish 실패·REST 503. BOOK degraded 유지. 빈 자료로 READY를 만들지 않음 |
| 원본 시장자료 | Dex의 필수 시각·보유자 이력·whale·실행 깊이는 미확보. Solana largest accounts 실제 HTTP/RPC 429 재확인. 다른 chain은 Original SCAN에서 제외 |
| 실제 정산 조회 | Kalshi settled 검색의 payload는 finalized임을 공식 OpenAPI와 실제 응답으로 확인. finalized·binary result·settlement value·시각이 일치하는 공개 시장을 실제 normalizer로 읽음. 실주문 제출 없음 |
| 대시보드 | main/합성 계정의 12개 화면, 총 24개 화면에 예외 없음. 자동 모의운영 상태와 자료 대기 표시 포함 |
| 운영·재설치 | API·Streamlit HTTP 200. 장부 온라인 백업. 기존 가중치 SHA 재검증과 Redis/REST runtime 재설치 후 서비스 재기동. worker를 의존 서비스보다 먼저 중지 |
| 정적·패키징 | Ruff check/format 통과. 변경된 앱 Docker 이미지 재빌드. 모델 가중치·로컬 인증·선택 CLI bundle은 이미지에 포함하지 않음 |

실모델 schema probe는 수익성·판단 정확도·장기 무중단 운영의 증거가 아닙니다.
일반 VET 분석의 스키마 오류 원인 진단·수정·재검증은 아직 완료하지 않았습니다.
원본 전략의 시장 관측 공급이 미완성이므로 실제 자동 모의 진입→청산의 시장 데이터 통합 성공을 주장하지 않습니다.
합성 계정의 체결·정산 검사는 실제 자금이나 실제 주문이 아니며, 필수 자료의 대체물로 사용하지 않았습니다.
기존 Starlette/httpx 경고 1건은 남아 있습니다.

install_script·start_skill과 비밀값 없는 로컬 모델/자동 실행 환경변수 제안을 초안에 저장하고 readback으로 확인했습니다.
기존 unrestricted 네트워크와 저장소 목록은 보존했습니다. status:saved/requires_publish:true이며 새 환경 적용에는 설정 검토·저장 후 Publish가 필요합니다.
새 작업에서의 snapshot 복원은 미검증입니다.

## CLI 확장 당시 검증 — 2026-10-21, Asia/Seoul

단일 전역 Provider·모델을 유지하면서 Codex·Claude·Gemini·Qwen·OpenCode·Copilot·Goose·Ollama CLI와 공통 JSON 브리지를 구현했습니다.
설정·사용 절차·CLI별 인증 경계는 [CLI 호환 가이드](cli-providers.md)에 기록합니다.

| 검사 | 실제 관측과 범위 |
| --- | --- |
| SQLite 전체 테스트 | pytest **167건 통과**. 기존 매매·원장·권한 검사와 CLI 프로토콜·subprocess·비용·인벤토리 검사 포함 |
| PostgreSQL 전체 테스트 | 별도 PostgreSQL 16.13 컨테이너와 테스트별 임의 schema에서 **167건 통과** |
| CLI 계약 | 7개 추가 native 응답 형식, 공통 브리지 4개 AI 역할 probe, 동일 전역 모델, SIZE/FILLS/RISK 호출 차단을 가짜 실행 파일로 검사 |
| 실패 처리 | 모델 불일치·도구 이벤트·user echo·종료 이벤트 누락·중복 key·비정상 usage·출력 초과·timeout·취소·잘못 설치된 실행 파일·symlink/FIFO 거절 검사 |
| 상태·비용 | 실패한 probe에 이전 성공 관측이 남지 않음. 구독/과금 미확인 CLI 토큰은 단가를 설정해도 비용 N/A 유지 |
| CLI 설치 | Claude 2.1.306, Gemini 0.65.0, Qwen 0.25.3, OpenCode 1.19.3, Copilot 1.0.101을 고정 npm lock으로 저장소 내부 설치 |
| 실제 실행 옵션 | 위 5종의 help/version 종료 코드 0. 사용 옵션을 설치된 help·공식 reference와 대조. Claude의 max-turns는 공식 reference에 있고 help에는 표시되지 않음 |
| 대시보드 | 12개 화면 × main/합성 계정 = **24개 화면**, 예외 0건. Global Model Configuration의 CLI 설치 목록도 확인 |
| 서비스·재생 | API·대시보드 HTTP 200. 별도 합성 계정 HTTP 재생 최종 현금 1004.10달러, 현금·수량·감사 체인 모두 일치 |
| Docker | 변경된 소스로 이미지 재빌드 성공. 비루트·read-only·network none 실행의 providers 명령이 9개 프로필을 반환. 선택 CLI 바이너리·로그인 파일은 이미지에 없음 |
| 정적 검사 | Ruff check/format 통과. Markdown 12개와 내부 링크 39개 검사 |

Claude의 bare 옵션은 이 버전에서 OAuth/keychain 인증을 읽지 않습니다. 기존 로그인 사용 요구에 맞춰 safe mode와 빈 tools·strict empty MCP·스킬 차단을 적용했습니다.
Gemini의 확장 차단에는 공식 none 선택자를 사용합니다. Copilot의 JSON 모드에서는 help에 없는 no-banner 옵션을 제거했습니다.
이 검사는 명령 구성과 오류 처리를 검증하며 **실제 계정의 인증·추론 성공을 증명하지 않습니다**.
Goose/Ollama 바이너리는 설치하지 않았으며 계약 테스트 범위입니다. 기타 CLI는 inference 전용 브리지를 직접 구성해야 합니다.

이 클라우드의 기존 Codex 추론 401, World Monitor Redis/seed 미구성, 신선한 검증 시장 자료 부족은 해결되지 않았습니다.
새 설정 revision으로 재시작한 실제 서비스의 readiness는 MODEL_PROBE_REQUIRED·NO_VERIFIED_FRESH_MARKET_SNAPSHOTS로 degraded이며 BOOK·밈코인 진입이 차단됩니다.
RISK는 LLM과 독립 실행되고 PAPER ONLY를 유지합니다. 기존 Starlette/httpx 경고 1건은 남아 있습니다.

고정 CLI 설치 명령을 install_script에 추가하고 설치 조회·인증·브리지·기동 지침을 start_skill에 저장했습니다.
초안 저장은 status:saved/requires_publish:true로 확인했습니다. 새 환경에 적용하려면 환경 설정에서 검토·저장 후 Publish해야 합니다.
기존 unrestricted 네트워크·저장소·비밀값은 유지했습니다. 새 작업에서의 snapshot 복원과 실제 모델 추론은 미검증입니다.

## 승인 후 초기 애플리케이션 검증 — 2026-10-21 (CLI 확장 전 기록)

Phase 1 검토 후 사용자의 진행 승인에 따라 실제 앱을 구현했습니다. 아래의 Phase 1 기록은 당시 결과로 보존합니다.
현재 코드·설치·운영 계약은 [README](../README.md), [구현 해석](implementation-decisions.md), [운영 절차](runbook.md)를 따릅니다.

| 당시 검사 | 실제 관측 |
| --- | --- |
| SQLite 테스트 | pytest 85건 통과. 원문 경계, Provider mock, 장부·부분 체결·멱등성·동시성·rollback·인증·재생·설정 변경 차단 검사 |
| PostgreSQL 테스트 | 별도 PostgreSQL 16 컨테이너의 테스트별 임의 schema에서 pytest 85건 통과 |
| 정적 검사 | Ruff check/format 통과. Markdown 11개와 저장소 내부 링크를 scripts/check_docs.py로 검사 |
| CLI 독립 데모 | 합성 계정에서 60달러 매수→5분 후 RISK 매도. 수수료 합계 1.90달러, 최종 현금 1004.10달러, 현금/수량/감사 체인 일치 |
| 대시보드 | Streamlit AppTest로 12개 화면 × 실제 공개자료 계정/합성 데모 계정 = 24개 화면 검사, 예외 0건 |
| 실제 개발 서비스 | API /health 200, Streamlit /_stcore/health 200, worker 기동·재기동 확인 |
| 실제 공개 데이터 | 앱 Adapter로 DexScreener BONK 30 pairs 수집·보존. 누락된 보유자/원천 timestamp/체결 깊이는 null 유지 |
| 로컬 World Monitor | 고정 commit 설치 후 실제 기동. fear 200/unavailable:true, prediction 200/dataAvailable:false/fetchedAt:0, signals 503. BOOK 차단 확인 |
| 실제 Codex CLI | 0.163.0, 기존 ChatGPT 로그인 표시 확인. 로컬 상태 경로 EROFS, 별도 추론에서 401 관측. 최종 앱 probe는 SCAN에서 CODEX_AUTHENTICATION_FAILED, verified:false |
| Docker 빌드 | 고정 Python image와 CA build secret으로 빌드 성공. session CA·로그인·.env를 이미지에 복사하지 않음 |
| Compose 통합 | 별도 검증 프로젝트에서 PostgreSQL·API·대시보드·worker 모두 기동. API/대시보드 200, HTTP 재생으로 1004.10달러·수수료 1.90달러·장부 일치 확인 |

pytest에는 설치된 Starlette/httpx 호환 경고 1건이 있습니다. 실패한 테스트는 아니며 실제 외부 모델 검증의 대체물로 보지 않습니다.
초기 Docker 기동에서 복사된 소스의 소유권 문제를 발견했습니다. 비루트 앱 UID로 소유권을 지정한 뒤 통합 검사를 통과했습니다.
설치 스크립트를 다시 실행해 기존 .env와 장부를 보존하는 재설치 및 서비스 재기동도 검사했습니다.

### 현재 운영 차단 조건

1. **Codex 실제 추론 미인증**: 모델 ID는 설치된 CLI가 표시한 값을 로컬 .env에 저장했으며 코드 상수로 고정하지 않았습니다.
   API 키는 입력하지 않았습니다. 로그인 표시와 별개로 추론이 실패해 네 AI 역할의 실제 출력 검증을 완료하지 못했습니다.
2. **World Monitor 실데이터 seed 미구성**: HTTP 서버 설치·실행과 READY를 구별합니다. Redis REST/seed가 없으므로 BOOK을 켜지 않습니다.
3. **밈코인 실행 자료 미완성**: 공개 Dex 응답에는 보유자 이력·whale 상태·두 독립 구간·원천 timestamp·실행 깊이가 없습니다.
   추가 신뢰 자료 공급이 구성되지 않았고, 온체인 집중도는 공개 RPC 429도 관측했습니다.

HTTP Adapter 6종은 mock 계약 테스트입니다. 유료 OpenAI/Anthropic/Google/xAI, 실제 Ollama 모델 및 실제 CLOB 체결 조건의 성공을 주장하지 않습니다.
합성 데모 수익은 미리 정한 가격 경로의 계산 예시입니다. 수익성, 장기 무중단 운영, 모델별 우월성·확률 보정은 미검증입니다.
초기 schema 이후의 migration 및 새 작업에서의 환경 snapshot 복원도 검증 범위에 포함하지 않습니다.

### 환경 설정 저장

설치 스크립트·기동 지침·비밀값 없는 전역 환경변수 제안을 설정 초안에 저장했고 도구가 status:saved를 반환했습니다.
기존 unrestricted 네트워크 정책과 저장소 목록은 변경하지 않았습니다. 새 API 키 요구도 추가하지 않았습니다.
도구는 requires_publish:true를 반환했습니다. 이는 초안 저장이며 자동 적용·게시가 아닙니다.
새 환경에 적용하려면 환경 설정에서 검토·저장 후 Publish해야 합니다. 현재 작업의 서비스 재기동 검증과 새 작업 복원 검증은 별개입니다.

---

## Phase 1 당시의 검증 기록

검증일: 2026-10-21, Asia/Seoul. 대상: 설계 문서, 공식 자료·공개 데이터 계약, 원문의 수식 검산.
프로젝트 애플리케이션·Provider Adapter·매매 엔진은 아직 없으므로 아래 결과를 제품 통합 테스트나 운영 준비 완료로 해석하지 않습니다.

## 1. 네트워크 재확인

처음 원문·DexScreener 문서·Polymarket 문서는 프록시 CONNECT 403으로 실패했습니다.
사용자가 도메인 전체 허용을 적용한 뒤 런타임 spec revision 2, 모드 unrestricted가 조회됐고 세 문서 모두 HTTP 200으로 다시 읽었습니다.
시작 시 생성된 `/etc/codex/network-policy.json`에는 이전 restricted 정책이 남아 있었습니다.
따라서 시작 스냅샷만으로 변경 미적용을 판단하지 않고 현재 상태와 실제 요청 결과를 함께 사용했습니다.
상태 도구의 network state는 unknown이므로 모든 목적지의 준비가 증명됐다고 일반화하지 않습니다.

에이전트가 저장했던 세 조사 도메인 허용 초안은 이후 사용자가 변경한 unrestricted 정책으로 대체됐습니다.
그 사용자 설정을 다시 좁히지 않았습니다. 설치·기동 스크립트는 이번 문서 작업에 필요하지 않아 저장하지 않았으며 게시 완료도 주장하지 않습니다.

## 2. 자료·공개 API의 실제 결과

| 확인 대상 | 최종 결과 | 증명 범위 / 남은 제한 |
| --- | --- | --- |
| 원문 가이드 전체 | HTTP 200, Quick Start·STEP 1~4·FAQ·공식 자료 읽음 | 전략 추출 근거 확보; 원문 자체의 미정의 항목 존재 |
| DexScreener 공식 reference | HTTP 200, Markdown/OpenAPI 조각과 Pair 필드 읽음 | holders 필드가 없음을 확인 |
| Polymarket 공식 문서 | HTTP 200, 공개 시장 조회 계약 읽음 | 데이터 API 성공과 별개 |
| World Monitor 공식 소스 | Git read 성공, pinned commit 확보 | 실제 로컬 서비스는 미기동 |
| DexScreener BONK 검색 | HTTP 200, pairs 30개 | 공개 검색·필드 확인; 거래 유효성 검증 아님 |
| DexScreener 원문 token route | 최초 generic client 403 → 앱 식별 UA 재요청 200, 30 pairs, h6/h24 존재 | 요청 설정 변경 후 해당 작업 성공. 모든 차단 원인을 UA로 단정하지 않음 |
| DexScreener 현행 chain token route | HTTP 200, 1 pair, h6/h24 존재 | legacy와 같은 검색 범위를 보장하지 않음 |
| Kalshi 열린 시장 | HTTP 200, markets 100개와 cursor | 공개 조회와 dollar string 가격 필드 확인 |
| Polymarket Gamma 열린 시장 | HTTP 451; server envoy; body error code: 1026 | 유효 스냅샷 없음. 지역·원격·중간 정책 중 원인 확정 불가; 우회하지 않음 |
| Solana getTokenSupply | HTTP 200, JSON-RPC result 존재 | 공급량만 확인; 최대 계정 집중도 증명 아님 |
| Solana getTokenLargestAccounts | HTTP 429 | 최대 보유 계정 검증 미완료. 원문 VET상 통과로 볼 수 없음 |
| 공식 모델 SDK 문서 5종 | OpenAI·Anthropic·Google·xAI·Ollama HTTP 200 | Adapter 설계 근거; 실제 모델 기능/연결은 미검증 |
| LiteLLM metadata·보안 권고 | PyPI/GitHub HTTP 200, 버전·두 권고의 영향/패치 범위 읽음 | 최신 전체 의존성 감사 미실행; LiteLLM 미채택 |

라이브 조회는 당시의 관측 한 번 또는 진단 재요청입니다. 가용성 SLA·과거 데이터 완전성·거래 가능성·수익성의 근거가 아닙니다.

## 3. 공식 World Monitor SDK 기존 테스트

대상 소스 revision: `c0f783620dcdae713a6d28c239844c12311e2bf6`.
작업 디렉터리: `/tmp/universal-trading-worldmonitor-source`.

```bash
python3 -m unittest discover -s sdk/python/tests -v
```

실제 결과: **20 passed, 0 failed**, 종료 코드 0. 실행기는 `Ran 20 tests ... OK`로 완료했습니다.
원래 SDK의 `FakeTransport` 테스트이며 네트워크·실제 비밀값·계정 인증을 사용하지 않습니다.
JSON/SSE body 처리, 설정 우선순위, wire header, RPC 오류/결과, REST query와 오류 전달을 검사했습니다.

이 SDK는 MCP-first 옵션 조사 대상입니다. 이 테스트가 Original의 로컬 WM REST·Redis·seed 또는 우리 Adapter의 검증을 대신하지 않습니다.

## 4. 원문 계산과 경계값

Python Decimal을 사용한 독립 검산 **10건 통과, 0건 실패**입니다.
표시용 반올림은 ROUND_HALF_UP이며 원문이 구현 반올림 정책을 지정했다고 주장하지 않습니다.

| 계산 | 실제 검산값 |
| --- | --- |
| 4717.70 / 4 | 1179.425 |
| 위 결과의 소수 2자리 표시 | 1179.43 |
| 167.75 / 1179.425의 소수 3자리 표시 | 0.142 |
| 위 원문 예시의 ratio < 0.20 | true / CLOSE |
| h24=4000, h6=200의 strict 비교 | false / 이 조건으로 CLOSE 아님 |
| h24=4000, h6=199.99의 strict 비교 | true / CLOSE |
| 20달러 effective_fee | 0.0475 / 4.75% |
| 50달러 effective_fee | 0.019 / 1.9% |
| 1000달러 effective_fee | 0.0045 / 0.45% |
| 1000달러 계좌의 6% | 60.00달러 |

이는 명세 수식의 예제·경계 검산입니다. SIZE/FILLS/RISK 구현 테스트 10개를 통과했다고 표현하지 않습니다.
Kelly 정확식, BOOK 8%의 방향 해석, 부분 체결 정책은 미정의라 검산 결과로 승인하지 않았습니다.

## 5. 문서 정적 검사

최종 검사 통과, 종료 코드 0, 오류 0건입니다.

- 필수 설계 문서 7개와 README·이 보고서를 포함한 Markdown 파일 9개 확인.
- 저장소 내부 링크 20개와 해당 anchor 확인.
- 코드 fence 8개가 모두 닫혔으며 줄 끝 공백·마지막 줄 개행에 오류 없음.
- 환경변수 예시 5개 확인: Provider·모델·키·base URL은 빈 입력값, TRADING_MODE만 paper.
- 에이전트별 Provider/모델/API 키 대입 설정 없음.

새 파일의 whitespace 검사는 git diff --no-index --check를 사용했습니다.
/dev/null과의 차이 자체를 뜻하는 종료 코드 1과 실제 whitespace 진단 출력을 구별했습니다.
환경이 다시 starting으로 표시된 뒤 준비 완료 시 파일 9개와 검사 자료가 같은 작업 환경에 유지된 것도 확인했습니다.
이는 새 작업에서의 게시 snapshot 복원 검증과는 별개입니다.

## 6. Phase 1 당시 미실행 항목 — 현재 상태는 상단 참조

| 항목 | 상태·이유 |
| --- | --- |
| 프로젝트 pytest / Ruff / build | 미실행. Python 애플리케이션과 테스트·package manifest를 Phase 2 이전에 만들지 않음 |
| 실제 LLM 연결·capability/schema probe | 미실행. LLM_PROVIDER/LLM_MODEL/LLM_API_KEY가 모두 unset임을 값 출력 없이 확인 |
| 로컬 WM REST의 3개 준비 검사 | 미실행. 로컬 stack 미기동; 원문의 예상 degraded 결과를 실제 관측으로 둔갑시키지 않음 |
| 온체인 집중도 통합검증 | 미완료. 최대 계정 RPC 429; holder-history 공급자 미선정 |
| 실제 paper fills·장부·Kill Switch | 미실행. 관련 엔진은 아직 설계 단계 |
| 모델별 수익률·비용·확률 보정 | 미실행. 모델·평가 표본·운영 기간이 없음 |
| 새 작업에서 snapshot 복원·서비스 재기동 | 미실행. 게시·복원 검증을 완료했다고 주장하지 않음 |

## 7. 핵심 자료 스냅샷 식별

| 자료 | revision 또는 SHA-256 |
| --- | --- |
| 원문 HTML | 40514ff20c1d51c38a7b51898b044a4e4fcd6dc6f4648155eb9513b2c88f53c6 |
| World Monitor | c0f783620dcdae713a6d28c239844c12311e2bf6 |
| DexScreener reference 응답 | 478a1ae00656d095067331d223508db9acf3f86c03f0962d65168e16ce1d542e |
| Polymarket 문서 응답 | 3a170102c8d08b503a6e40943edb32d29b030582870eb49183c06f4693dbd092 |
| LiteLLM PyPI metadata | 6673a8ccb3b97248347a013f8f01d02565d5756dce4bd405a55c9041b71883ab |
| OpenAI helpers | 8b91583fdd85ee6fb33eb0157c7fc4980211afb889c96f8cec1680f5e9754265 |
| Anthropic README | 05c7f018e77a7a0fd94679317f03a5720bbf257f1e43bfdd25162e42afb5a35e |
| Google GenAI README | 66952d47594d7a9c588782f57cb02d04fa4fd27b108112fd06b036234cdebc34 |
| xAI README | 31b35957625483405e0595d8f41f62ae67f18a79d390dc0690dc2bd99c4de20e |
| Ollama API | 96db03080e385dbabf5d70e75f298d41b75983bc30975cae56f7119f848dd0f5 |
| 새 Polymarket SDK README | ad3fde3677d0b2abd368fed7148efbfa560c57d74c3b1f98d22e94e1658d7b51 |

외부 문서·raw main 링크는 이후 바뀔 수 있습니다. 위 해시는 이번에 읽은 내용의 식별자이며 공급자 서명이나 독립 artifact 검증 증명이 아닙니다.
원문 전체 재배포 대신 위치·필요한 인용·추출 규칙·해시를 저장했습니다. 민감한 환경변수 값은 기록하지 않았습니다.

이후 사용자 승인으로 구현을 진행했습니다. [로드맵](development-roadmap.md)의 현재 범위와 상단의 실제 검증 결과를 참고하세요.
