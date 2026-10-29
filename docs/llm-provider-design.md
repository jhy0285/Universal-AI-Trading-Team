# 단일 LLM Provider 설계

상태: Phase 1 설계 기록 + 승인 후 구현. 아래 SDK/인터페이스 후보는 조사 당시의 제안입니다.
현재 구현은 네이티브 HTTP Adapter 6종, CLI Adapter 8종과 공통 JSON CLI 브리지입니다.
추가로 API 키 없는 loopback-only llama.cpp CPU Adapter를 구현하고 사용자가 선택한 로컬 모델의 네 역할 실제 schema probe를 통과했습니다.
모델 ID·가중치 URL·해시와 자동 실행은 [자동 페이퍼 운영](autonomous-paper.md)을 따릅니다.
CLI별 설정·설치·인증·검사 범위는 [CLI 호환 가이드](cli-providers.md)를 따릅니다.
전역 설정·출력 스키마·같은 모델 재시도·사용량 기록·프로세스 설정 버전 검사를 구현하고 mock으로 검증했습니다.
유료 외부 모델은 검증되지 않았습니다. 현재 실행 계약은 [구현 해석](implementation-decisions.md)과 [README](../README.md)를 우선합니다.

## 1. 설정의 유일한 원천

| 환경변수 | 의미 | 검증 규칙 |
| --- | --- | --- |
| LLM_PROVIDER | CLI 8종 / custom_cli / llama_cpp / openai / anthropic / google / xai / ollama / openai_compatible 중 하나 | 현재 작업은 환경변수로 llama_cpp 선택. 모델 ID 미설정 시 호출 차단 |
| LLM_MODEL | 사용자가 입력하는 실제 모델 ID | 공백·누락 거절; API로 사용 가능 여부를 별도 검증 |
| LLM_API_KEY | 선택한 Provider에 사용할 단 하나의 키 | 비밀값; 로그·DB·프롬프트·대시보드에 저장/반환하지 않음 |
| LLM_BASE_URL | 필요한 경우 명시하는 호환 API/로컬 Ollama 주소 | 승인된 host·scheme만; 임의 URL·리디렉션에 키를 전달하지 않음 |
| LLM_CLI_EXECUTABLE / LLM_CLI_COMMAND | 실행 파일 지정 / custom_cli용 JSON argv 배열 | shell/eval 없이 실행. native 도구 제한 옵션의 임의 교체 금지 |
| LLM_CLI_INPUT / LLM_CLI_OUTPUT | 브리지의 JSON/text 입력 및 응답 프로토콜 | 전체 응답·종료 이벤트·역할 schema를 검증 |
| LLM_CLI_API_KEY_ENV / LLM_CLI_BACKEND | 전역 키를 전달할 native 변수 / backend 선택 | 역할별 설정 금지. 키를 argv에 전달하지 않음 |
| TRADING_MODE | paper | paper 외 값은 시작 실패 |

환경변수에서 읽은 키를 네이티브 HTTP Adapter에 명시적으로 전달합니다.
별도의 Provider 기본 키 변수나 SDK의 암묵적인 계정 선택을 사용하지 않습니다.
CLI는 기존 로그인을 허용하고, 지원하는 native 키 변수에는 전역 LLM_API_KEY만 명시적으로 전달합니다.
기존 CLI 인증 selector·proxy·CA를 보존하며 로그인 파일의 내용을 읽어 옮기지 않습니다.
Codex CLI는 기존 로그인으로 인증하며 LLM_API_KEY를 쓰지 않습니다. Ollama도 키 생략을 허용합니다.
원격 Ollama가 인증을 요구하면 동일한 전역 키를 사용하며, 인증 실패 시 로컬 모델로 전환하지 않습니다.

