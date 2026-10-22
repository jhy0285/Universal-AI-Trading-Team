# CLI Provider 호환 가이드

전역 `LLM_PROVIDER`와 `LLM_MODEL`로 CLI 하나를 선택합니다. CHIEF·SCAN·VET·BOOK이 같은 설정을 공유합니다.
SIZE·FILLS·RISK는 CLI를 호출하지 않습니다. 실제 주문·서명 경로도 추가하지 않았습니다.

## 기본 제공 어댑터

| LLM_PROVIDER | CLI | 입력·출력 방식 | 인증 |
| --- | --- | --- | --- |
| codex_cli | Codex CLI | stdin → JSONL + schema 결과 파일 | 기존 Codex 로그인 |
| claude_cli | Claude Code | stdin → native JSON schema / structured_output | 기존 Claude 로그인 또는 전역 키 |
| gemini_cli | Gemini CLI | stdin → stream-json | 기존 Google 로그인 또는 전역 키 |
| qwen_cli | Qwen Code | stdin → stream-json | 기존 Qwen/OAuth 또는 설정한 backend 인증 |
| opencode_cli | OpenCode | stdin → JSON events | 기존 provider 인증. 모델은 provider/model 형식 |
| copilot_cli | GitHub Copilot CLI | stdin → JSONL | 기존 Copilot 로그인 또는 전역 토큰 |
| goose_cli | Goose | stdin → quiet JSON text | 기존 Goose backend 인증 |
| ollama_cli | Ollama CLI | stdin → JSON text | 설치된 로컬 모델 또는 기존 Ollama 인증 |
| custom_cli | 공통 JSON 브리지 | JSON/text stdin, JSON argv 설정, JSON/JSONL/결과 파일 | 기존 CLI 인증 또는 지정한 환경변수 |

`claude`, `gemini`, `qwen`, `opencode`, `copilot`, `goose`, `codex`, `custom` 별칭도 canonical provider로 정규화합니다.
`ollama`는 기존 HTTP Adapter 이름이며 CLI는 `ollama_cli`를 사용합니다.

위 목록의 native 형식과 공통 프로토콜을 구현했습니다. **각 CLI의 로그인·모델 권한·비용·버전 호환성은 사용 환경에서 probe해야 합니다.**
TTY 전용 도구나 독자적인 출력 형식은 비대화형 JSON 브리지가 필요합니다.
Aider, Cursor Agent, Amp, Cline, Continue, Kilo, Kiro, Droid 등 추가 도구는 해당 버전의 비대화형 기능과 도구 차단 옵션을 설정한 브리지로 연결할 수 있습니다.
이 도구들의 모든 버전에 대한 native 옵션을 검증했다고 주장하지 않습니다.

## 설치와 목록

Codex는 기존 설치를 사용합니다. 아래 선택 설치는 Claude/Gemini/Qwen/OpenCode/Copilot의 고정 npm 버전을 저장소 내부에 설치합니다.
시스템 전역 설치와 기존 로그인 파일은 변경하지 않습니다. 실행 중인 CLI를 갱신하기 전 worker를 중지하세요.

```bash
bash scripts/install-clis.sh
.venv/bin/trading-team providers
```

lockfile: `tooling/cli/package-lock.json`. 설치 경로는 `tooling/cli/node_modules`이며 Git·Docker 이미지에서 제외합니다.
대부분의 lifecycle script는 실행하지 않으며, Claude/OpenCode의 공식 binary-linking postinstall만 명시적으로 실행합니다.
코드는 PATH와 저장소의 이 선택 설치 경로에서 실행 파일을 찾습니다. 잘못 설치된 텍스트 placeholder는 실행 가능한 CLI로 표시하지 않습니다.
Goose/Ollama는 공식 배포본을 설치하세요. Ollama 모델 다운로드를 자동으로 시작하지 않습니다.

실제 설치 및 `--help`/`--version`으로 확인한 버전:

| CLI | 버전 | 검사 범위 |
| --- | --- | --- |
| Claude Code | 2.1.306 | native 옵션 존재·실행 파일·버전 |
| Gemini CLI | 0.65.0 | headless/stream-json/policy/MCP 제한 옵션 |
| Qwen Code | 0.25.3 | headless/core-tools/exclude-tools/MCP 제한 옵션 |
| OpenCode | 1.19.3 | run/model/agent/JSON format 옵션 |
| Copilot CLI | 1.0.101 | model/JSONL/tools/remote 제한 옵션 |

