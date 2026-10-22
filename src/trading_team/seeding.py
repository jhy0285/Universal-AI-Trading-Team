"""Run the pinned local World Monitor's public-data seeders; never create seed fixtures."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from trading_team.cli_process import run_process
from trading_team.llm_types import LLMError

SCRIPTS = (
    "seed-earthquakes.mjs",
    "seed-regulatory-actions.mjs",
    "seed-fear-greed.mjs",
    "seed-prediction-markets.mjs",
    "seed-cross-source-signals.mjs",
)


async def seed_worldmonitor(settings) -> dict:
    if not settings.worldmonitor_seed_enabled:
        return {"status": "disabled", "reason": "LOCAL_SEED_DISABLED"}
    token = settings.worldmonitor_redis_token.get_secret_value()
    node = shutil.which("node")
    root = Path(__file__).resolve().parents[2]
    upstream = root / ".services/worldmonitor"
    if not token or not node or not upstream.is_dir():
        return {"status": "degraded", "reason": "LOCAL_SEED_RUNTIME_UNAVAILABLE"}
    env = os.environ.copy()
    for key in ("API_ADMIN_TOKEN", "DATABASE_URL", "POSTGRES_PASSWORD", "LLM_API_KEY"):
        env.pop(key, None)
    env.update(
        {
            "UPSTASH_REDIS_REST_URL": "http://127.0.0.1:8079",
            "UPSTASH_REDIS_REST_TOKEN": token,
            "NODE_USE_ENV_PROXY": "1",
        }
    )
    env.setdefault(
        "NODE_EXTRA_CA_CERTS", env.get("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    )
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = env.get(key, "") + ",localhost,127.0.0.1,::1"
    results = []
    logs = root / ".state/services"
    logs.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        if not (upstream / "scripts" / name).is_file():
            results.append({"script": name, "status": "missing"})
            continue
        try:
            result = await run_process(
                [node, str(upstream / "scripts" / name)],
                stdin=b"",
                cwd=upstream,
                env=env,
                timeout=240,
                output_limit=2_000_000,
            )
            # These logs contain public-source diagnostics. Redact the local token if an upstream error echoes it.
            output = (
                (result.stdout + result.stderr)
                .decode(errors="replace")
                .replace(token, "[REDACTED]")
            )
            (logs / (name + ".log")).write_text(output)
            results.append(
                {
                    "script": name,
                    "status": "completed" if result.returncode == 0 else "degraded",
                    "exit_code": result.returncode,
                }
            )
        except LLMError as exc:
            results.append({"script": name, "status": "degraded", "reason": exc.code})
    return {
        "status": "executed",
        "scripts": results,
        "readiness": "check actual local REST data; exit code alone does not establish readiness",
    }
