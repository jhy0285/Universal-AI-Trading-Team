from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def _safe(part: object) -> str:
    # A field name can come from the model's own output (an unexpected key), so it is not trusted.
    return re.sub(r"[^A-Za-z0-9_]", "?", str(part))[:40]


def schema_detail(exc: BaseException) -> str:
    """Say which fields failed validation and why, without echoing any model output."""
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        return ",".join(
            f"{'.'.join(_safe(part) for part in item['loc']) or '<root>'}:{item['type']}"
            for item in errors[:5]
        )
    return type(exc).__name__


@dataclass
class Attempt:
    role: str
    cycle_id: str
    provider: str
    model: str
    status: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    basis: str = "provider_reported"
    # False for a schema failure that the runtime immediately retried with corrective feedback.
    final: bool = True


@dataclass
class Reply:
    payload: BaseModel
    usage: dict = field(default_factory=dict)


class LLMError(Exception):
    def __init__(self, code: str, retryable: bool = False, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        # Field paths and error types only; never model output or provider response bodies.
        self.detail = detail


class LLMProvider(Protocol):
    async def generate(self, messages: list[dict], response_schema: type[T]) -> Reply: ...
    async def close(self) -> None: ...
