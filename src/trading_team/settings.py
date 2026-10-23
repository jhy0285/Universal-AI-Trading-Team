from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_team.cli_registry import CLI_PROVIDER_NAMES

ProviderName = Literal[
    "codex_cli",
    "claude_cli",
    "gemini_cli",
    "qwen_cli",
    "opencode_cli",
    "copilot_cli",
    "goose_cli",
    "ollama_cli",
    "custom_cli",
    "openai",
    "anthropic",
    "google",
    "xai",
    "ollama",
    "openai_compatible",
    "llama_cpp",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        def checked_dotenv():
            values = dotenv_settings()
            forbidden = {
                f"{role}_{suffix}"
                for role in ("CHIEF", "SCAN", "VET", "BOOK", "SIZE", "FILLS", "RISK")
                for suffix in ("MODEL", "PROVIDER", "API_KEY")
            }
            if any(key.upper() in forbidden and value for key, value in values.items()):
                raise ValueError("per-agent LLM configuration is not supported")
            return values

        return init_settings, env_settings, checked_dotenv, file_secret_settings

    trading_mode: Literal["paper"] = "paper"
    # Virtual USD for a newly created main ledger. An existing ledger keeps its own history.
    paper_initial_cash: Decimal = Field(
        default=Decimal("1000"), gt=0, le=100_000_000, allow_inf_nan=False
    )
    llm_provider: ProviderName = "codex_cli"
    llm_model: str = Field(default="", max_length=150)
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = ""
    llm_timeout_seconds: float = Field(default=120, ge=1, le=300)
    llm_max_retries: int = Field(default=1, ge=0, le=2)
    local_llm_model_url: str = ""
    local_llm_model_sha256: str = ""
    local_llm_threads: int = Field(default=4, ge=1, le=32)
    local_llm_context_size: int = Field(default=8192, ge=2048, le=32768)
    autonomous_enabled: bool = False
    autonomous_collect_seconds: int = Field(default=60, ge=30, le=900)
    autonomous_probe_retry_seconds: int = Field(default=3600, ge=900, le=86400)
    llm_cli_executable: str = ""
    llm_cli_command: tuple[str, ...] = Field(default=(), repr=False)
    llm_cli_input: Literal["json", "text"] = "json"
    llm_cli_output: Literal[
        "bridge_json",
        "json_text",
        "claude_json",
        "gemini_jsonl",
        "qwen_jsonl",
        "opencode_jsonl",
        "copilot_jsonl",
    ] = "bridge_json"
    llm_cli_api_key_env: str = ""
    llm_cli_backend: str = ""
    llm_cli_max_output_bytes: int = Field(default=2_000_000, ge=1024, le=8_000_000)
    codex_executable: str = "codex"
    codex_runner: Literal["local", "docker"] = "local"
    codex_container_image: str = (
        "debian@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8"
    )
    database_url: str = "sqlite:///.state/trading.sqlite3"
    api_admin_token: SecretStr = SecretStr("")
    worldmonitor_base_url: str = "http://127.0.0.1:3000"
    worldmonitor_redis_token: SecretStr = SecretStr("")
    worldmonitor_seed_enabled: bool = False
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    # Original strategy: candidates come from DexScreener launch data only.
    dex_discovery: Literal["launches", "search", "both"] = "launches"
    dex_queries: str = "BONK"
    market_data_feed_url: str = ""
    market_data_feed_api_key: SecretStr = SecretStr("")
    # Free Jupiter/Solana RPC/GeckoTerminal observations for the most liquid Dex pools.
    market_observer_enabled: bool = True
    market_observer_max_assets: int = Field(default=3, ge=1, le=10)
    # Optional free key from Jupiter's portal: 60 instead of 30 requests/minute.
    jupiter_api_key: SecretStr = SecretStr("")
    # Unreferenced live observation evidence older than this is pruned daily; 0 keeps everything.
    snapshot_retention_days: int = Field(default=14, ge=0, le=3650)
    state_dir: Path = Path(".state")
    quote_max_age_seconds: int = Field(default=60, ge=1, le=300)
    market_max_age_seconds: int = Field(default=300, ge=1, le=900)
    policy_version: Literal["paper-interpretation-v1"] = "paper-interpretation-v1"
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)

    @field_validator("llm_provider", mode="before")
    @classmethod
    def provider_alias(cls, value):
        return {
            "codex": "codex_cli",
            "claude": "claude_cli",
            "gemini": "gemini_cli",
            "qwen": "qwen_cli",
            "opencode": "opencode_cli",
            "copilot": "copilot_cli",
            "goose": "goose_cli",
            "custom": "custom_cli",
        }.get(value, value)

    @field_validator("llm_model")
    @classmethod
    def model_id(cls, value: str) -> str:
        if any(c in value for c in "\n\r\x00"):
            raise ValueError("invalid model ID")
        return value.strip()

    @field_validator("worldmonitor_base_url")
    @classmethod
    def local_worldmonitor_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
            "worldmonitor",
        }:
            raise ValueError(
                "Original Strategy requires a local World Monitor; hosted fallback is disabled"
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("World Monitor URL must not contain credentials or query parameters")
        return value.rstrip("/")

    @field_validator("llm_base_url", "solana_rpc_url", "market_data_feed_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        if not value:
            return value
        p = urlsplit(value)
        if (
            p.scheme not in {"http", "https"}
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
        ):
            raise ValueError("expected an explicit URL without embedded credentials or query")
        if p.scheme != "https" and p.hostname not in {"localhost", "127.0.0.1", "::1", "ollama"}:
            raise ValueError("remote providers require HTTPS")
        return value.rstrip("/")

    @model_validator(mode="after")
    def forbid_agent_overrides(self) -> Settings:
        if self.llm_provider == "llama_cpp":
            target = urlsplit(self.llm_base_url or "http://127.0.0.1:8081/v1")
            if target.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("llama_cpp is a loopback-only CPU inference service")
        if self.local_llm_model_url:
            target = urlsplit(self.local_llm_model_url)
            if (
                target.scheme != "https"
                or not target.hostname
                or target.username
                or target.password
                or target.query
                or target.fragment
            ):
                raise ValueError("model artifact URL must be HTTPS without credentials or query")
        if self.local_llm_model_sha256:
            import re

            if not re.fullmatch(r"[a-f0-9]{64}", self.local_llm_model_sha256):
                raise ValueError("model artifact requires a SHA-256 digest")
        for role in ("CHIEF", "SCAN", "VET", "BOOK", "SIZE", "FILLS", "RISK"):
            for suffix in ("MODEL", "PROVIDER", "API_KEY"):
                if os.environ.get(f"{role}_{suffix}"):
                    raise ValueError("per-agent LLM configuration is not supported")
        if (
            any("\x00" in argument for argument in self.llm_cli_command)
            or self.llm_cli_command
            and not self.llm_cli_command[0].strip()
        ):
            raise ValueError("CLI command must be a nonempty executable argv array")
        if self.llm_cli_command and self.llm_provider != "custom_cli":
            raise ValueError(
                "LLM_CLI_COMMAND is for custom_cli; native profiles retain their tool restrictions"
            )
        if (
            self.llm_provider == "custom_cli"
            and self.llm_cli_input == "text"
            and not any("{model}" in value for value in self.llm_cli_command)
        ):
            raise ValueError("text CLI commands must explicitly pass {model}")
        if self.llm_cli_api_key_env:
            import re

            name = self.llm_cli_api_key_env
            if (
                not re.fullmatch(r"[A-Z][A-Z0-9_]{1,99}", name)
                or name in {"HOME", "PATH", "SHELL", "DATABASE_URL", "API_ADMIN_TOKEN"}
                or name.startswith(
                    ("CODEX_", "OAI_", "IDENTITY_", "LD_", "DYLD_", "PYTHON", "BASH", "SSL_")
                )
            ):
                raise ValueError("invalid CLI API-key environment name")
        secret_flags = {"--api-key", "--apikey", "--token", "--access-token", "--password"}
        if any(
            argument.split("=", 1)[0].lower() in secret_flags for argument in self.llm_cli_command
        ):
            raise ValueError("CLI credentials belong in the environment, never command arguments")
        return self

    @property
    def revision(self) -> str:
        # Credentials never participate in a public fingerprint.
        body = {
            "provider": self.llm_provider,
            "model": self.llm_model,
            "base_url": self.llm_base_url,
            "policy": self.policy_version,
            "local_model_sha256": self.local_llm_model_sha256
            if self.llm_provider == "llama_cpp"
            else "",
            "market_data_feed_url": self.market_data_feed_url,
            "market_observer": {
                "enabled": self.market_observer_enabled,
                "max_assets": self.market_observer_max_assets,
            },
            "worldmonitor_base_url": self.worldmonitor_base_url,
            "local_context_size": self.local_llm_context_size
            if self.llm_provider == "llama_cpp"
            else None,
            "automation": {
                "enabled": self.autonomous_enabled,
                "collect_seconds": self.autonomous_collect_seconds,
                "probe_retry_seconds": self.autonomous_probe_retry_seconds,
                "local_seed": self.worldmonitor_seed_enabled,
            },
            "cli": {
                "executable": self.llm_cli_executable,
                "command": self.llm_cli_command,
                "input": self.llm_cli_input,
                "output": self.llm_cli_output,
                "backend": self.llm_cli_backend,
                "key_env": self.llm_cli_api_key_env,
                "max_output_bytes": self.llm_cli_max_output_bytes,
                "codex_runner": self.codex_runner,
                "codex_executable": self.codex_executable,
                "codex_image": self.codex_container_image,
            },
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def model_blockers(self) -> list[str]:
        reasons = []
        if not self.llm_model:
            reasons.append("LLM_MODEL_UNSET")
        if (
            self.llm_provider not in CLI_PROVIDER_NAMES | {"ollama", "llama_cpp"}
            and not self.llm_api_key.get_secret_value()
        ):
            reasons.append("LLM_API_KEY_UNSET")
        if self.llm_provider == "openai_compatible" and not self.llm_base_url:
            reasons.append("LLM_BASE_URL_UNSET")
        if self.llm_provider == "custom_cli" and not self.llm_cli_command:
            reasons.append("LLM_CLI_COMMAND_UNSET")
        if (
            self.llm_provider in CLI_PROVIDER_NAMES
            and self.llm_model.lower().split("/", 1)[0] == "auto"
        ):
            reasons.append("CLI_AUTOMATIC_MODEL_ROUTING_NOT_ALLOWED")
        return reasons

    def public(self) -> dict:
        return {
            "provider": self.llm_provider,
            "model": self.llm_model,
            "api_key_present": bool(self.llm_api_key.get_secret_value()),
            "authentication": "loopback_no_api_key"
            if self.llm_provider == "llama_cpp"
            else "existing_cli_login_or_environment"
            if self.llm_provider in CLI_PROVIDER_NAMES
            else "environment",
            "revision": self.revision,
            "paper_only": True,
            "blockers": self.model_blockers(),
            "cli": self.llm_provider in CLI_PROVIDER_NAMES,
            "autonomous_enabled": self.autonomous_enabled,
            "funds": "paper_money_only",
            "paper_initial_cash": str(self.paper_initial_cash),
        }
