from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


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


@dataclass
class Reply:
    payload: BaseModel
    usage: dict = field(default_factory=dict)


class LLMError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class LLMProvider(Protocol):
    async def generate(self, messages: list[dict], response_schema: type[T]) -> Reply: ...
    async def close(self) -> None: ...
