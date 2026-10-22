from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from trading_team.domain import (
    ExecutionQuote,
    Level,
    MarketSnapshot,
    ScanOutput,
    StrictModel,
    TransactionWindow,
    VetOutput,
    stable_hash,
)
from trading_team.engine import Desk
from trading_team.metrics import performance
from trading_team.policy import POLICY_VERSION
from trading_team.storage import ReplayRun


class ReplayEvent(StrictModel):
    at: AwareDatetime
    kind: Literal["cycle", "risk"]
    snapshots: list[MarketSnapshot] = Field(default_factory=list, max_length=100)
    scan: ScanOutput | None = None
    vet: dict[str, VetOutput] = Field(default_factory=dict)


class ReplayCase(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    policy_version: Literal["paper-interpretation-v1"] = POLICY_VERSION
    initial_cash: Decimal = Field(default=Decimal("1000"), gt=0, le=100_000_000)
    events: list[ReplayEvent] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def verify(self):
        if [e.at for e in self.events] != sorted(e.at for e in self.events):
            raise ValueError("replay events must be chronological")
        for event in self.events:
            if any(x.provenance != "fixture" for x in event.snapshots):
                raise ValueError(
                    "explicit replay inputs must be labelled fixture; no live-data impersonation"
                )
            if event.kind == "cycle" and event.scan is None:
                raise ValueError("recorded SCAN is required for reproducible replay")
        return self

    @property
    def dataset_hash(self):
        # Model outputs are excluded from the common external-data fingerprint.
        return stable_hash(
            {
                "cash": str(self.initial_cash),
                "policy": self.policy_version,
                "events": [
                    {
                        "at": e.at.isoformat(),
                        "kind": e.kind,
                        "snapshots": [x.model_dump(mode="json") for x in e.snapshots],
                    }
                    for e in self.events
                ],
            }
        )


async def run_replay(
    desk: Desk, case: ReplayCase, account_id: str, *, use_model: bool = False
) -> dict:
    # Each run receives its own ledger. There is no reset or overwrite operation.
    desk.db.create_account(account_id, "fixture", case.initial_cash)
    with desk.db.transaction() as s:
        s.add(
            ReplayRun(
                id=account_id,
                account_id=account_id,
                dataset_hash=case.dataset_hash,
                policy_version=case.policy_version,
                initial_cash=case.initial_cash,
                provider=desk.settings.llm_provider if use_model else "recorded-fixture",
                model=desk.settings.llm_model if use_model else "fixture-not-a-live-model",
                mode="sequential_model_replay" if use_model else "recorded_response_replay",
            )
        )
    results = []
    for index, event in enumerate(case.events):
        if event.kind == "cycle":
            result = await desk.run_cycle(
                account_id,
                f"replay:{index}",
                snapshots=event.snapshots,
                recorded_scan=None if use_model else event.scan,
                recorded_vet=None if use_model else event.vet,
                now=event.at,
            )
        else:
            result = await desk.run_risk(
                account_id, observations={x.asset_id: x for x in event.snapshots}, now=event.at
            )
        results.append(result)
    return {
        "account_id": account_id,
        "provenance": "fixture",
        "mode": "model" if use_model else "recorded",
        "dataset_hash": case.dataset_hash,
        "results": results,
        "performance": performance(desk.db, account_id, now=case.events[-1].at),
        "integrity": desk.broker.integrity(account_id),
    }


def demo_case() -> ReplayCase:
    """Synthetic evidence for an isolated test account. Never used as a live-data fallback."""
    at = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    asset = "solana:FIXTURE_ONLY_NOT_A_REAL_TOKEN"
    buy = ExecutionQuote(
        asset_id=asset,
        side="buy",
        reference_price="100",
        levels=[Level(price="100", quantity="1")],
        observed_at=at,
        source="fixture",
        source_reference="fixture:buy-depth",
        provenance="fixture",
    )
    snapshot = MarketSnapshot(
        asset_id=asset,
        symbol="FIXTURE",
        chain="solana",
        pair_id="fixture-pool",
        source="fixture",
        source_reference="fixture:complete-market-evidence",
        fetched_at=at,
        observed_at=at,
        provenance="fixture",
        price="100",
        previous_price="98",
        liquidity_usd="20000",
        previous_liquidity_usd="19000",
        volume_h6="500",
        volume_h24="2000",
        market_cap="1000000",
        pair_created_at=at - timedelta(days=1),
        price_change_h1_percent="1",
        holder_count=1100,
        previous_holder_count=1000,
        holder_observed_at=at,
        previous_holder_observed_at=at - timedelta(minutes=15),
        largest_token_account_share="0.04",
        onchain_verified=True,
        onchain_observed_at=at,
        contract_safe=True,
        contract_source="fixture:contract-inspection",
        windows=[
            TransactionWindow(
                start=at - timedelta(minutes=10), end=at - timedelta(minutes=5), buys=20, sells=10
            ),
            TransactionWindow(start=at - timedelta(minutes=5), end=at, buys=20, sells=10),
        ],
        buy_quote=buy,
        large_holder_selling=False,
    )
    vet = VetOutput(
        verdict="PASS",
        signal_priced_in=False,
        uses_world_news=False,
        win_probability="0.7",
        win_payoff_ratio="2",
        expected_return_fraction="0.1",
        evidence=[snapshot.digest],
        explanation="Recorded synthetic VET; no live inference.",
    )
    close_at = at + timedelta(minutes=5)
    sell = ExecutionQuote(
        asset_id=asset,
        side="sell",
        reference_price="110",
        levels=[Level(price="110", quantity="1")],
        observed_at=close_at,
        source="fixture",
        source_reference="fixture:sell-depth",
        provenance="fixture",
    )
    closing = snapshot.model_copy(
        update={
            "price": Decimal("110"),
            "volume_h6": Decimal("10"),
            "volume_h24": Decimal("1000"),
            "observed_at": close_at,
            "fetched_at": close_at,
            "buy_quote": None,
            "sell_quote": sell,
        }
    )
    return ReplayCase(
        name="isolated-paper-smoke",
        events=[
            ReplayEvent(
                at=at,
                kind="cycle",
                snapshots=[snapshot],
                scan=ScanOutput(
                    candidate_ids=[asset],
                    evidence=[snapshot.digest],
                    explanation="Recorded synthetic ranking.",
                ),
                vet={asset: vet},
            ),
            ReplayEvent(at=close_at, kind="risk", snapshots=[closing]),
        ],
    )
