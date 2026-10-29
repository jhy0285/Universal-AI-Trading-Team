# 운영 절차

현재 자동 페이퍼 운영과 API 키 없는 CPU 모델은 [자동 페이퍼 운영](autonomous-paper.md)을 따릅니다.
`GET /automation`에서 worker lease·설정 일치와 실제 작업 결과를 확인합니다.

## 설치·실행

저장소 루트에서 `bash scripts/bootstrap.sh`를 실행합니다. Python 3.12와 uv 0.12.23으로 검증했습니다.
Node 24.19.0/npm 11.9.0에서 `bash scripts/setup-worldmonitor.sh`를 검증했습니다.
의존성은 `uv.lock`과 고정 World Monitor commit/package lock을 사용합니다.

```bash
.venv/bin/python scripts/services.py start --worldmonitor
.venv/bin/python scripts/services.py status
```

서비스는 127.0.0.1의 8000(API), 8501(대시보드), 3000(WM)에 바인딩합니다.
cloud 포트 포워딩이 필요한 경우 이 세 포트만 연결하세요. 서비스 로그는 `.state/services/`에 있습니다.
재시작은 `scripts/services.py stop` 후 `start --worldmonitor`입니다.
stop은 저장된 PID와 시작 시각이 일치하는 이 저장소 프로세스만 대상으로 합니다.
worker는 DB lease를 갱신합니다. 두 worker가 같은 장부를 중복 처리하지 않으며, lease를 잃으면 작업을 취소합니다.

## 전역 모델

`bash scripts/install-clis.sh`로 Claude/Gemini/Qwen/OpenCode/Copilot의 고정 버전을 선택 설치합니다.
`.venv/bin/trading-team providers` 또는 `GET /providers`는 설치 목록만 조회하며 추론을 호출하지 않습니다.
Codex를 포함한 8개 native CLI와 다른 도구의 JSON 브리지 설정은 [CLI 호환 가이드](cli-providers.md)를 따릅니다.
모든 AI 역할이 하나의 전역 CLI·모델을 사용하며, native 로그인/구독의 권한·한도는 해당 서비스에서 확인해야 합니다.

Codex CLI는 API 키 없이 기존 로그인을 사용합니다. 사용자 PC의 `codex login status`뿐 아니라 실제 `codex exec`도 성공해야 합니다.
앱 구독이 일반 API 과금 계정·키를 제공한다는 뜻은 아닙니다. 로그인 파일·토큰을 Git이나 채팅으로 복사하지 마세요.

1. `.env`의 전역 `LLM_PROVIDER`, `LLM_MODEL`, 필요 시 `LLM_API_KEY`를 설정합니다.
2. `trading-team init`으로 활성 설정을 반영하고 API/worker를 함께 재시작합니다.
3. `trading-team probe`로 CHIEF/SCAN/VET/BOOK의 JSON 출력을 검사합니다.
4. 대시보드에서 모델/준비 상태를 확인합니다. 연결 검사는 24시간 후 만료합니다.

이 클라우드에서 Codex state/auth 경로는 읽기 전용이었습니다. 로컬 runner는 설치 ID 생성에서 실패할 수 있습니다.
선택적 Docker runner는 같은 CODEX_HOME 선택자에 임시 상태 공간을 두고 기존 auth 파일만 읽기 전용 연결합니다.
키·로그인 내용을 읽어 저장하지 않고, session CA를 읽기 전용으로 연결합니다. 이 환경에서는 별도 추론의 401도 관측됐습니다.
최종 앱 probe의 오류 코드는 검증 보고서에 기록합니다. 인증이 검증되지 않은 상태에서 자동 모의 진입을 켜지 않습니다.

## 자료 준비와 World Monitor

```bash
.venv/bin/trading-team collect
```

공개 DexScreener 자료를 보존합니다. 값이 있다는 사실과 거래에 충분한 자료가 있다는 사실을 구별합니다.
스캐너 화면의 null·stale 사유와 `collect` 결과의 `errors`를 확인하세요. 출처별 실패 코드가 기록됩니다.
Jupiter 429는 무료 키, GeckoTerminal 429는 개인 PC 실행, 보유자 조회 실패는 다른 RPC로 해결합니다. [무료 시장 관측](market-observer.md)을 참고하세요.

