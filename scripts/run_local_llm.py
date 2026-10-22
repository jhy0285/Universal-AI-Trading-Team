#!/usr/bin/env python3
"""Run the one configured GGUF on CPU, bound to loopback, with tools disabled."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from trading_team.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def main():
    os.chdir(ROOT)
    cfg = Settings()
    if cfg.llm_provider != "llama_cpp" or not cfg.llm_model:
        raise SystemExit("Select llama_cpp and set the global LLM_MODEL first")
    target = urlsplit(cfg.llm_base_url or "http://127.0.0.1:8081/v1")
    if target.scheme != "http" or target.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Managed CPU inference binds to local HTTP only")
    service = ROOT / ".services/local-llm"
    pin = json.loads((ROOT / "tooling/local-llm/runtime.json").read_text())
    executable = service / pin["version"] / "llama-server"
    model = service / "models/model.gguf"
    installed = (
        json.loads((service / "install.json").read_text())
        if (service / "install.json").exists()
        else {}
    )
    if (
        not executable.is_file()
        or not model.is_file()
        or installed.get("model_sha256") != cfg.local_llm_model_sha256
    ):
        raise SystemExit("Run scripts/setup_local_llm.py for the selected, verified model first")
    # Avoid inheriting llama-specific tool/router controls from an interactive shell.
    child_env = {
        name: value for name, value in os.environ.items() if not name.startswith("LLAMA_ARG_")
    }
    for name in ("API_ADMIN_TOKEN", "DATABASE_URL", "POSTGRES_PASSWORD", "LLM_API_KEY"):
        child_env.pop(name, None)
    command = [
        str(executable),
        "--model",
        str(model),
        "--alias",
        cfg.llm_model,
        "--host",
        "::1" if target.hostname == "::1" else "127.0.0.1",
        "--port",
        str(target.port or 8081),
        "--threads",
        str(cfg.local_llm_threads),
        "--threads-batch",
        str(cfg.local_llm_threads),
        "--ctx-size",
        str(cfg.local_llm_context_size),
        "--parallel",
        "1",
        "--n-gpu-layers",
        "0",
        "--offline",
        "--no-agent",
        "--mcp-servers-json",
        '{"mcpServers":{}}',
        "--no-ui-mcp-proxy",
        "--no-webui",
        "--no-context-shift",
        "--jinja",
        "--reasoning",
        "off",
        "--cors-origins",
        "localhost",
        "--metrics",
    ]
    os.execve(executable, command, child_env)


if __name__ == "__main__":
    main()
