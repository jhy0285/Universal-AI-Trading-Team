from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from trading_team.domain import (
    BookOutput,
    ChiefOutput,
    ExecutionQuote,
    Level,
    PredictionPrice,
    PredictionQuestion,
    ScanOutput,
    VetOutput,
    stable_hash,
)
from trading_team.engine import Desk
from trading_team.llm import Attempt, LLMRuntime, Reply
from trading_team.replay import run_replay
from trading_team.storage import AgentRecord, ModelUsage, Position


@pytest.mark.parametrize(
    ("basis", "expected"),
    [
        ("cli_usage_unknown_billing", None),
        ("subscription_usage", None),
        ("cli_api_usage", D(".00003")),
        ("provider_reported", D(".00003")),
    ],
)
async def test_cli_cost_requires_known_api_billing(db, settings, basis, expected):
    cfg = settings.model_copy(update={"input_price_per_million": 2, "output_price_per_million": 2})
    desk = Desk(cfg, db)
    try:
        desk.record_usage(
            Attempt(
                "SCAN",
                "cost-check",
                "custom_cli",
                "one-model",
                "passed",
                1,
                input_tokens=10,
                output_tokens=5,
                basis=basis,
            )
        )
        with db.transaction() as session:
            record = session.scalars(select(ModelUsage)).one()
            assert record.usage_basis == basis
            assert record.estimated_cost == expected
    finally:
        await desk.close()


class NoLLM:
    async def generate(self, *_args):
        raise AssertionError("Recorded replay/RISK must never call an LLM")

    async def close(self):
        pass


class SchemaProvider:
    def __init__(self, case, forged=False):
        self.case, self.forged, self.calls = case, forged, []

    async def generate(self, messages, schema):
        payload = json.loads(messages[-1]["content"])
        self.calls.append((schema.__name__, payload))
        if "example" in payload:
            result = schema.model_validate(payload["example"])
        elif schema is ScanOutput:
            result = (
                self.case.events[0].scan.model_copy(update={"candidate_ids": ["invented"]})
                if self.forged
                else self.case.events[0].scan
            )
        elif schema is VetOutput:
            result = next(iter(self.case.events[0].vet.values()))
        elif schema is BookOutput:
            result = BookOutput(
                probability_yes=".75",
                abstain=False,
                evidence=[payload["evidence_id"]],
                explanation="Mock estimate",
            )
        else:
            result = ChiefOutput(summary="Mock factual summary", evidence=[])
        return Reply(result, {"input_tokens": 10, "output_tokens": 5})

    async def close(self):
        pass


async def test_replay_two_ledgers_same_fills_and_metrics(db, settings, case):
    desk = Desk(settings, db, LLMRuntime(settings, NoLLM()))
    try:
        one = await run_replay(desk, case, "one")
        two = await run_replay(desk, case, "two")
        assert one["dataset_hash"] == two["dataset_hash"]
        assert one["performance"]["cash"] == two["performance"]["cash"]
        assert D(one["performance"]["net_pnl"]) == D("4.1")
        assert all(one["integrity"].values()) and all(two["integrity"].values())
        with pytest.raises(ValueError, match="already exists"):
            await run_replay(desk, case, "one")
    finally:
        await desk.close()


async def test_risk_unpriced_close_then_fresh_exit_even_when_killed(db, settings, case):
    desk = Desk(settings, db, LLMRuntime(settings, NoLLM()))
    event = case.events[0]
    await desk.run_cycle(
        "paper",
        "enter",
        snapshots=event.snapshots,
        recorded_scan=event.scan,
        recorded_vet=event.vet,
        now=event.at,
    )
    missing = await desk.run_risk("paper", observations={}, now=case.events[1].at)
    assert missing["results"][0]["close_requested"] and missing["results"][0]["order"] is None
    with db.transaction() as s:
        p = s.scalar(select(Position).where(Position.account_id == "paper"))
        assert p.status == "close_requested_unpriced" and p.last_price is None
    desk.broker.kill("paper", True, "halt entries only")
    closing = case.events[1]
    result = await desk.run_risk(
        "paper", observations={x.asset_id: x for x in closing.snapshots}, now=closing.at
    )
    assert result["results"][0]["order"]["status"] == "filled"
    await desk.close()