이 검사는 실제 추론·로그인의 성공을 의미하지 않습니다. Goose/Ollama 바이너리는 이 클라우드에 설치하지 않았으며 계약 테스트로 검사했습니다.
현재 관리형 클라우드의 Codex 추론 401 제한도 그대로입니다.
Claude 2.1.306의 `--bare`는 OAuth/keychain 인증을 읽지 않으므로 사용하지 않습니다.
기존 로그인 경로를 유지하는 `--safe-mode`에 빈 tools·strict empty MCP·스킬 차단을 함께 적용합니다.

## Claude 또는 Gemini로 변경

사용자의 정상 동작하는 CLI 로그인 환경에서 `.env`를 수정합니다. `LLM_MODEL`에 해당 CLI에서 사용할 수 있는 **구체적인 모델 ID**를 입력하세요.
아래의 빈 모델 값은 입력 위치를 보여 주며, 비어 있으면 모델 호출이 차단됩니다.

```dotenv
LLM_PROVIDER=claude_cli
LLM_MODEL=
LLM_API_KEY=
LLM_CLI_EXECUTABLE=
LLM_CLI_COMMAND=[]
```

Gemini를 사용할 때는 전역 Provider를 바꿉니다.

```dotenv
LLM_PROVIDER=gemini_cli
LLM_MODEL=
LLM_API_KEY=
```

CLI의 기존 OAuth/구독 로그인 경로를 사용하므로 별도 API 키가 없어도 연결을 시도할 수 있습니다.
각 서비스의 가입·구독·모델 사용 한도는 그대로 적용됩니다. 앱 구독을 API 키로 변환하지 않습니다.
저장소 내부 설치를 직접 실행하려면 `tooling/cli/node_modules/.bin/claude` 또는 `tooling/cli/node_modules/.bin/gemini` 경로를 사용하세요.
로그인은 해당 CLI의 공식 대화형 절차로 수행하며, 이 앱이 자동 로그인하거나 로그인 파일을 옮기지 않습니다.

```bash
.venv/bin/trading-team init
.venv/bin/python scripts/services.py stop
.venv/bin/python scripts/services.py start --worldmonitor
.venv/bin/trading-team probe
```

대시보드의 Global Model Configuration과 `GET /providers`에서 설치·설정 목록을 확인할 수 있습니다.
연결 검사는 현재 전역 모델에 대해서만 실행합니다. 자동 Provider/모델 fallback, 자동 로그인, 새 키 발급은 하지 않습니다.
구독 로그인에서 사용할 수 없는 모델이나 CLI의 옵션이 바뀌면 실패 사유를 기록하고 진입을 차단합니다.

## 공통 브리지

추가 CLI의 inference 전용 비대화형 명령을 JSON argv 배열로 설정합니다. shell 문자열을 실행하거나 `eval`하지 않습니다.
명령에 키·토큰을 넣지 마세요. `LLM_CLI_API_KEY_ENV`로 전역 `LLM_API_KEY`를 필요한 native 변수에 전달할 수 있습니다.

```dotenv
LLM_PROVIDER=custom_cli
LLM_MODEL=
LLM_CLI_COMMAND=["/absolute/path/to/my-json-bridge"]
LLM_CLI_INPUT=json
LLM_CLI_OUTPUT=bridge_json
LLM_CLI_API_KEY_ENV=
```

브리지 stdin 계약:

```json
{
  "protocol_version": 1,
  "model": "YOUR_GLOBAL_MODEL_ID",
  "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
  "response_schema": {"type": "object", "properties": {}},
  "tool_policy": "deny"
}
```

브리지 stdout 계약:

```json
{
  "protocol_version": 1,
  "status": "completed",
  "model": "YOUR_GLOBAL_MODEL_ID",
  "response": {"ok": true},
  "tool_calls": 0,
  "usage": {"input_tokens": 10, "output_tokens": 5}
}
```

