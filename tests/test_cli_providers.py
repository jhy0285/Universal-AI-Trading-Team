from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from pydantic import SecretStr, ValidationError

from trading_team.cli_process import run_process
from trading_team.cli_providers import CLIProvider
from trading_team.cli_registry import CLI_PROFILES, inventory, transport_info
from trading_team.domain import ProbeOutput, ScanOutput
from trading_team.llm import LLMError, LLMRuntime
from trading_team.settings import Settings

NATIVE = [name for name in CLI_PROFILES if name not in {"codex_cli", "custom_cli"}]


def config(tmp_path, provider="custom_cli", **values):
    return Settings(
        _env_file=None,
        llm_provider=provider,
        llm_model="one-model",
        state_dir=tmp_path / "state",
        **values,
    )


def native_output(provider, payload=None, model="one-model"):
    payload = payload or {"ok": True}
    content = json.dumps(payload)
    if provider == "claude_cli":
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": payload,
                "modelUsage": {model: {}},
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
    if provider == "gemini_cli":
        events = [
            {"type": "init", "model": model},
            {"type": "message", "role": "assistant", "content": content, "delta": True},
            {
                "type": "result",
                "status": "success",
                "stats": {
                    "models": {model: {"tokens": {"input": 10, "candidates": 5, "cached": 0}}}
                },
            },
        ]
    elif provider == "qwen_cli":
        events = [
            {"type": "system", "subtype": "init", "model": model},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": content}]},
            },
            {
                "type": "result",
                "subtype": "success",
                "result": content,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ]
    elif provider == "opencode_cli":
        events = [
            {"type": "step_start", "part": {}},
            {"type": "text", "part": {"text": content}},
            {
                "type": "step_finish",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 10, "output": 5, "cache": {"read": 0}},
                },
            },
        ]
    elif provider == "copilot_cli":
        events = [
            {"type": "session.start", "data": {}},
            {"type": "assistant.message", "data": {"content": content}},
            {"type": "session.idle", "data": {}},
        ]
    else:
        return content
    return "\n".join(json.dumps(event) for event in events)


@pytest.mark.parametrize("provider", NATIVE)
def test_native_response_protocols(provider, tmp_path):
    reply = CLIProvider(config(tmp_path, provider)).parse(native_output(provider), ProbeOutput)
    assert reply.payload.ok
    assert reply.usage["basis"] == "cli_usage_unknown_billing"


@pytest.mark.parametrize("provider", NATIVE)
def test_native_invalid_or_echoed_json_never_becomes_output(provider, tmp_path):
    adapter = CLIProvider(config(tmp_path, provider))
    with pytest.raises(LLMError):
        adapter.parse('Here is your JSON: {"ok":true}', ProbeOutput)
    with pytest.raises(LLMError):
        adapter.parse(native_output(provider, {"ok": True, "order": "buy"}), ProbeOutput)


@pytest.mark.parametrize(
    "provider", ["claude_cli", "gemini_cli", "qwen_cli", "opencode_cli", "copilot_cli"]
)
def test_native_tool_events_are_rejected(provider, tmp_path):
    adapter = CLIProvider(config(tmp_path, provider))
    body = native_output(provider)
    if provider == "claude_cli":
        data = json.loads(body)
        data["permission_denials"] = [{"tool_name": "Bash"}]
        body = json.dumps(data)
    else:
        body = (
            json.dumps(
                {
                    "type": "tool.execution_start" if provider == "copilot_cli" else "tool_use",
                    "name": "shell",
                }
            )
            + "\n"
            + body
        )
    with pytest.raises(LLMError, match="CLI_TOOL_USE_BLOCKED"):
        adapter.parse(body, ProbeOutput)


@pytest.mark.parametrize("provider", ["claude_cli", "gemini_cli", "qwen_cli"])
def test_native_reported_model_fallback_rejected(provider, tmp_path):
    with pytest.raises(LLMError, match="CLI_MODEL_MISMATCH"):
        CLIProvider(config(tmp_path, provider)).parse(
            native_output(provider, model="different-model"), ProbeOutput
        )


