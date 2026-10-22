from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CLIProfile:
    provider: str
    name: str
    executable: str
    output_protocol: str
    documentation: str
    install_hint: str
    tool_control: str


CLI_PROFILES = {
    item.provider: item
    for item in (
        CLIProfile(
            "codex_cli",
            "Codex CLI",
            "codex",
            "codex_jsonl_file",
            "https://developers.openai.com/codex/cli/reference/",
            "Install the official Codex CLI and sign in normally.",
            "read-only sandbox; tool features disabled; reject tool events",
        ),
        CLIProfile(
            "claude_cli",
            "Claude Code",
            "claude",
            "claude_json",
            "https://code.claude.com/docs/en/cli-reference",
            "npm install -g @anthropic-ai/claude-code",
            "safe mode preserves normal auth; skills disabled; empty tools and strict empty MCP config",
        ),
        CLIProfile(
            "gemini_cli",
            "Gemini CLI",
            "gemini",
            "gemini_jsonl",
            "https://geminicli.com/docs/cli/headless/",
            "npm install -g @google/gemini-cli",
            "empty core tools; MCP/extensions/hooks disabled; deny policy",
        ),
        CLIProfile(
            "qwen_cli",
            "Qwen Code",
            "qwen",
            "qwen_jsonl",
            "https://github.com/QwenLM/qwen-code",
            "npm install -g @qwen-code/qwen-code",
            "empty core tools; deny all; disable MCP",
        ),
        CLIProfile(
            "opencode_cli",
            "OpenCode",
            "opencode",
            "opencode_jsonl",
            "https://opencode.ai/docs/cli/",
            "npm install -g opencode-ai",
            "permission deny all; temporary agent; plugins disabled",
        ),
        CLIProfile(
            "copilot_cli",
            "GitHub Copilot CLI",
            "copilot",
            "copilot_jsonl",
            "https://docs.github.com/en/copilot/reference/cli-command-reference",
            "npm install -g @github/copilot",
            "empty available tools; deny all; built-in MCP disabled",
        ),
        CLIProfile(
            "goose_cli",
            "Goose",
            "goose",
            "json_text",
            "https://block.github.io/goose/docs/guides/running-from-cli/",
            "Install the official Goose binary and configure one provider.",
            "chat mode; no extension profile; no session",
        ),
        CLIProfile(
            "ollama_cli",
            "Ollama CLI",
            "ollama",
            "json_text",
            "https://docs.ollama.com/cli",
            "Install Ollama and explicitly pull your chosen model.",
            "generation only; no tools supplied",
        ),
        CLIProfile(
            "custom_cli",
            "Custom JSON CLI bridge",
            "",
            "bridge_json",
            "docs/cli-providers.md",
            "Configure LLM_CLI_COMMAND as a JSON argv array for your bridge.",
            "bridge contract requires zero tool calls; operator supplies a trusted inference-only bridge",
        ),
    )
}
CLI_PROVIDER_NAMES = frozenset(CLI_PROFILES)


def executable_ready(path: str) -> bool:
    if os.name != "posix":
        return True
    try:
        with open(path, "rb") as executable:
            prefix = executable.read(4)
        return prefix.startswith((b"#!", b"\x7fELF")) or prefix in {
            b"\xcf\xfa\xed\xfe",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
        }
    except OSError:
        return False


def resolve_executable(name: str) -> str | None:
    if (found := shutil.which(name)) and executable_ready(found):
        return str(Path(found).absolute())
    # Optional repository-local npm bundle; never modify a user's global installation.
    local = Path(__file__).resolve().parents[2] / "tooling" / "cli" / "node_modules" / ".bin" / name
    if (
        not Path(name).is_absolute()
        and len(Path(name).parts) == 1
        and (found := shutil.which(str(local)))
        and executable_ready(found)
    ):
        return str(Path(found).absolute())
    return None


def inventory() -> list[dict]:
    return [
        {
            **asdict(profile),
            "installed": bool(resolve_executable(profile.executable))
            if profile.executable
            else False,
            "inference_verified": False,
            "verification_note": "Installation detection only. Probe the one configured provider/model to verify inference.",
        }
        for profile in CLI_PROFILES.values()
    ]


def transport_info(settings) -> dict:
    if settings.llm_provider == "llama_cpp":
        return {
            "kind": "local_http",
            "name": "llama.cpp CPU",
            "authentication": "not_required_loopback",
            "tool_control": "loopback inference only; native tools/MCP/agent mode disabled",
        }
    if settings.llm_provider not in CLI_PROFILES:
        return {"kind": "http", "authentication": "not_checked"}
    profile = CLI_PROFILES[settings.llm_provider]
    executable = settings.llm_cli_executable or (
        settings.llm_cli_command[0]
        if settings.llm_provider == "custom_cli" and settings.llm_cli_command
        else settings.codex_executable
        if settings.llm_provider == "codex_cli"
        else profile.executable
    )
    return {
        "kind": "cli",
        "name": profile.name,
        "installed": bool(executable and resolve_executable(executable)),
        "tool_control": profile.tool_control,
        "authentication": "not_checked",
        "output_protocol": settings.llm_cli_output
        if settings.llm_provider == "custom_cli"
        else profile.output_protocol,
    }