CHIEF·SCAN·VET·BOOK은 **동일 Provider·모델·키·공용 클라이언트**를 사용합니다.
에이전트별 override, 모델 라우터, 대체 모델 목록, 자동 Provider 전환은 설정 스키마에 두지 않습니다.
SIZE·FILLS·RISK는 Provider를 받지 않는 Python 서비스입니다.

## 2. 공통 인터페이스와 스키마

다음은 구현 계약을 설명하는 예시이며 이번 단계에서 실행되는 코드는 아닙니다.

```python
class LLMProvider(Protocol):
    async def generate(
        self,
        messages: list[Message],
        response_schema: type[T],
        tools: list[ReadOnlyToolSpec] | None = None,
        *,
        request: GenerationRequest,
    ) -> LLMResult[T]: ...
```

`T`는 Pydantic BaseModel 파생 타입입니다. `Message`, `GenerationRequest`, `ReadOnlyToolSpec`, `LLMResult`도 Pydantic으로 정의합니다.
입력·출력에 알 수 없는 필드를 거절하고 NaN/Infinity, 확률 범위, 유한한 가격, 알려진 snapshot ID와 근거 ID를 검사합니다.
`LLMResult`는 검증된 payload와 별도로 model ID, Provider request ID, usage, latency, finish/refusal 상태를 보존합니다.

- SCAN: 실제 자산 ID와 evidence reference를 가진 후보 제안. 임의 주문 수량은 허용하지 않음.
- VET: 위험 근거·결측·거절 의견. 컨트랙트 검사 결과를 LLM이 관측 사실로 생성할 수 없음.
- BOOK: 방향·확률·불확실성·자료 기준시각·근거. `0 <= p <= 1`이며 시장가격을 독립 추정으로 복제하지 않음.
- CHIEF: 검증된 결과의 보고·제안. 엔진 승인이나 Kill Switch 해제를 할 수 없음.

Pydantic 통과는 사실 검증이나 수익성의 증명이 아닙니다. 외부 자산 ID·근거·시점·하드 필터를 Python이 다시 검증합니다.

## 3. Adapter 후보

| Provider | 검토한 공식 근거 또는 예정 경로 | 구현 시 주의점 |
| --- | --- | --- |
| OpenAI | 공식 Python SDK `helpers.md`의 Pydantic parse | Responses/Chat 응답·거절·사용량의 차이를 내부에서 정규화 |
| Anthropic | 공식 Python SDK README | Messages 형태·시스템 프롬프트·content blocks를 변환; 실제 모델 기능은 연결 테스트 |
| Google | 공식 `python-genai` README의 response_schema | JSON schema 하위집합·usage 형식·비동기 호출을 정규화 |
| xAI | 공식 Python SDK README | 공식 SDK는 gRPC 기반; 클라우드 프록시 호환성 확인 필요. HTTPS 호환 경로는 공식 계약 확인 후 선택 |
| Ollama | 공식 `docs/api.md`의 format JSON/schema 및 chat | 로컬 실행 자원·모델 태그/해시·설치된 모델 확인; JSON 출력도 재검증 |
| OpenAI-compatible | 명시된 base URL용 별도 Adapter | 이름만으로 OpenAI 전체 기능을 가정하지 않고 실제 endpoint·인증·스키마 확인 |

여러 Provider를 지원하는 것은 배포마다 한 개를 선택할 수 있다는 뜻입니다. 한 사이클에서 여러 모델을 섞지 않습니다.
실제 모델명은 문서의 SDK 예시를 복사해 기본값으로 채택하지 않고 사용자가 환경변수로 지정합니다.
SDK 버전·잠금 파일·취약점 검토는 Phase 2 구현 전에 확정합니다.

## 4. LiteLLM 검토 결과