@pytest.mark.parametrize("provider", ["gemini_cli", "qwen_cli", "opencode_cli", "copilot_cli"])
def test_stream_requires_terminal_and_ignores_user_echo(provider, tmp_path):
    adapter = CLIProvider(config(tmp_path, provider))
    lines = native_output(provider).splitlines()
    with pytest.raises(LLMError, match="CLI_REFUSAL_OR_INCOMPLETE"):
        adapter.parse("\n".join(lines[:-1]), ProbeOutput)
    if provider == "gemini_cli":
        data = [json.loads(line) for line in lines]
        data[1]["role"] = "user"
        with pytest.raises(LLMError):
            adapter.parse("\n".join(json.dumps(item) for item in data), ProbeOutput)


def bridge(payload=None, **changes):
    return {
        "protocol_version": 1,
        "status": "completed",
        "model": "one-model",
        "response": payload or {"ok": True},
        "tool_calls": 0,
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"model": "other-model"},
        {"protocol_version": 2},
        {"protocol_version": True},
        {"unexpected_metadata": "untrusted"},
        {"tool_calls": True},
        {"tool_calls": 1},
        {"status": "error"},
    ],
)
def test_custom_bridge_contract_rejects_invalid(change, tmp_path):
    adapter = CLIProvider(config(tmp_path))
    with pytest.raises(LLMError):
        adapter.parse(json.dumps(bridge(**change)), ProbeOutput)


@pytest.mark.parametrize("raw", ['{"ok":true,"ok":false}', '{"ok":NaN}', "[]", "null", "not JSON"])
def test_duplicate_keys_nonfinite_and_nonobjects_rejected(raw, tmp_path):
    adapter = CLIProvider(config(tmp_path, llm_cli_output="json_text"))
    with pytest.raises(LLMError):
        adapter.parse(raw, ProbeOutput)


@pytest.mark.parametrize(
    "usage", [{"input_tokens": -1}, {"input_tokens": True}, {"output_tokens": "2"}]
)
def test_invalid_usage_is_not_hidden_by_aggregation(usage, tmp_path):
    with pytest.raises(LLMError, match="CLI_USAGE_INVALID"):
        CLIProvider(config(tmp_path)).parse(json.dumps(bridge(usage=usage)), ProbeOutput)


def make_executable(tmp_path, body, name="fixture-cli"):
    executable = tmp_path / name
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize("provider", NATIVE)
async def test_native_subprocess_receives_global_model_and_validates_output(provider, tmp_path):
    body = (
        "import sys,json,pathlib\ntext=sys.stdin.read()\nenvelope=json.loads(text.split('\\n',1)[1])\nassert envelope['model']=='one-model'\nassert 'one-model' in sys.argv\nassert pathlib.Path('schema.json').exists()\nprint("
        + repr(native_output(provider))
        + ")\n"
    )
    executable = make_executable(tmp_path, body)
    cfg = config(tmp_path, provider, llm_cli_executable=str(executable))
    reply = await CLIProvider(cfg).generate([], ProbeOutput)
    assert reply.payload.ok
    assert not list(cfg.state_dir.glob("cli-*"))


async def test_custom_bridge_complete_runtime_probe_and_billing_unknown(tmp_path):
    executable = make_executable(
        tmp_path,
        "import sys,json\nenvelope=json.load(sys.stdin)\nrequest=json.loads(envelope['messages'][-1]['content'])\nprint(json.dumps({'protocol_version':1,'status':'completed','model':envelope['model'],'response':request['example'],'tool_calls':0,'usage':{'input_tokens':10,'output_tokens':5}}))\n",
    )
    cfg = config(tmp_path, llm_cli_command=(str(executable),))
    attempts = []
    runtime = LLMRuntime(cfg, record_attempt=attempts.append)
    try:
        result = await runtime.probe()
        assert result["verified"] and len(attempts) == 4
        assert {item.model for item in attempts} == {"one-model"}
        assert {item.provider for item in attempts} == {"custom_cli"}
        assert {item.basis for item in attempts} == {"cli_usage_unknown_billing"}
        assert result["transport"]["authentication"] == "verified"
        for role in ["SIZE", "FILLS", "RISK"]:
            with pytest.raises(LLMError, match="DETERMINISTIC_ROLE"):
                await runtime.generate(role, {}, ScanOutput, "c")
    finally:
        await runtime.close()


