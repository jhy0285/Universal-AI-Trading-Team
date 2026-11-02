#!/usr/bin/env python3
"""Run the exact command the configured CLI provider builds, and show how it ends.

The provider hides the child's stderr on purpose, so a CLI that hangs or fails only reports
CLI_TIMEOUT or CLI_EXEC_FAILED. This sends the same argv, stdin and environment for the SCAN
schema probe and prints the exit status plus the first bytes of stdout and stderr.
It prints environment variable names, never their values.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

from trading_team.cli_providers import CLIProvider
from trading_team.cli_registry import CLI_PROVIDER_NAMES
from trading_team.domain import ScanOutput
from trading_team.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=60, help="seconds before giving up")
    parser.add_argument("--show", type=int, default=400, help="bytes of each stream to print")
    args = parser.parse_args()
    cfg = Settings()
    if cfg.llm_provider not in CLI_PROVIDER_NAMES or cfg.llm_provider == "codex_cli":
        raise SystemExit("Select a CLI provider other than codex_cli (LLM_PROVIDER) first")
    example = {"candidate_ids": [], "evidence": [], "explanation": "probe"}
    messages = [
        {"role": "system", "content": "You are a constrained analyst. Your role is SCAN."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": "Connection/schema probe. Return the supplied example verbatim.",
                    "example": example,
                }
            ),
        },
    ]
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="debug-cli-", dir=cfg.state_dir.resolve()) as temp:
        argv, stdin, env = CLIProvider(cfg).build(Path(temp), messages, ScanOutput)
        print("provider:", cfg.llm_provider, "| model:", cfg.llm_model)
        print("flags   :", [a for a in argv if a.startswith("--")])
        print("env keys:", sorted(env))
        if cfg.llm_base_url:
            print(
                "WARNING : LLM_BASE_URL is set. A CLI provider may send its requests there "
                "(claude_cli exports it as ANTHROPIC_BASE_URL). Unset it unless that is intended."
            )
        started = time.monotonic()
        try:
            done = subprocess.run(
                argv, input=stdin, env=env, cwd=temp, capture_output=True, timeout=args.timeout
            )
        except subprocess.TimeoutExpired as exc:
            print(f"TIMEOUT after {time.monotonic() - started:.0f}s with no complete answer")
            print("stdout so far:", (exc.stdout or b"")[: args.show].decode(errors="replace"))
            print("stderr so far:", (exc.stderr or b"")[: args.show].decode(errors="replace"))
            raise SystemExit(1) from None
        print(f"finished in {time.monotonic() - started:.1f}s, exit status {done.returncode}")
        print("stdout:", done.stdout[: args.show].decode(errors="replace"))
        print("stderr:", done.stderr[: args.show].decode(errors="replace"))
        raise SystemExit(0 if done.returncode == 0 else 1)


if __name__ == "__main__":
    main()
