#!/usr/bin/env python3
"""Install a pinned CPU runtime and the environment-selected, checksum-verified GGUF."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tarfile
from pathlib import Path

import httpx

from trading_team.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download(url: str, path: Path, expected: str, limit: int):
    if path.is_file() and digest(path) == expected:
        print(f"Verified existing {path.name}", flush=True)
        return
    temporary = path.with_suffix(path.suffix + ".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    checksum, total, checkpoint = hashlib.sha256(), 0, 0
    try:
        with httpx.Client(
            timeout=httpx.Timeout(60, read=120), follow_redirects=True, trust_env=True
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise SystemExit(f"Artifact download failed: HTTP {response.status_code}")
                if response.url.scheme != "https":
                    raise SystemExit("Artifact redirect requires HTTPS")
                with temporary.open("wb") as target:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise SystemExit("Artifact exceeds size limit")
                        checksum.update(chunk)
                        target.write(chunk)
                        if total - checkpoint >= 256 * 1024 * 1024:
                            print(
                                f"{path.name}: {total // (1024 * 1024)} MiB downloaded", flush=True
                            )
                            checkpoint = total
        if checksum.hexdigest() != expected:
            raise SystemExit("Artifact SHA-256 mismatch; refusing installation")
        os.replace(temporary, path)
        print(f"Verified SHA-256: {path.name}", flush=True)
    except httpx.HTTPError:
        raise SystemExit(
            "Artifact transport failed; proxy and TLS verification were preserved"
        ) from None
    finally:
        temporary.unlink(missing_ok=True)


def main():
    os.chdir(ROOT)
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise SystemExit(
            "Pinned runtime supports Linux x86_64; use your own compatible local service elsewhere"
        )
    cfg = Settings()
    if not cfg.local_llm_model_url or not cfg.local_llm_model_sha256:
        raise SystemExit(
            "Set LOCAL_LLM_MODEL_URL and LOCAL_LLM_MODEL_SHA256 in the environment first"
        )
    service = ROOT / ".services/local-llm"
    model = service / "models/model.gguf"
    if not model.exists() and shutil.disk_usage(ROOT).free < 3_500_000_000:
        raise SystemExit("At least 3.5 GB of free disk space is required")
    pin = json.loads((ROOT / "tooling/local-llm/runtime.json").read_text())
    archive = ROOT / ".cache/local-llm" / (pin["version"] + ".tar.gz")
    download(pin["url"], archive, pin["sha256"], pin["size"])
    runtime = service / pin["version"]
    if not (runtime / "llama-server").exists():
        temporary = service / (pin["version"] + ".installing")
        temporary.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary, filter="data")
        candidates = list(temporary.rglob("llama-server"))
        if len(candidates) != 1:
            raise SystemExit("Runtime archive has an unexpected layout")
        source = candidates[0].parent
        if source != temporary:
            os.replace(source, runtime)
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, runtime)
    download(cfg.local_llm_model_url, model, cfg.local_llm_model_sha256, 5_000_000_000)
    with model.open("rb") as source:
        if source.read(4) != b"GGUF":
            raise SystemExit("Model artifact is not GGUF")
    (service / "install.json").write_text(
        json.dumps(
            {
                "runtime": pin,
                "model_url": cfg.local_llm_model_url,
                "model_sha256": cfg.local_llm_model_sha256,
            },
            indent=2,
        )
    )
    print(
        "CPU runtime and configured model installed. No API key or CLI login was changed.",
        flush=True,
    )


if __name__ == "__main__":
    main()
