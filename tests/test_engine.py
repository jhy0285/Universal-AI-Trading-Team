from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
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
from trading_team.llm import Attempt, HTTPProvider, LLMError, LLMRuntime, Reply
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
        elif issubclass(schema, ScanOutput):
            result = (
                self.case.events[0].scan.model_copy(update={"candidate_ids": ["invented"]})
                if self.forged
                else self.case.events[0].scan
            )
        elif issubclass(schema, VetOutput):
            result = next(iter(self.case.events[0].vet.values()))
        elif issubclass(schema, BookOutput):
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
    # The per-call schema only admits supplied IDs, so the invented one fails validation.
    assert result["reason"] == "LLM_SCHEMA_INVALID" and not result["orders"]
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


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [("cited", "ROUND_TRIP_FEES_ERASE_EDGE"), ("prose", "VET_MODEL_LLM_SCHEMA_INVALID")],
)
async def test_local_model_vet_reject_with_zero_estimates_completes(
    db, settings, case, evidence, reason
):
    # Reply recorded from Qwen3-4B Q4_K_M on llama.cpp b11790 for this demo case. The grammar
    # does not enforce exclusive bounds, so 0 used to fail the whole cycle as LLM_SCHEMA_INVALID.
    # The model wrote prose as evidence; the per-call enum now makes a grammar cite the ID, and an
    # unconstrained provider's prose rejects only that candidate.
    vet_reply = {
        "verdict": "REJECT",
        "signal_priced_in": False,
        "uses_world_news": False,
        "win_probability": 0,
        "win_payoff_ratio": 0,
        "expected_return_fraction": 0,
        "evidence": ["holder_count: 1100", "previous_holder_count: 1000"],
        "explanation": "The evidence is incomplete and does not provide sufficient information.",
    }
    schemas = {}

    def respond(request):
        body = json.loads(request.content)
        schema = body["response_format"]["json_schema"]
        schemas[schema["name"]] = schema["schema"]
        payload = json.loads(body["messages"][-1]["content"])
        if "example" in payload:
            content = payload["example"]
        elif schema["name"] == "ScanOutput":
            content = case.events[0].scan.model_dump(mode="json")
        elif schema["name"] == "VetOutput":
            cite = [payload["evidence_id"]] if evidence == "cited" else vet_reply["evidence"]
            content = vet_reply | {"evidence": cite}
        else:
            content = {"summary": "Recorded facts only.", "evidence": []}
        return httpx.Response(
            200,
            json={
                "model": "local-model",
                "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    cfg = settings.model_copy(update={"llm_provider": "llama_cpp", "llm_model": "local-model"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        desk = Desk(cfg, db, LLMRuntime(cfg, HTTPProvider(cfg, client)))
        try:
            assert (await desk.probe())["verified"]
            result = await run_replay(desk, case, "local-model-replay", use_model=True)
        finally:
            await desk.close()
    cycle = result["results"][0]
    assert cycle["status"] == "completed" and cycle["orders"] == []
    assert cycle["decisions"] == [
        {
            "asset_id": case.events[0].snapshots[0].asset_id,
            "allowed": False,
            "reason": reason,
        }
    ]
    assert all(result["integrity"].values())
    properties = schemas["VetOutput"]["properties"]
    digest = case.events[0].snapshots[0].digest
    assert properties["evidence"]["items"]["enum"] == [digest]
    assert properties["evidence"]["minItems"] == 1
    for name in ("win_probability", "win_payoff_ratio", "expected_return_fraction"):
        assert [x["type"] for x in properties[name]["anyOf"]] == ["number", "null"]
    assert properties["win_probability"]["anyOf"][0] == {
        "type": "number",
        "minimum": 0.0,
        "maximum": 1.0,
    }


class FailingFirstVet(SchemaProvider):
    """The first candidate's VET call fails; later candidates answer normally."""

    def __init__(self, case, failures=1):
        super().__init__(case)
        self.failures = failures

    async def generate(self, messages, schema):
        payload = json.loads(messages[-1]["content"])
        if issubclass(schema, ScanOutput) and "snapshots" in payload:
            rows = payload["snapshots"]
            return Reply(
                schema(
                    candidate_ids=[x["asset_id"] for x in rows],
                    evidence=[x["evidence_id"] for x in rows],
                    explanation="both",
                ),
                {},
            )
        if issubclass(schema, VetOutput) and "evidence_id" in payload:
            if self.failures:
                self.failures -= 1
                raise LLMError("LLM_REFUSAL_OR_INCOMPLETE")
            vet = next(iter(self.case.events[0].vet.values()))
            return Reply(vet.model_copy(update={"evidence": [payload["evidence_id"]]}), {})
        return await super().generate(messages, schema)


async def test_one_candidate_model_failure_rejects_only_that_candidate(db, settings, case):
    event = case.events[0]
    first = event.snapshots[0]
    second_id = "solana:FIXTURE_SECOND_NOT_A_REAL_TOKEN"
    second = first.model_copy(
        update={
            "asset_id": second_id,
            "buy_quote": first.buy_quote.model_copy(update={"asset_id": second_id}),
        }
    )
    desk = Desk(settings, db, LLMRuntime(settings, FailingFirstVet(case)))
    await desk.probe()
    result = await desk.run_cycle("paper", "isolated", snapshots=[first, second], now=event.at)
    await desk.close()
    assert result["status"] == "completed"
    reasons = {x["asset_id"]: x["reason"] for x in result["decisions"]}
    assert reasons == {
        first.asset_id: "VET_MODEL_LLM_REFUSAL_OR_INCOMPLETE",
        second_id: "VET_PASS",
    }
    assert [x["asset_id"] for x in result["orders"]] == [second_id]


async def test_model_replays_on_test_ledgers_never_halt_the_live_desk(db, settings, case):
    from trading_team.storage import Account

    desk = Desk(settings, db, LLMRuntime(settings, FailingFirstVet(case, failures=10)))
    await desk.probe()
    for index in range(4):
        await run_replay(desk, case, f"model-replay-{index}", use_model=True)
    await desk.close()
    with db.transaction() as s:
        assert not s.get(Account, "main").killed
