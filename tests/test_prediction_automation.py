from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import select

from trading_team import autonomy
from trading_team.broker import PaperBroker
from trading_team.data import DataError, PredictionMarkets, PublicDataClient, WorldMonitor
from trading_team.domain import MarketSnapshot, PredictionResolution, VenueTarget, utcnow
from trading_team.engine import Desk
from trading_team.llm import LLMRuntime, Reply
from trading_team.operations import LeaseManager
from trading_team.storage import Account, Cycle, LedgerEntry, Position
from trading_team.worker import build_scheduler


@pytest.mark.parametrize("outcome", ["yes", "no"])
async def test_confirmed_binary_kalshi_resolution_is_not_a_live_order(settings, outcome):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "market": {
                    "ticker": "EXACT",
                    "status": "finalized",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                    "settlement_ts": utcnow().isoformat(),
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        market, resolution = await PredictionMarkets(
            PublicDataClient(client=client)
        ).risk_observation("prediction:kalshi:EXACT:" + outcome)
    assert market is None and resolution.payout == (1 if outcome == "yes" else 0)
    assert all(call.method == "GET" for call in calls)


async def test_pending_or_canceled_market_does_not_create_fake_settlement():
    def handler(request):
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": [[".5", "100"]]}})
        return httpx.Response(
            200, json={"market": {"ticker": "EXACT", "status": "closed", "result": "yes"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observed, resolved = await PredictionMarkets(
            PublicDataClient(client=client)
        ).risk_observation("prediction:kalshi:EXACT:yes")
        assert observed is None and resolved is None


@pytest.mark.parametrize(
    "change",
    [
        {"settlement_value_dollars": "0"},
        {"settlement_ts": None},
        {"result": "scalar"},
        {"settlement_ts": (utcnow() + timedelta(days=1)).isoformat()},
    ],
)
async def test_finalized_market_requires_matching_binary_value_and_time(change):
    value = {
        "ticker": "EXACT",
        "status": "finalized",
        "result": "yes",
        "settlement_value_dollars": "1",
        "settlement_ts": utcnow().isoformat(),
    } | change
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"market": value}))
    ) as client:
        observed, resolved = await PredictionMarkets(
            PublicDataClient(client=client)
        ).risk_observation("prediction:kalshi:EXACT:yes")
    assert observed is None and resolved is None


def test_original_scan_remains_solana_only(snapshot):
    from trading_team.policy import scan_gate

    assert (
        scan_gate(snapshot.model_copy(update={"chain": "other-chain"}), snapshot.fetched_at).reason
        == "ORIGINAL_SOLANA_ONLY"
    )


@pytest.mark.parametrize("resolved", [True, False])
async def test_polymarket_requires_resolved_status_and_exact_binary_prices(resolved):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "123",
                "closed": True,
                "umaResolutionStatus": "resolved" if resolved else "disputed",
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["1","0"]',
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = PredictionMarkets(PublicDataClient(client=client))
        if resolved:
            _, resolution = await adapter.risk_observation("prediction:polymarket:123:no")
            assert resolution.payout == 0
        else:
            with pytest.raises(DataError):
                await adapter.risk_observation("prediction:polymarket:123:no")


@pytest.mark.parametrize("payout", [D(0), D(1)])
def test_paper_binary_settlement_is_atomic_idempotent_and_closes_episode(db, snapshot, payout):
    broker = PaperBroker(db)
    data = snapshot.model_dump(mode="json")
    asset = "prediction:kalshi:EXACT:yes"
    data.update(asset_id=asset, chain="prediction", pair_id="EXACT")
    for key in ("buy_quote", "sell_quote"):
        if data.get(key):
            data[key]["asset_id"] = asset
    sample = MarketSnapshot.model_validate(data)
    initial = broker.execute(
        order_id="prediction-entry",
        cycle_id="fixture",
        account_id="paper",
        snapshot=sample,
        quote=sample.buy_quote,
        side="buy",
        requested=D(60),
        now=sample.fetched_at,
        market_kind="prediction",
    )
    assert initial["status"] == "filled"
    resolution = PredictionResolution(
        asset_id=asset,
        payout=payout,
        fetched_at=sample.fetched_at,
        source="kalshi_public_resolution",
        source_reference="EXACT",
        provenance="fixture",
    )
    order = broker.settle_prediction("paper", resolution, sample.fetched_at)
    assert order["reason"] == "PAPER_BINARY_RESOLUTION" and D(order["fee"]) == 0
    expected = (
        D(1000) - D(initial["notional"]) - D(initial["fee"]) + D(initial["quantity"]) * payout
    )
    assert broker.settle_prediction("paper", resolution, sample.fetched_at) is None
    with db.transaction() as session:
        account = session.get(Account, "paper")
        assert account.cash == expected
        assert (
            session.scalars(select(Position).where(Position.account_id == "paper")).one().quantity
            == 0
        )
        assert (
            len(list(session.scalars(select(LedgerEntry).where(LedgerEntry.account_id == "paper"))))
            == 2
        )
    assert all(broker.integrity("paper").values())


def test_resolution_rejects_wrong_provenance_and_fractional_payout(db, snapshot):
    with pytest.raises(ValueError):
        PredictionResolution(
            asset_id="prediction:kalshi:X:yes",
            payout=".5",
            fetched_at=utcnow(),
            source="kalshi_public_resolution",
            source_reference="X",
        )
    with pytest.raises(ValueError, match="provenance"):
        PaperBroker(db).settle_prediction(
            "paper",
            PredictionResolution(
                asset_id="prediction:kalshi:X:yes",
                payout=1,
                fetched_at=utcnow(),
                source="kalshi_public_resolution",
                source_reference="X",
            ),
            utcnow(),
        )