WM 필수 검사:

```text
GET /api/market/v1/get-fear-greed-index
GET /api/prediction/v1/list-prediction-markets?page_size=1
GET /api/intelligence/v1/list-cross-source-signals
```

로컬 Redis/REST proxy와 공식 seed 파이프라인을 구성했습니다. 현재 첫 두 경로에는 실제 데이터가 있고 세 번째는 교차 신호 부재로 503입니다.
worker가 실제 자료를 계속 갱신합니다. 비어 있거나 시각이 오래된 seed는 계속 degraded로 취급합니다.
이는 공개 웹사이트를 proxy하는 것과 다릅니다. 이 저장소는 hosted API나 가짜 seed로 READY를 만들지 않습니다.
공개 upstream 의존성에는 AGPL 라이선스가 적용되며 별도 checkout에 LICENSE를 보존합니다.

BOOK은 WM 발견 목록의 정확한 ID/제목에 해당하는 질문·원천 근거·resolution rules·마감 시각을 API로 전달받습니다.
확률이 기록된 후에만 지정 venue의 공개 가격을 조회하고, 제목/규칙/마감을 다시 대조합니다.
WM의 발견 목록만으로 근거와 resolution rules가 완성된 것으로 간주하지 않습니다.
Kalshi book에 원천 timestamp가 없으면 체결용으로 사용하지 않습니다. Gamma 451/온체인 429는 우회하지 않습니다.

## Docker

일반 환경에서는 README의 Compose 명령을 사용하세요. 관리형 cloud의 Docker context가 외부 daemon을 가리키거나 buildx 경로가 읽기 전용이면:

```bash
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
  BUILDX_CONFIG="$PWD/.cache/buildx" \
  docker --host=unix:///var/run/docker.sock compose build
```

프록시와 기본 Docker proxy 주입 설정은 유지합니다. 빌드 CA는 BuildKit secret으로 전달하며 이미지에 복사하지 않습니다.
runtime은 `SSL_CERT_FILE`의 공개 CA bundle을 read-only secret으로 연결합니다. TLS 검증을 끄지 않습니다.
Compose 앱은 일반 HTTP/Ollama Provider에 적합합니다. CLI를 사용할 때는 호스트의 설치·정상 로그인을 사용하세요.
선택 설치한 npm CLI bundle은 기본 앱 이미지에 포함하지 않습니다.
container에 Docker socket이나 로그인 파일을 기본으로 노출하지 않습니다.

## 장부 점검·재생

`GET /audit?account_id=...`는 현금/수량의 ledger 일치와 감사 체인을 검사합니다.
운영 DB는 PostgreSQL을 권장합니다. SQLite는 개발·독립 데모 용도입니다.
DB와 `.state`는 실행 기록이므로 업데이트하면서 삭제하지 않습니다. PostgreSQL은 pg_dump를 이용해 별도로 백업하세요.

```bash
.venv/bin/trading-team fixture --output /tmp/replay.json
.venv/bin/trading-team replay /tmp/replay.json --account run-01
```

동일 계정에 재생을 덮어쓰지 않습니다. 실제 전역 모델 비교는 새 계정에서 `--use-model`로 순차 실행합니다.
모델을 변경한 뒤에는 API/worker 재시작과 probe를 반복하세요. 기존 실행은 provider/model/config revision을 보존합니다.
미평가·확정 결과가 없는 예측의 Brier score와 장기 수익성은 표시하지 않습니다.

## 테스트

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

PostgreSQL 전용 테스트 DB URL을 환경변수 `UAT_TEST_DATABASE_URL`에 설정한 뒤 `pytest -q --postgres`를 실행합니다.
각 테스트는 임의의 별도 schema를 만들고 자신의 schema만 정리합니다. 운영 장부를 지우지 않습니다.
GitHub Actions에도 SQLite/PostgreSQL 검사를 구성했습니다. 원격 CI 자체를 실행했다고 주장하지 않습니다.