async def test_model_probe_required_and_cycle_idempotent(db, settings, case):
    provider = SchemaProvider(case)
    desk = Desk(settings, db, LLMRuntime(settings, provider))
    first = await desk.run_cycle("main", "same")
    assert first["reason"] == "MODEL_PROBE_REQUIRED" and not provider.calls
    second = await desk.run_cycle("main", "same")
    assert second["idempotent_replay"]
    assert (await desk.probe())["verified"]
    await desk.close()


async def test_forged_candidates_cannot_order(db, settings, case):
    provider = SchemaProvider(case, forged=True)
    desk = Desk(settings, db, LLMRuntime(settings, provider))
    await desk.probe()
    event = case.events[0]
    result = await desk.run_cycle("paper", "forgery", snapshots=event.snapshots, now=event.at)
    assert result["reason"] == "SCAN_UNKNOWN_OR_DUPLICATE_EVIDENCE" and not result["orders"]
    await desk.close()


async def test_recorded_output_cannot_drive_main_account(db, settings, case):
    desk = Desk(settings, db, LLMRuntime(settings, NoLLM()))
    with pytest.raises(ValueError, match="recorded responses"):
        await desk.run_cycle(
            "main",
            "wrong",
            snapshots=case.events[0].snapshots,
            recorded_scan=case.events[0].scan,
            now=case.events[0].at,
        )
    await desk.close()


async def test_changed_global_model_fences_old_process(db, settings, case):
    first = Desk(settings, db, LLMRuntime(settings, SchemaProvider(case)))
    first.activate_config()
    changed = settings.model_copy(update={"llm_model": "second-model"})
    second = Desk(changed, db, LLMRuntime(changed, SchemaProvider(case)))
    second.activate_config()
    result = await first.probe()
    assert not result["verified"]
    assert "GLOBAL_CONFIGURATION_CHANGED" in result["roles"]["SCAN"]
    assert (await second.probe())["verified"]
    await first.close()
    await second.close()


@pytest.mark.parametrize("ready", [True, False])
async def test_book_commits_probability_before_prices_or_stops_degraded(
    db, settings, case, monkeypatch, ready
):
    provider = SchemaProvider(case)
    desk = Desk(settings, db, LLMRuntime(settings, provider))
    await desk.probe()

    async def readiness():
        return {"status": "READY" if ready else "degraded"}

    monkeypatch.setattr(desk.wm, "readiness", readiness)
    at = case.events[0].at
    question = PredictionQuestion(
        question_id="q",
        title="Will the fixture happen?",
        resolution_rules="Resolve according to fixture event",
        closes_at=at + timedelta(days=1),
        evidence_observed_at=at,
        source_ids=["fixture-source"],
        evidence_text=["Fixture evidence only"],
    )
    price_calls = []

    async def prices():
        with db.transaction() as s:
            saved = [
                x
                for x in s.scalars(select(AgentRecord).where(AgentRecord.role == "BOOK"))
                if x.status == "passed"
            ]
            assert saved and D(json.loads(saved[-1].payload)["probability_yes"]) == D(".75")
        sent = next(x[1] for x in provider.calls if x[0] == "BookOutput" and "question" in x[1])
        assert "ask" not in sent["question"] and "yesPrice" not in sent["question"]
        price_calls.append(True)
        quote = ExecutionQuote(
            asset_id="prediction:polymarket:123:yes",
            side="buy",
            reference_price=".5",
            levels=[Level(price=".5", quantity="200")],
            observed_at=at,
            source="mock-live-adapter",
            source_reference="123",
        )
        return [
            PredictionPrice(
                question_id="q",
                venue="polymarket",
                venue_market_id="123",
                outcome="yes",
                ask=".5",
                observed_at=at,
                matching_verified=True,
                resolution_hash=stable_hash(question.resolution_rules),
                quote=quote,
            )
        ]

    result = await desk.run_book("main", "book", question, prices, now=at)
    if ready:
        assert price_calls and result["orders"][0]["status"] == "filled"
    else:
        assert not price_calls and result["reason"] == "WORLD_MONITOR_DEGRADED"
    await desk.close()