async def test_prediction_canonical_metadata_is_price_blind():
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "market": {
                    "ticker": "EXACT",
                    "status": "open",
                    "title": "Will the event occur?",
                    "rules_primary": "Official canonical rules",
                    "close_time": (utcnow() + timedelta(days=1)).isoformat(),
                    "yes_ask_dollars": ".99",
                    "last_price_dollars": ".98",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        metadata, targets = await PredictionMarkets(
            PublicDataClient(client=client)
        ).question_metadata("kalshi", "EXACT")
    assert not any("price" in key or "ask" in key for key in metadata)
    assert [target.outcome for target in targets] == ["yes", "no"]


async def test_book_degraded_never_waits_for_human_or_calls_model(db, settings, monkeypatch):
    desk = Desk(settings, db)
    monkeypatch.setattr(desk, "capabilities", lambda: {"verified": True})

    async def readiness():
        return {"status": "degraded"}

    monkeypatch.setattr(desk.wm, "readiness", readiness)
    try:
        result = await autonomy.book(desk)
        assert result["reason"] == "WORLD_MONITOR_DEGRADED" and not result["orders"]
    finally:
        await desk.close()


async def test_book_assembles_recent_evidence_without_human_input(db, settings, monkeypatch):
    desk = Desk(settings, db)
    monkeypatch.setattr(desk, "capabilities", lambda: {"verified": True})

    async def readiness():
        return {"status": "READY"}

    async def discovery():
        return {
            "markets": [
                {
                    "id": "EXACT",
                    "source": "MARKET_SOURCE_KALSHI",
                    "title": "Earthquake magnitude question",
                }
            ]
        }

    async def events():
        return {
            "signals": [
                {
                    "id": "s",
                    "type": "CROSS_SOURCE_SIGNAL_TYPE_EARTHQUAKE_SIGNIFICANT",
                    "summary": "Earthquake magnitude increased in the observed region",
                    "detectedAt": int(utcnow().timestamp() * 1000),
                }
            ]
        }

    async def metadata(_venue, _id):
        return {
            "question_id": "kalshi:EXACT",
            "title": "Earthquake magnitude question",
            "resolution_rules": "Official rules",
            "closes_at": utcnow() + timedelta(days=1),
        }, [VenueTarget(venue="kalshi", market_id="EXACT", outcome="yes")]

    async def run_book(_account, _request, question, loader):
        assert question.evidence_text and question.source_ids == ["wm:signal:s"]
        assert "price" not in question.model_dump()
        return {"status": "completed", "orders": []}

    monkeypatch.setattr(desk.wm, "readiness", readiness)
    monkeypatch.setattr(desk.wm, "discovery", discovery)
    monkeypatch.setattr(desk.wm, "events", events)
    monkeypatch.setattr(desk.prediction, "question_metadata", metadata)
    monkeypatch.setattr(desk, "run_book", run_book)
    try:
        result = await autonomy.book(desk)
        assert (
            result["questions_evaluated"] == 1 and result["human_trade_approval_required"] is False
        )
    finally:
        await desk.close()


async def test_startup_probe_triggers_scan_after_verification_without_consuming_blocked_cycle(
    db, settings, monkeypatch
):
    class Provider:
        async def generate(self, messages, schema):
            return Reply(schema.model_validate(json.loads(messages[-1]["content"])["example"]))

        async def close(self):
            pass

    settings = settings.model_copy(update={"autonomous_enabled": True})
    desk = Desk(settings, db, runtime=LLMRuntime(settings, Provider()))
    scheduler = build_scheduler(desk, LeaseManager(db))
    scheduler.start(paused=True)
    calls = []

    async def cycle(account, request):
        calls.append((account, request))
        return {"status": "completed", "orders": []}

    monkeypatch.setattr(desk, "run_cycle", cycle)
    try:
        scan = scheduler.get_job("scan")
        await scan.func(*scan.args)
        assert not calls
        with db.transaction() as session:
            assert not list(session.scalars(select(Cycle)))
        probe = scheduler.get_job("model-probe")
        await probe.func(*probe.args)
        assert scheduler.get_job("scan").next_run_time <= utcnow() + timedelta(seconds=1)
        await scan.func(*scan.args)
        assert calls and settings.revision in calls[0][1]
    finally:
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        await desk.close()


async def test_disabled_automation_never_starts_scheduled_entries(db, settings, monkeypatch):
    desk = Desk(settings, db)
    scheduler = build_scheduler(desk, LeaseManager(db))

    async def forbidden(*_args):
        raise AssertionError("disabled automation must not enter a trade")

    monkeypatch.setattr(desk, "run_cycle", forbidden)
    try:
        scan = scheduler.get_job("scan")
        await scan.func(*scan.args)
    finally:
        await desk.close()


@pytest.mark.parametrize("kind", ["prediction", "signals", "fear_greed"])
async def test_stale_or_default_only_worldmonitor_data_is_not_ready(settings, kind):
    now = utcnow()
    ms = int(now.timestamp() * 1000)

    def handler(request):
        if "fear-greed" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "compositeScore": 50,
                    "unavailable": False,
                    "seededAt": now.isoformat(),
                    "sentiment": {
                        "inputsJson": "{}" if kind == "fear_greed" else '{"observed":42}'
                    },
                },
            )
        if "prediction" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "dataAvailable": True,
                    "fetchedAt": 1 if kind == "prediction" else ms,
                    "markets": [{"id": "q"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "signals": [{"id": "s", "summary": "Observed signal", "detectedAt": ms}],
                "evaluatedAt": 1 if kind == "signals" else ms,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = await WorldMonitor(settings, PublicDataClient(client=client)).readiness()
    assert value["status"] == "degraded" and not value["checks"][kind]["ready"]
