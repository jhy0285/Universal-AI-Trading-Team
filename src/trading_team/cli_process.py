from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from trading_team.cli_registry import executable_ready
from trading_team.llm_types import LLMError


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


async def stop_process(process):
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.returncode is None:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        # Some native CLIs keep inherited pipes open after their launcher exits.
        # Closing the transport releases those pipes without waiting for EOF forever.
        process._transport.close()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            raise LLMError("CLI_PROCESS_CLEANUP_FAILED") from None


async def run_process(
    argv: list[str],
    *,
    stdin: bytes,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    output_limit: int,
) -> ProcessOutput:
    """Explicit argv only. Bound both pipes during reading, and reap children on every exit."""
    if not argv or not executable_ready(argv[0]):
        raise LLMError("CLI_START_FAILED")
    try:
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            ),
            timeout=min(timeout, 10),
        )
    except TimeoutError as exc:
        raise LLMError("CLI_START_TIMEOUT") from exc
    except OSError as exc:
        raise LLMError("CLI_START_FAILED") from exc
    total = 0

    async def read(stream):
        nonlocal total
        pieces = []
        while chunk := await stream.read(16384):
            total += len(chunk)
            if total > output_limit:
                raise LLMError("CLI_OUTPUT_TOO_LARGE")
            pieces.append(chunk)
        return b"".join(pieces)

    async def feed():
        try:
            process.stdin.write(stdin)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    jobs = [
        asyncio.create_task(read(process.stdout)),
        asyncio.create_task(read(process.stderr)),
        asyncio.create_task(feed()),
        asyncio.create_task(process.wait()),
    ]
    try:
        values = await asyncio.wait_for(asyncio.gather(*jobs), timeout=timeout)
        return ProcessOutput(process.returncode, values[0], values[1])
    except TimeoutError as exc:
        await stop_process(process)
        raise LLMError("CLI_TIMEOUT", retryable=True) from exc
    except BaseException:
        await stop_process(process)
        raise
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        process._transport.close()
        await asyncio.sleep(0)
