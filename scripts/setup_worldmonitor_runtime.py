#!/usr/bin/env python3
"""Prepare a private local Redis and the pinned upstream Redis REST proxy."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path

from dotenv import set_key

from trading_team.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def main():
    os.chdir(ROOT)
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise SystemExit("This prepared runtime is for Linux x86_64")
    runtime = ROOT / ".services/worldmonitor-runtime"
    binary = runtime / "bin/redis-server"
    runtime.mkdir(parents=True, exist_ok=True)
    pin = json.loads((ROOT / "tooling/worldmonitor-runtime/redis.json").read_text())
    if not binary.exists():
        child_env = os.environ.copy()
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            child_env.pop(key, None)
        docker = ["docker", "--host=unix:///var/run/docker.sock"]
        subprocess.run(docker + ["pull", pin["image"]], env=child_env, check=True)
        container = "uat-redis-extract-" + uuid.uuid4().hex[:12]
        subprocess.run(
            docker + ["create", "--name", container, pin["image"]],
            env=child_env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            binary.parent.mkdir(exist_ok=True)
            for name in ("redis-server", "redis-cli"):
                subprocess.run(
                    docker
                    + ["cp", f"{container}:/usr/local/bin/{name}", str(binary.parent / name)],
                    env=child_env,
                    check=True,
                )
        finally:
            subprocess.run(
                docker + ["rm", container], env=child_env, check=True, stdout=subprocess.DEVNULL
            )
    subprocess.run([str(binary), "--version"], check=True)
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm is required for the local Redis REST proxy")
    subprocess.run(
        [
            npm,
            "ci",
            "--prefix",
            "tooling/worldmonitor-runtime",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--cache",
            ".cache/npm",
        ],
        check=True,
    )
    cfg = Settings()
    token = cfg.worldmonitor_redis_token.get_secret_value()
    if not token:
        token = secrets.token_urlsafe(32)
        set_key(ROOT / ".env", "WORLDMONITOR_REDIS_TOKEN", token)
        (ROOT / ".env").chmod(0o600)
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
        raise SystemExit("The local Redis token must be 16-128 URL-safe characters")
    state = ROOT / ".state/worldmonitor"
    data = state / "redis"
    data.mkdir(parents=True, exist_ok=True)
    config = state / "redis.conf"
    config.write_text(
        f'bind 127.0.0.1\nport 16379\nprotected-mode yes\nrequirepass {json.dumps(token)}\nappendonly yes\nappendfsync everysec\nmaxmemory 256mb\nmaxmemory-policy noeviction\ndir {json.dumps(str(data))}\nlogfile ""\n'
    )
    config.chmod(0o600)
    source = (ROOT / ".services/worldmonitor/docker/redis-rest-proxy.mjs").read_text()
    anchor = "server.listen(PORT, '0.0.0.0'"
    if source.count(anchor) != 1:
        raise SystemExit(
            "Pinned upstream proxy binding changed; refusing an unexpected modification"
        )
    (runtime / "redis-rest-proxy.mjs").write_text(
        source.replace(anchor, "server.listen(PORT, '127.0.0.1'")
    )
    modules = runtime / "node_modules"
    if not modules.exists():
        modules.symlink_to(
            ROOT / "tooling/worldmonitor-runtime/node_modules", target_is_directory=True
        )
    (runtime / "install.json").write_text(
        json.dumps(
            {
                "redis_image": pin["image"],
                "proxy_upstream_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "local_change": "loopback binding only",
            },
            indent=2,
        )
    )
    print("Prepared private loopback Redis and the upstream REST proxy. No seed data was invented.")


if __name__ == "__main__":
    main()
