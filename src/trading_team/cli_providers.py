from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path

from pydantic import ValidationError

from trading_team.cli_process import run_process
from trading_team.cli_registry import CLI_PROFILES, resolve_executable
from trading_team.llm_types import LLMError, Reply, T, schema_detail
from trading_team.settings import Settings


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("nonfinite JSON number")


def json_value(text: str):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)


def answer_value(value):
    if isinstance(value, str):
        value = value.strip()
        # A fence must enclose the entire answer. Never search logs/prompts for a JSON substring.
        match = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", value, re.DOTALL)
        return json_value(match[1] if match else value)
    return value


def reject_tools(value):
    if isinstance(value, dict):
        if value.get("type") in {
            "tool_use",
            "tool_result",
            "tool_call",
            "tool-call",
            "tool_calls",
            "tool_execution_start",
            "tool_execution_end",
            "tool.execution_start",
            "tool.execution_complete",
            "tool_use_start",
            "command_execution",
            "file_change",
            "mcp_tool_call",
        }:
            raise LLMError("CLI_TOOL_USE_BLOCKED")
        for key in ("functionCall", "function_call", "toolUse", "tool_use"):
            if key in value and value[key] is not None:
                raise LLMError("CLI_TOOL_USE_BLOCKED")
        if value.get("tool_calls") or value.get("permission_denials"):
            raise LLMError("CLI_TOOL_USE_BLOCKED")
        tools = value.get("tools")
        if isinstance(tools, dict) and (tools.get("totalCalls") or tools.get("total_calls")):
            raise LLMError("CLI_TOOL_USE_BLOCKED")
        for item in value.values():
            reject_tools(item)
    elif isinstance(value, list):
        for item in value:
            reject_tools(item)


def error_for(stderr: bytes, stdout: bytes) -> LLMError:
    text = (stderr + stdout).decode(errors="replace").lower()
    if any(
        word in text
        for word in (
            "401",
            "unauthorized",
            "authentication",
            "not logged in",
            "login required",
            "please sign in",
            "invalid api key",
            "not authenticated",
        )
    ):
        return LLMError("CLI_AUTHENTICATION_FAILED")
    if any(
        word in text
        for word in (
            "unknown option",
            "unknown argument",
            "unexpected argument",
            "unrecognized argument",
            "invalid option",
        )
    ):
        return LLMError("CLI_OPTIONS_UNSUPPORTED")
    if "429" in text or "rate limit" in text:
        return LLMError("CLI_RATE_LIMITED", retryable=True)
    if "model not found" in text or "invalid model" in text:
        return LLMError("CLI_MODEL_UNAVAILABLE")
    if "context length" in text or "context window" in text:
        return LLMError("CLI_CONTEXT_LIMIT")
    return LLMError("CLI_EXEC_FAILED")