def test_gemini_uses_documented_no_extensions_selector(tmp_path):
    executable = make_executable(tmp_path, "pass\n")
    cfg = config(tmp_path, "gemini_cli", llm_cli_executable=str(executable))
    directory = tmp_path / "gemini-work"
    directory.mkdir()
    command, _, _ = CLIProvider(cfg).build(directory, [], ProbeOutput)
    assert command[command.index("--extensions") + 1] == "none"
    settings = json.loads((directory / ".gemini" / "settings.json").read_text())
    assert settings["tools"]["core"] == []
    assert not settings["hooksConfig"]["enabled"]


async def test_custom_text_argv_does_not_execute_shell_and_output_file(tmp_path):
    executable = make_executable(
        tmp_path,
        "import sys,json,pathlib\njson.load(sys.stdin)\nassert sys.argv[1] == 'one-model'\nassert sys.argv[2] == 'literal; $(touch SHOULD_NOT_EXIST)'\npathlib.Path(sys.argv[3]).write_text(json.dumps({'ok':True}))\n",
    )
    cfg = config(
        tmp_path,
        llm_cli_command=(
            str(executable),
            "{model}",
            "literal; $(touch SHOULD_NOT_EXIST)",
            "{output_file}",
        ),
        llm_cli_output="json_text",
    )
    assert (await CLIProvider(cfg).generate([], ProbeOutput)).payload.ok
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


@pytest.mark.parametrize("mode", ["fifo", "symlink"])
async def test_custom_output_file_must_be_regular(mode, tmp_path):
    statement = (
        "os.mkfifo(sys.argv[1])" if mode == "fifo" else "os.symlink('schema.json', sys.argv[1])"
    )
    executable = make_executable(tmp_path, "import sys,os\nsys.stdin.read()\n" + statement + "\n")
    cfg = config(tmp_path, llm_cli_command=(str(executable), "{output_file}"))
    with pytest.raises(LLMError, match="CLI_OUTPUT_FILE_INVALID"):
        await asyncio.wait_for(CLIProvider(cfg).generate([], ProbeOutput), timeout=3)


def test_child_credentials_mapping_and_unrelated_app_secrets_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("API_ADMIN_TOKEN", "do-not-forward-admin")
    monkeypatch.setenv("DATABASE_URL", "do-not-forward-db")
    monkeypatch.setenv("POSTGRES_PASSWORD", "do-not-forward-password")
    executable = make_executable(tmp_path, "pass\n")
    cfg = config(
        tmp_path,
        "claude_cli",
        llm_cli_executable=str(executable),
        llm_api_key=SecretStr("fake-global-key"),
    )
    directory = tmp_path / "child"
    directory.mkdir()
    command, _, child_env = CLIProvider(cfg).build(directory, [], ProbeOutput)
    assert child_env["ANTHROPIC_API_KEY"] == "fake-global-key"
    assert child_env["LLM_MODEL"] == "one-model"
    assert not any(
        key in child_env for key in ["API_ADMIN_TOKEN", "DATABASE_URL", "POSTGRES_PASSWORD"]
    )
    assert "fake-global-key" not in " ".join(command)
    assert "--tools" in command and command[command.index("--tools") + 1] == ""
    assert "--safe-mode" in command and "--strict-mcp-config" in command
    assert "--disable-slash-commands" in command
    assert "--bare" not in command  # Claude bare mode skips normal OAuth credentials.
    assert "--dangerously-skip-permissions" not in command


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("401 unauthorized", "CLI_AUTHENTICATION_FAILED"),
        ("unknown option --newflag", "CLI_OPTIONS_UNSUPPORTED"),
        ("model not found", "CLI_MODEL_UNAVAILABLE"),
        ("429 rate limit", "CLI_RATE_LIMITED"),
    ],
)
async def test_cli_error_messages_are_redacted(tmp_path, reason, expected):
    executable = make_executable(
        tmp_path,
        "import sys\nsys.stdin.read()\nsys.stderr.write("
        + repr(reason + " SECRET_VALUE")
        + ")\nsys.exit(1)\n",
    )
    cfg = config(tmp_path, llm_cli_command=(str(executable),))
    with pytest.raises(LLMError, match=expected) as error:
        await CLIProvider(cfg).generate([], ProbeOutput)
    assert "SECRET_VALUE" not in str(error.value)


