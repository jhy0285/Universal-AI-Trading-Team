from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Probability = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Role = Literal["CHIEF", "SCAN", "VET", "BOOK", "SIZE", "FILLS", "RISK"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def stable_hash(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Level(StrictModel):
    price: Positive
    quantity: Positive


class ExecutionQuote(StrictModel):
    asset_id: str
    side: Literal["buy", "sell"]
    reference_price: Positive
    levels: list[Level] = Field(min_length=1, max_length=1000)
    observed_at: AwareDatetime
    source: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    provenance: Literal["live", "recorded", "fixture"] = "live"

    @model_validator(mode="after")
    def sorted_levels(self) -> ExecutionQuote:
        prices = [x.price for x in self.levels]
        if prices != sorted(prices, reverse=self.side == "sell"):
            raise ValueError("quote levels must follow executable price priority")
        return self

    @property
    def digest(self) -> str:
        return stable_hash(self)


class TransactionWindow(StrictModel):
    start: AwareDatetime
    end: AwareDatetime
    buys: int = Field(ge=0)
    sells: int = Field(ge=0)

    @model_validator(mode="after")
    def times(self) -> TransactionWindow:
        if self.start >= self.end:
            raise ValueError("invalid transaction window")
        return self


class MarketSnapshot(StrictModel):
    asset_id: str
    symbol: str
    chain: str
    pair_id: str
    source: str
    source_reference: str
    fetched_at: AwareDatetime
    observed_at: AwareDatetime | None
    provenance: Literal["live", "recorded", "fixture"] = "live"
    price: Positive | None = None
    liquidity_usd: Nonnegative | None = None
    previous_liquidity_usd: Nonnegative | None = None
    volume_h6: Nonnegative | None = None
    volume_h24: Nonnegative | None = None
    market_cap: Nonnegative | None = None
    pair_created_at: AwareDatetime | None = None
    price_change_h1_percent: Decimal | None = None
    holder_count: int | None = Field(default=None, ge=0)
    previous_holder_count: int | None = Field(default=None, ge=0)
    holder_observed_at: AwareDatetime | None = None
    previous_holder_observed_at: AwareDatetime | None = None
    previous_price: Positive | None = None
    largest_token_account_share: Probability | None = None
    onchain_verified: bool = False
    onchain_observed_at: AwareDatetime | None = None
    contract_safe: bool | None = None
    contract_source: str | None = None
    news_source_ids: list[str] = Field(default_factory=list)
    windows: list[TransactionWindow] = Field(default_factory=list, max_length=2)
    buy_quote: ExecutionQuote | None = None
    sell_quote: ExecutionQuote | None = None
    large_holder_selling: bool | None = None

    @model_validator(mode="after")
    def alignment(self) -> MarketSnapshot:
        for quote, side in ((self.buy_quote, "buy"), (self.sell_quote, "sell")):
            if quote and (
                quote.asset_id != self.asset_id
                or quote.side != side
                or quote.provenance != self.provenance
            ):
                raise ValueError("execution quote does not match asset, side or provenance")
        if len(self.windows) == 2 and self.windows[0].end > self.windows[1].start:
            raise ValueError("transaction windows must not overlap")
        if (
            self.holder_observed_at is not None
            and self.previous_holder_observed_at is not None
            and self.previous_holder_observed_at >= self.holder_observed_at
        ):
            raise ValueError("holder observations must be ordered")
        return self

    @property
    def digest(self) -> str:
        return stable_hash(self)

    def fresh(self, now: datetime, max_age: int = 300) -> bool:
        if self.observed_at is None:
            return False
        return (
            0 <= (now - self.observed_at).total_seconds() <= max_age
            and 0 <= (now - self.fetched_at).total_seconds() <= max_age
        )


class ScanOutput(StrictModel):
    candidate_ids: list[str] = Field(max_length=10)
    evidence: list[str]
    explanation: str


class VetOutput(StrictModel):
    verdict: Literal["PASS", "PASS_PARTIAL", "REJECT"]
    signal_priced_in: bool
    uses_world_news: bool
    win_probability: Probability | None
    win_payoff_ratio: Positive | None
    expected_return_fraction: Decimal | None
    evidence: list[str]
    explanation: str


class BookOutput(StrictModel):
    probability_yes: Probability | None
    abstain: bool
    evidence: list[str]
    explanation: str

    @model_validator(mode="after")
    def probability_required(self) -> BookOutput:
        if not self.abstain and self.probability_yes is None:
            raise ValueError("non-abstaining BOOK requires a probability")
        return self


class ChiefOutput(StrictModel):
    summary: str
    evidence: list[str]


class ProbeOutput(StrictModel):
    ok: Literal[True]


class PredictionQuestion(StrictModel):
    question_id: str
    title: str
    resolution_rules: str
    closes_at: AwareDatetime
    evidence_observed_at: AwareDatetime
    source_ids: list[str]
    evidence_text: list[str] = Field(default_factory=list, max_length=100)
    # There is deliberately no market price in this schema.


class PredictionPrice(StrictModel):
    question_id: str
    venue: Literal["kalshi", "polymarket"]
    venue_market_id: str
    outcome: Literal["yes", "no"]
    ask: Annotated[Decimal, Field(gt=0, lt=1)]
    observed_at: AwareDatetime
    matching_verified: bool
    resolution_hash: str
    quote: ExecutionQuote | None = None


class VenueTarget(StrictModel):
    venue: Literal["kalshi", "polymarket"]
    market_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,150}$")
    outcome: Literal["yes", "no"]


class PredictionResolution(StrictModel):
    asset_id: str
    payout: Probability
    fetched_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    source: Literal["kalshi_public_resolution", "polymarket_public_resolution"]
    source_reference: str = Field(min_length=1)
    provenance: Literal["live", "fixture"] = "live"

    @model_validator(mode="after")
    def binary_only(self):
        if self.payout not in {Decimal(0), Decimal(1)}:
            raise ValueError("only confirmed binary resolution is supported")
        return self

    @property
    def digest(self) -> str:
        return stable_hash(self)


class GateResult(StrictModel):
    allowed: bool
    reason: str
    details: dict = Field(default_factory=dict)