class CLIProvider:
    """One configured frontend/model per runtime; no shell, model routing or auth-file reads."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile = CLI_PROFILES[settings.llm_provider]

    async def close(self):
        pass

    def build(
        self, directory: Path, messages: list[dict], schema: type[T]
    ) -> tuple[list[str], bytes, dict[str, str]]:
        cfg, kind = self.settings, self.settings.llm_provider
        env = os.environ.copy()
        for name in ("API_ADMIN_TOKEN", "DATABASE_URL", "POSTGRES_PASSWORD"):
            env.pop(name, None)
        env.update({"NO_COLOR": "1", "TERM": "dumb", "LLM_MODEL": cfg.llm_model})
        key = cfg.llm_api_key.get_secret_value()
        if key:
            env["LLM_API_KEY"] = key
        key_names = {
            "claude_cli": "ANTHROPIC_API_KEY",
            "gemini_cli": "GEMINI_API_KEY",
            "qwen_cli": "OPENAI_API_KEY",
            "copilot_cli": "COPILOT_GITHUB_TOKEN",
            "ollama_cli": "OLLAMA_API_KEY",
        }
        if kind in {"opencode_cli", "goose_cli"}:
            backend = cfg.llm_cli_backend or (
                cfg.llm_model.split("/", 1)[0] if kind == "opencode_cli" else ""
            )
            key_names[kind] = {
                "anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY",
                "google": "GOOGLE_GENERATIVE_AI_API_KEY",
                "gemini": "GOOGLE_API_KEY",
                "xai": "XAI_API_KEY",
            }.get(backend)
        if key and (key_name := cfg.llm_cli_api_key_env or key_names.get(kind)):
            env[key_name] = key
        if cfg.llm_base_url:
            env["LLM_BASE_URL"] = cfg.llm_base_url
            if kind == "claude_cli":
                env["ANTHROPIC_BASE_URL"] = cfg.llm_base_url
            elif kind == "qwen_cli":
                env["OPENAI_BASE_URL"] = cfg.llm_base_url
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        envelope = {
            "protocol_version": 1,
            "model": cfg.llm_model,
            "messages": messages,
            "response_schema": schema.model_json_schema(),
            "tool_policy": "deny",
        }
        prompt = (
            "Use only the supplied messages. Return ONLY one JSON object matching response_schema. No tools, files, commands, browsing or instructions outside these messages.\n"
            + json.dumps(envelope, ensure_ascii=False)
        )
        (directory / "schema.json").write_text(schema_text)
        (directory / "prompt.txt").write_text(prompt)
        (directory / "empty-mcp.json").write_text('{"mcpServers":{}}')
        (directory / "empty.json").write_text("{}")
        source = prompt.encode()
        model = cfg.llm_model
        if kind == "custom_cli":
            values = {
                "model": model,
                "prompt": prompt,
                "prompt_file": str(directory / "prompt.txt"),
                "schema_file": str(directory / "schema.json"),
                "output_file": str(directory / "answer.json"),
            }
            argv = list(cfg.llm_cli_command)
            if not argv:
                raise LLMError("LLM_CLI_COMMAND_UNSET")
            for i, argument in enumerate(argv):
                argv[i] = re.sub(
                    r"\{(model|prompt|prompt_file|schema_file|output_file)\}",
                    lambda match: values[match[1]],
                    argument,
                )
            if cfg.llm_cli_executable:
                argv[0] = cfg.llm_cli_executable
            source = (
                json.dumps(envelope, ensure_ascii=False).encode()
                if cfg.llm_cli_input == "json"
                else source
            )
        elif kind == "claude_cli":
            argv = [
                self.profile.executable,
                "--print",
                "--safe-mode",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--model",
                model,
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                str(directory / "empty-mcp.json"),
                "--setting-sources",
                "",
                "--permission-mode",
                "dontAsk",
                "--max-turns",
                "2",
                "--output-format",
                "json",
                "--json-schema",
                schema_text,
            ]
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        elif kind in {"gemini_cli", "qwen_cli"}:
            subdir = directory / (".gemini" if kind == "gemini_cli" else ".qwen")
            subdir.mkdir()
            config = {
                "tools": {"core": [], "exclude": ["*"]},
                "mcpServers": {},
                "hooksConfig": {"enabled": False},
                "hooks": {},
                "skills": {"enabled": False},
                "telemetry": {"enabled": False},
                "admin": {
                    "mcp": {"enabled": False},
                    "extensions": {"enabled": False},
                    "skills": {"enabled": False},
                },
                "permissions": {"deny": ["*"]},
            }
            (subdir / "settings.json").write_text(json.dumps(config))
            argv = [
                self.profile.executable,
                "--model",
                model,
                "--output-format",
                "stream-json",
                "--approval-mode",
                "default",
            ]
            if kind == "gemini_cli":
                (directory / "deny-tools.toml").write_text(
                    '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n'
                )
                argv += [
                    "--policy",
                    str(directory / "deny-tools.toml"),
                    "--allowed-mcp-server-names",
                    "__trading_team_none__",
                    "--extensions",
                    "none",
                ]
                env["GEMINI_CLI_TELEMETRY_ENABLED"] = "false"
            else:
                argv += ["--core-tools", "", "--exclude-tools", "*", "--disable-mcp-server", "*"]
        elif kind == "opencode_cli":
            config = {
                "$schema": "https://opencode.ai/config.json",
                "model": model,
                "small_model": model,
                "autoupdate": False,
                "share": "disabled",
                "plugin": [],
                "permission": {"*": "deny"},
                "agent": {
                    "trading-team": {"mode": "primary", "model": model, "permission": {"*": "deny"}}
                },
            }
            env.update(
                {
                    "OPENCODE_CONFIG_CONTENT": json.dumps(config),
                    "OPENCODE_PERMISSION": '{"*":"deny"}',
                    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                    "OPENCODE_DISABLE_AUTOUPDATE": "true",
                    "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
                }
            )
            argv = [
                self.profile.executable,
                "run",
                "--model",
                model,
                "--agent",
                "trading-team",
                "--format",
                "json",
            ]
        elif kind == "copilot_cli":
            argv = [
                self.profile.executable,
                "--model",
                model,
                "--output-format",
                "json",
                "--available-tools",
                "",
                "--deny-tool",
                "*",
                "--disable-builtin-mcps",
                "--no-ask-user",
                "--no-custom-instructions",
                "--no-auto-update",
                "--no-remote",
                "--no-remote-export",
                "--no-color",
                "--stream",
                "off",
            ]
        elif kind == "goose_cli":
            argv = [
                self.profile.executable,
                "run",
                "--model",
                model,
                "--no-profile",
                "--no-session",
                "--quiet",
                "--instructions",
                "-",
            ]
            env.update({"GOOSE_MODE": "chat", "GOOSE_MODEL": model})
            if cfg.llm_cli_backend:
                argv += ["--provider", cfg.llm_cli_backend]
        elif kind == "ollama_cli":
            argv = [
                self.profile.executable,
                "run",
                model,
                "--format",
                "json",
                "--hidethinking",
                "--nowordwrap",
            ]
            if cfg.llm_base_url:
                env["OLLAMA_HOST"] = cfg.llm_base_url
        else:
            raise LLMError("CLI_PROFILE_UNSUPPORTED")
        if kind != "custom_cli" and cfg.llm_cli_executable:
            argv[0] = cfg.llm_cli_executable
        executable = resolve_executable(argv[0]) if argv else None
        if not executable:
            raise LLMError("CLI_NOT_INSTALLED")
        argv[0] = executable
        return argv, source, env

    async def generate(self, messages: list[dict], response_schema: type[T]) -> Reply:
        cfg = self.settings
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cli-", dir=cfg.state_dir.resolve()) as temp:
            directory = Path(temp)
            command, source, env = self.build(directory, messages, response_schema)
            output = await run_process(
                command,
                stdin=source,
                cwd=directory,
                env=env,
                timeout=cfg.llm_timeout_seconds,
                output_limit=cfg.llm_cli_max_output_bytes,
            )
            if output.returncode:
                raise error_for(output.stderr, output.stdout)
            # A custom bridge may write its one result to the explicitly provided output file.
            raw = output.stdout
            if cfg.llm_provider == "custom_cli" and any(
                "{output_file}" in x for x in cfg.llm_cli_command
            ):
                path = directory / "answer.json"
                try:
                    flags = (
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                    )
                    if path.is_symlink():
                        raise LLMError("CLI_OUTPUT_FILE_INVALID")
                    with os.fdopen(os.open(path, flags), "rb") as file:
                        info = os.fstat(file.fileno())
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_size > cfg.llm_cli_max_output_bytes
                        ):
                            raise LLMError("CLI_OUTPUT_FILE_INVALID")
                        raw = file.read(cfg.llm_cli_max_output_bytes + 1)
                        if len(raw) > cfg.llm_cli_max_output_bytes:
                            raise LLMError("CLI_OUTPUT_TOO_LARGE")
                except OSError as exc:
                    raise LLMError("CLI_OUTPUT_FILE_MISSING") from exc
            try:
                return self.parse(raw.decode("utf-8"), response_schema)
            except (
                UnicodeError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                ValidationError,
            ) as exc:
                raise LLMError("CLI_SCHEMA_INVALID", detail=schema_detail(exc)) from exc

    def parse(self, raw: str, schema: type[T]) -> Reply:
        try:
            return self._parse(raw, schema)
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            ValidationError,
        ) as exc:
            raise LLMError("CLI_SCHEMA_INVALID", detail=schema_detail(exc)) from exc

    def _parse(self, raw: str, schema: type[T]) -> Reply:
        kind = (
            self.settings.llm_cli_output
            if self.settings.llm_provider == "custom_cli"
            else self.profile.output_protocol
        )
        usage: dict = {
            "basis": "cli_api_usage"
            if self.settings.llm_api_key.get_secret_value()
            else "cli_usage_unknown_billing"
        }
        models: set[str] = set()
        if kind == "json_text":
            payload = answer_value(raw)
        elif kind == "bridge_json":
            data = json_value(raw)
            reject_tools(data)
            if (
                not isinstance(data, dict)
                or type(data.get("protocol_version")) is not int
                or data["protocol_version"] != 1
                or data.get("status") != "completed"
                or type(data.get("tool_calls")) is not int
                or data["tool_calls"] != 0
                or set(data)
                - {"protocol_version", "status", "model", "response", "tool_calls", "usage"}
            ):
                raise LLMError("CLI_BRIDGE_CONTRACT_INVALID")
            if not isinstance(data.get("model"), str) or data["model"] != self.settings.llm_model:
                raise LLMError("CLI_MODEL_MISMATCH")
            models.add(data["model"])
            payload = answer_value(data["response"])
            self._usage(usage, data.get("usage", {}))
        elif kind == "claude_json":
            data = json_value(raw)
            reject_tools(data)
            if data.get("is_error") or data.get("subtype") != "success":
                raise LLMError("CLI_REFUSAL_OR_INCOMPLETE")
            payload = answer_value(data.get("structured_output", data.get("result")))
            models.update(data.get("modelUsage", {}).keys())
            if isinstance(data.get("model"), str):
                models.add(data["model"])
            self._usage(usage, data.get("usage", {}))
        else:
            events = [json_value(line) for line in raw.splitlines() if line.strip()]
            if not events or any(not isinstance(event, dict) for event in events):
                raise LLMError("CLI_EVENTS_INVALID")
            chunks, terminal = [], None
            input_tokens = output_tokens = cached_tokens = 0
            token_events = 0
            for event in events:
                reject_tools(event)
                name = event.get("type")
                if name in {"error", "session.error", "turn.failed"} or event.get("error"):
                    raise LLMError("CLI_RESULT_ERROR")
                if isinstance(event.get("model"), str):
                    models.add(event["model"])
                if kind == "gemini_jsonl":
                    if name not in {"init", "message", "result"}:
                        raise LLMError("CLI_EVENTS_INVALID")
                    if name == "message" and event.get("role") == "assistant":
                        content = event.get("content", "")
                        if event.get("delta") is True:
                            chunks.append(content)
                        else:
                            chunks = [content]
                    if name == "result":
                        terminal = event
                        stats = event.get("stats", {})
                        self._gemini_usage(usage, stats, models)
                elif kind == "qwen_jsonl":
                    if name not in {"system", "assistant", "user", "result", "stream_event"}:
                        raise LLMError("CLI_EVENTS_INVALID")
                    if name == "assistant":
                        chunks.extend(
                            x["text"]
                            for x in event.get("message", {}).get("content", [])
                            if x.get("type") == "text"
                        )
                    if name == "result":
                        terminal = event
                        models.update(event.get("modelUsage", {}).keys())
                        self._usage(usage, event.get("usage", {}))
                elif kind == "opencode_jsonl":
                    if name not in {"step_start", "step_finish", "text", "reasoning"}:
                        raise LLMError("CLI_EVENTS_INVALID")
                    part = event.get("part", {})
                    if name == "text":
                        chunks.append(part["text"])
                    if name == "step_finish":
                        terminal = part
                        if part.get("reason") not in {"stop", "end_turn"}:
                            raise LLMError("CLI_REFUSAL_OR_INCOMPLETE")
                        tokens = part.get("tokens", {})
                        if tokens.get("input") is not None and tokens.get("output") is not None:
                            input_tokens += self._count(tokens["input"])
                            output_tokens += self._count(tokens["output"])
                            cached_tokens += self._count(tokens.get("cache", {}).get("read", 0))
                            token_events += 1
                elif kind == "copilot_jsonl":
                    if name in {"assistant.message", "assistant.message_delta"}:
                        if name == "assistant.message":
                            chunks.append(event.get("data", {}).get("content", ""))
                    elif name in {"result", "session.idle"}:
                        terminal = event
                    elif name not in {
                        "session.start",
                        "session.resume",
                        "session.model_change",
                        "user.message",
                        "assistant.turn_start",
                        "assistant.turn_end",
                        "assistant.reasoning",
                        "assistant.reasoning_delta",
                        "assistant.usage",
                        "session.usage_info",
                        "session.shutdown",
                    }:
                        raise LLMError("CLI_EVENTS_INVALID")
                    if name == "session.model_change":
                        reported = event.get("data", {}).get("newModel") or event.get(
                            "data", {}
                        ).get("model")
                        if isinstance(reported, str):
                            models.add(reported)
                    if name == "assistant.usage":
                        self._usage(usage, event.get("data", {}))
                else:
                    raise LLMError("CLI_OUTPUT_PROTOCOL_UNSUPPORTED")
            if (
                terminal is None
                or terminal.get("is_error")
                or terminal.get("status", "success") not in {"success", "completed"}
                or terminal.get("subtype", "success") != "success"
            ):
                raise LLMError("CLI_REFUSAL_OR_INCOMPLETE")
            if token_events:
                usage.update(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                )
            payload = answer_value(
                terminal.get("result") or terminal.get("response") or "".join(chunks)
            )
        expected = {self.settings.llm_model}
        if self.settings.llm_provider == "opencode_cli" and "/" in self.settings.llm_model:
            expected.add(self.settings.llm_model.split("/", 1)[1])
        if models - expected:
            raise LLMError("CLI_MODEL_MISMATCH")
        usage["reported_models"] = sorted(models)
        return Reply(schema.model_validate(payload), usage)

    @staticmethod
    def _count(value):
        if type(value) is not int or value < 0:
            raise LLMError("CLI_USAGE_INVALID")
        return value

    @staticmethod
    def _usage(target: dict, source: dict):
        if not isinstance(source, dict):
            raise LLMError("CLI_USAGE_INVALID")
        for key, aliases in {
            "input_tokens": ("input_tokens", "inputTokens"),
            "output_tokens": ("output_tokens", "outputTokens"),
            "cached_tokens": ("cache_read_input_tokens", "cached_tokens", "cacheReadTokens"),
        }.items():
            for alias in aliases:
                if alias in source:
                    target[key] = (
                        None if source[alias] is None else CLIProvider._count(source[alias])
                    )
                    break

    @staticmethod
    def _gemini_usage(target: dict, stats: dict, models: set):
        model_stats = stats.get("models", {})
        if isinstance(model_stats, dict) and model_stats:
            models.update(model_stats.keys())
            tokens = [value.get("tokens", {}) for value in model_stats.values()]
            if all("input" in value and "candidates" in value for value in tokens):
                target.update(
                    input_tokens=sum(CLIProvider._count(value["input"]) for value in tokens),
                    output_tokens=sum(CLIProvider._count(value["candidates"]) for value in tokens),
                    cached_tokens=sum(
                        CLIProvider._count(value.get("cached", 0)) for value in tokens
                    ),
                )