async def test_bounded_output_reaps_process(tmp_path):
    executable = make_executable(
        tmp_path,
        "import sys,time\nsys.stdout.write('x'*2000000)\nsys.stdout.flush()\ntime.sleep(30)\n",
    )
    with pytest.raises(LLMError, match="CLI_OUTPUT_TOO_LARGE"):
        await run_process(
            [str(executable)],
            stdin=b"",
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=3,
            output_limit=1024,
        )


async def test_timeout_and_cancellation_reap_process(tmp_path):
    pidfile = tmp_path / "pid"
    executable = make_executable(
        tmp_path,
        f"import os,pathlib,time\npathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n",
    )
    with pytest.raises(LLMError, match="CLI_TIMEOUT"):
        await run_process(
            [str(executable)],
            stdin=b"",
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=0.1,
            output_limit=1024,
        )
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    task = asyncio.create_task(
        run_process(
            [str(executable)],
            stdin=b"",
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=30,
            output_limit=1024,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("provider", NATIVE + ["custom_cli"])
def test_cli_login_does_not_require_global_api_key(provider, tmp_path):
    assert "LLM_API_KEY_UNSET" not in config(tmp_path, provider).model_blockers()


def test_custom_config_revision_and_secret_free_inventory(tmp_path):
    first = config(tmp_path, llm_cli_command=("my-bridge",))
    second = config(tmp_path, llm_cli_command=("different-bridge",))
    assert first.revision != second.revision
    assert first.revision == first.model_copy(update={"llm_api_key": SecretStr("rotated")}).revision
    assert "LLM_CLI_COMMAND_UNSET" in config(tmp_path).model_blockers()
    assert (
        "CLI_AUTOMATIC_MODEL_ROUTING_NOT_ALLOWED"
        in config(tmp_path, "copilot_cli").model_copy(update={"llm_model": "auto"}).model_blockers()
    )
    names = {item["provider"] for item in inventory()}
    assert {"claude_cli", "gemini_cli", "custom_cli"} <= names
    assert transport_info(first)["authentication"] == "not_checked"


@pytest.mark.parametrize("value", ["HOME", "PATH", "CODEX_HOME", "LD_PRELOAD", "DATABASE_URL"])
def test_key_mapping_cannot_repurpose_system_or_app_variables(value, tmp_path):
    with pytest.raises(ValidationError):
        config(tmp_path, llm_cli_api_key_env=value)


async def test_uninstalled_native_placeholder_is_rejected_before_spawn(tmp_path):
    stub = tmp_path / "broken-cli"
    stub.write_text('echo "native binary not installed"\n')
    stub.chmod(0o700)
    with pytest.raises(LLMError, match="CLI_START_FAILED"):
        await asyncio.wait_for(
            run_process(
                [str(stub)],
                stdin=b"",
                cwd=tmp_path,
                env=os.environ.copy(),
                timeout=1,
                output_limit=1024,
            ),
            timeout=2,
        )


def test_native_command_cannot_override_tool_restrictions(tmp_path):
    with pytest.raises(ValidationError):
        config(tmp_path, "claude_cli", llm_cli_command=("claude", "--dangerously-skip-permissions"))
    with pytest.raises(ValidationError):
        config(tmp_path, llm_cli_command=("bridge", "--api-key", "fake-secret"))