`response`는 요청받은 JSON schema에 맞아야 합니다. 위 `ok`는 예시입니다. usage는 생략 가능하며 모르는 값을 0으로 만들지 마세요.
model 불일치·tool_calls 발생·중복 JSON key·NaN/Infinity·추가 출력 필드는 거절합니다.
브리지는 모델을 실제로 지정하고 외부 도구/MCP/파일·명령 실행을 비활성화해야 합니다. 이 계약은 신뢰하는 로컬 프로그램과의 경계이며 OS sandbox를 제공하지 않습니다.

다른 CLI가 자체적으로 순수 JSON을 출력하는 경우:

```dotenv
LLM_PROVIDER=custom_cli
LLM_MODEL=
LLM_CLI_COMMAND=["/absolute/path/to/other-cli","--model","{model}","--prompt","{prompt}"]
LLM_CLI_INPUT=text
LLM_CLI_OUTPUT=json_text
```

해당 도구의 inference 전용·도구 차단 옵션을 argv에 추가하세요. text 모드에는 `{model}` 전달이 필수입니다.
플레이스홀더는 `{model}`, `{prompt}`, `{prompt_file}`, `{schema_file}`, `{output_file}`만 데이터로 치환합니다.
`{output_file}`을 사용하면 지정된 일반 파일만 읽으며 symlink/FIFO를 거절합니다.
기존 native 출력 형식을 쓰는 브리지는 `claude_json`, `gemini_jsonl`, `qwen_jsonl`, `opencode_jsonl`, `copilot_jsonl`도 선택할 수 있습니다.
대화 기록·배너·로그에서 JSON 일부를 추출하지 않습니다. 응답 전체가 규격에 맞아야 하며 assistant 외 user echo를 응답으로 채택하지 않습니다.

## 실행·도구·비용 경계

- 요청마다 별도의 임시 작업 디렉터리와 종료되는 프로세스를 사용합니다. 저장소 파일을 CLI 작업 대상으로 전달하지 않습니다.
- Claude는 safe mode/빈 tools/strict empty MCP/스킬 차단, Gemini·Qwen은 빈 core tools/deny 설정, OpenCode는 deny-all agent, Copilot은 도구 제한, Goose는 chat mode를 사용합니다.
- OpenCode의 작은 보조 모델도 같은 전역 모델로 지정합니다. 보고된 모델에 다른 모델이 섞이면 결과를 거절합니다. auto 모델 라우팅은 차단합니다.
- CLI 이벤트에 tool_use/command/MCP/file-change가 나타나거나 결과가 미완료이면 거절합니다. 요청된 모델만 확인 가능하고 실제 모델을 보고하지 않는 CLI의 한계도 있습니다.
- Claude safe mode에도 적용되는 관리자 hooks·정책과 Gemini 등의 system hooks는 별도 신뢰 경계입니다. 임의 버전·사용자 플러그인까지 OS 수준으로 격리했다고 주장하지 않습니다.
- stdout/stderr를 읽는 중에 크기를 제한하고, timeout·취소 시 프로세스 그룹과 pipe를 정리합니다. 잘못된 응답을 다른 Provider로 재시도하지 않습니다.
- API 관리 토큰·DB URL·PostgreSQL 비밀번호를 새 CLI 프로세스에 전달하지 않습니다. 기존 runtime 인증 selector·proxy·CA는 보존합니다.
- 키·raw stderr·프로세스 argv·로그인 파일 내용을 API/감사 로그에 저장하지 않습니다. 오류는 정해진 코드로 반환합니다.
- native CLI 사용량이 있어도 구독/API 과금 여부를 모르면 비용을 N/A로 둡니다. 전역 키를 명시해 사용하고 단가도 설정했을 때만 모델 비용을 추정합니다.

## 공식 참고 자료

- [Claude Code CLI](https://code.claude.com/docs/en/cli-reference)
- [Gemini headless](https://geminicli.com/docs/cli/headless/)
- [Qwen Code](https://github.com/QwenLM/qwen-code)
- [OpenCode CLI](https://opencode.ai/docs/cli/)
- [GitHub Copilot CLI](https://docs.github.com/en/copilot/reference/cli-command-reference)
- [Goose CLI](https://block.github.io/goose/docs/guides/running-from-cli/)
- [Ollama CLI](https://docs.ollama.com/cli)