2026-10-21 조회한 [PyPI metadata](https://pypi.org/pypi/litellm/json)의 최신 버전은 `1.106.1`입니다.
`requires_dist` 108개는 extras 포함 목록이며, extra marker가 없는 항목은 22개였습니다.
이 숫자는 전이 의존성 수나 실제 설치 수가 아닙니다. metadata의 `vulnerabilities=[]`만으로 안전성을 판정하지 않습니다.

공식 보안 권고 2건에서 영향 버전과 패치 버전도 읽었습니다.

| 공식 권고 | 확인한 문제와 패치 정보 |
| --- | --- |
| [GHSA-7hp6-4w63-5g45](https://github.com/BerriAI/litellm/security/advisories/GHSA-7hp6-4w63-5g45) | Proxy 관리자 권한 상승. 영향 범위: >=1.91.0,<1.100.4 / >=1.101.0,<1.101.3 / >=1.102.0,<1.102.2 / >=1.103.0,<1.103.1 / >=1.104.0rc1,<1.104.0rc2. 문서상 패치: 1.100.4, 1.101.3, 1.102.2, 1.103.1, 1.104.0rc2 |
| [GHSA-3cv6-jpf6-8222](https://github.com/BerriAI/litellm/security/advisories/GHSA-3cv6-jpf6-8222) | Proxy 라우팅 값에 의한 인증정보 유출/SSRF. 권고 표의 영향 범위 <1.94.0; 패치 목록 1.96.2, 1.95.1, 1.94.3, 1.93.2, 1.92.2, 1.91.5, 1.90.7, 1.89.7, 1.88.6 |

이 권고들은 Proxy와 특정 구성에 관련되며 Python 라이브러리 사용 전체에 동일하게 적용된다고 주장하지 않습니다.
권고 목록에는 다른 항목도 있고 전이 의존성·배포 artifact 전체 감사는 수행하지 않았습니다.

**Phase 1 선택:** LiteLLM은 채택 보류하고 선택한 Provider의 공식 SDK를 감싼 작은 Adapter부터 구현할 계획입니다.
단일 모델 운용에는 다중 Provider Proxy가 필요하지 않습니다. 공식 SDK 역시 안전성을 가정하지 않고 버전 고정·의존성 감사·TLS 검증을 수행합니다.
이번 단계에서는 LiteLLM이나 모델 SDK를 설치하지 않았습니다.

## 5. Capability Registry

레지스트리 키는 `(provider, model_id, API 버전, adapter 버전)`입니다.
기능별 상태를 `verified / unsupported / unknown`, 검증 시각, probe 결과, 유효기간과 함께 저장합니다.

| 기능 | 처리 계약 |
| --- | --- |
| Structured Output / JSON Schema | 실제 응답을 Pydantic으로 검증; Provider·모델별 schema 지원 범위를 probe |
| Tool / Function Calling | 선택 기능. 미지원이면 앱이 고정된 읽기 Tool Adapter로 자료를 먼저 수집해 같은 스키마 입력으로 전달 |
| Context / 출력 한도 | 실제 토큰 예산 확인; 넘으면 명시적으로 분할하거나 거래 차단, 근거를 몰래 잘라내지 않음 |
| Streaming | 초기 거래 판단에 필수 아님; 완료·검증 전 부분 출력으로 거래하지 않음 |
| Reasoning | 지원·usage 형식 확인; 숨겨진 reasoning이나 설명을 진실의 증거로 취급하지 않음 |
| Timeout / retry / usage | 오류·비용·사용량을 정규화; 여러 SDK의 중첩 재시도를 제한 |

필수 기능이 `unknown`이면 활성화를 막습니다. 공급자 단위의 정적 Yes/No 표로 모든 모델을 승인하지 않습니다.
식별자 필드(SCAN 후보·근거, VET·BOOK 근거)는 호출마다 허용 ID를 enum으로 좁힌 schema를 보냅니다. llama.cpp 같은 문법 제약 디코더는 다른 값을 출력할 수 없습니다.
텍스트 JSON 대체 경로는 실제 모델·해당 스키마 검증에 성공한 경우에만 등록합니다. JSON 수선이나 반복 프롬프트로 거래 승인을 얻지 않습니다.

## 6. 연결 검증·설정 변경·실패

활성화 전에는 실제 Provider 연결/모델 접근, 역할별 최소 출력 스키마, 필수 기능, timeout·usage 수집을 검증합니다.
이는 비용이 발생할 수 있는 검증이며 아직 실행하지 않았습니다. Provider·모델·키가 지정되지 않은 상태를 연결 성공으로 표시하지 않습니다.

환경변수는 worker 시작 시 읽습니다. 배포 설정을 변경해도 실행 중인 프로세스의 환경변수가 자동 갱신된다고 가정하지 않습니다.
정상 재시작 시 진행 중 사이클을 기존 설정으로 마무리하거나 명시적으로 중단하고, 새 설정 probe 성공 후 다음 사이클을 시작합니다.
강제 재시작으로 옛 설정을 복원할 수 없으면 미완료 사이클을 중단하고 새 사이클을 만듭니다.

전역 설정 revision/hash에는 Provider·모델·파라미터·스키마 버전을 포함하며 비밀 키는 포함하지 않습니다.
키 회전은 비밀값 대신 비밀 관리 시스템의 revision 메타데이터로 추적합니다.
대시보드는 전역 설정과 검증 결과만 보여주고, 키 변경은 안전한 환경 설정 경로에서 수행합니다.

일시 오류에 대한 제한된 재시도는 **같은 Provider·모델**로만 수행합니다. 자동 Fallback은 없습니다.
실패·할당량 초과·거절·무효 응답은 신규 거래 차단과 감사 이벤트로 끝내며, RISK의 Python 감시는 계속됩니다.

## 7. 단일 LLM에서의 비교 실험

모델 비교는 전역 모델을 실행 사이에 변경한 별도의 순차 실행으로 수행합니다.
스냅샷·기간·초기 장부·전략·비용·한도를 고정하고 모델 출력과 각 실행의 파라미터·표본 수·실패율을 기록합니다.
동일 스냅샷이라는 이유로 모델 출력의 무작위성이나 결과 표본 수 차이를 숨기지 않습니다.
의사결정 일치율·확률 보정·API 비용과 순손익을 함께 보고합니다. [비용 계획](cost-estimation.md)을 참조하세요.

## 8. 확인한 공식 자료

- [OpenAI Python helpers](https://github.com/openai/openai-python/blob/main/helpers.md)
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)
- [Google Python GenAI](https://github.com/googleapis/python-genai)
- [xAI Python SDK](https://github.com/xai-org/xai-sdk-python)
- [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md)

각 자료의 실제 HTTP 결과와 내용 해시는 [검증 보고서](validation-report.md)에 기록합니다. 기능 설명은 모델별 실제 연결 테스트의 대체물이 아닙니다.

## 9. 로컬 문법 디코딩의 한계

`llama_cpp`는 JSON 스키마를 디코딩 문법으로 바꾸며 **스키마 텍스트를 모델에게 보여 주지 않습니다.** 따라서

- `Field(description=...)`는 llama.cpp 모델에는 효과가 없습니다. OpenAI·Anthropic·Google 등 스키마를 모델에 전달하는 Provider에서만 도움이 됩니다.
- `gt=0` 같은 수치 하한은 문법이 강제하지 못합니다. 모델이 쓸 법한 값은 Pydantic이 사후에 검증합니다.
- 필드 규칙은 역할의 task 문장에 직접 씁니다(거절 시 `null`, 확률은 0~1 소수, 증거는 라벨 없는 `evidence_id` 그대로).
- 거절 verdict에서만 쓸모없는 `win_payoff_ratio <= 0`을 `null`로 바꿉니다. PASS는 Kelly가 비양수 배당을 거부하므로 실패로 둡니다.
- 증거 ID는 앱이 계산한 해시와 **정확히 일치**해야 합니다. 라벨 접두사나 지어낸 후보를 보정하지 않고 차단합니다.
