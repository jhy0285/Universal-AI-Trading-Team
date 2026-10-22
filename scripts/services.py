#!/usr/bin/env python3
"""Start/stop only this checkout's development processes, with logs and PID identity checks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

from trading_team.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".state" / "services"


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--worldmonitor", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    STATE.mkdir(parents=True, exist_ok=True)
    python = ROOT / ".venv/bin/python"
    commands = {
        "api": ([str(python), "-m", "trading_team.cli", "serve"], ROOT),
        "dashboard": (
            [
                str(python),
                "-m",
                "streamlit",
                "run",
                "src/trading_team/dashboard.py",
                "--server.address",
                "127.0.0.1",
                "--server.port",
                "8501",
                "--server.headless",
                "true",
                "--browser.gatherUsageStats",
                "false",
            ],
            ROOT,
        ),
        "worker": ([str(python), "-m", "trading_team.cli", "worker"], ROOT),
    }
    if settings.llm_provider == "llama_cpp":
        commands = {
            "local-llm": ([str(python), str(ROOT / "scripts/run_local_llm.py")], ROOT),
            **commands,
        }
    if args.worldmonitor:
        wm = ROOT / ".services/worldmonitor"
        commands["worldmonitor"] = (
            [
                str(wm / "node_modules/.bin/vite"),
                "--host",
                "127.0.0.1",
                "--port",
                "3000",
                "--strictPort",
            ],
            wm,
        )
        if settings.worldmonitor_seed_enabled:
            runtime = ROOT / ".services/worldmonitor-runtime"
            commands = {
                "worldmonitor-redis": (
                    [
                        str(runtime / "bin/redis-server"),
                        str(ROOT / ".state/worldmonitor/redis.conf"),
                    ],
                    ROOT,
                ),
                "worldmonitor-redis-rest": (
                    [shutil.which("node") or "node", str(runtime / "redis-rest-proxy.mjs")],
                    ROOT,
                ),
                "worldmonitor": commands.pop("worldmonitor"),
                **commands,
            }
    if args.action in {"stop", "status"}:
        for item in STATE.glob("*.json"):
            commands.setdefault(item.stem, ([], ROOT))
    items = list(commands.items())
    if args.action == "stop":
        priorities = {
            "worker": 0,
            "dashboard": 1,
            "api": 2,
            "worldmonitor": 3,
            "worldmonitor-redis-rest": 4,
            "worldmonitor-redis": 5,
            "local-llm": 6,
        }
        items.sort(key=lambda item: priorities.get(item[0], 100))
    for name, (command, cwd) in items:
        record = STATE / (name + ".json")
        saved = json.loads(record.read_text()) if record.exists() else None
        running = saved is not None and identity(saved["pid"]) == saved["identity"]
        if args.action == "start" and not running:
            if not cwd.exists() or not Path(command[0]).exists():
                raise SystemExit(f"{name}: install dependencies first")
            with (STATE / (name + ".log")).open("ab") as log:
                process_env = os.environ.copy()
                if name == "worldmonitor":
                    process_env["BROWSER"] = "none"
                if (
                    name in {"worldmonitor", "worldmonitor-redis-rest"}
                    and settings.worldmonitor_seed_enabled
                ):
                    token = settings.worldmonitor_redis_token.get_secret_value()
                    process_env.update(
                        {
                            "UPSTASH_REDIS_REST_URL": "http://127.0.0.1:8079",
                            "UPSTASH_REDIS_REST_TOKEN": token,
                            "SRH_TOKEN": token,
                            "REDIS_URL": "redis://:" + token + "@127.0.0.1:16379",
                            "PORT": "8079",
                            "NODE_USE_ENV_PROXY": "1",
                        }
                    )
                    process_env.setdefault(
                        "NODE_EXTRA_CA_CERTS",
                        process_env.get("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt"),
                    )
                    for key in ("NO_PROXY", "no_proxy"):
                        process_env[key] = process_env.get(key, "") + ",localhost,127.0.0.1,::1"
                child = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=process_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            record.write_text(json.dumps({"pid": child.pid, "identity": identity(child.pid)}))
            time.sleep(0.2)
            running = child.poll() is None
            if not running:
                raise SystemExit(f"{name}: startup failed; inspect .state/services/{name}.log")
            if name == "local-llm" and running:
                target = urlsplit(settings.llm_base_url or "http://127.0.0.1:8081/v1")
                deadline = time.monotonic() + 45
                healthy = False
                while time.monotonic() < deadline and child.poll() is None:
                    connection = HTTPConnection(target.hostname, target.port or 8081, timeout=1)
                    try:
                        connection.request("GET", "/health")
                        healthy = connection.getresponse().status == 200
                        if healthy:
                            break
                    except OSError:
                        pass
                    finally:
                        connection.close()
                    time.sleep(0.5)
                if not healthy:
                    raise SystemExit("local-llm: not ready; inspect .state/services/local-llm.log")
        elif args.action == "stop" and running:
            os.killpg(saved["pid"], signal.SIGTERM)
            for _ in range(30):
                if identity(saved["pid"]) != saved["identity"]:
                    break
                time.sleep(0.1)
            running = identity(saved["pid"]) == saved["identity"]
            if not running:
                record.unlink(missing_ok=True)
        print(f"{name}: {'running' if running else 'stopped'}")


if __name__ == "__main__":
    main()
