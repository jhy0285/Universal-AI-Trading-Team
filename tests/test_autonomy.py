from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from trading_team import autonomy
from trading_team.data import DataError, MarketDataFeed, PublicDataClient
from trading_team.domain import ProbeOutput, utcnow
from trading_team.engine import Desk
from trading_team.llm import HTTPProvider, LLMError, LLMRuntime, Reply
from trading_team.operations import LeaseManager
from trading_team.settings import Settings
from trading_team.storage import Account, AutomationJob, CapabilityRow, ModelUsage
from trading_team.worker import build_scheduler


class ExampleProvider:
    async def generate(self, messages, schema):
        request = json.loads(messages[-1]["content"])
        return Reply(schema.model_validate(request["example"]))

    async def close(self):
        pass


async def test_model_probe_is_automatic_then_reused(db, settings):
    desk = Desk(settings, db, runtime=LLMRuntime(settings, ExampleProvider()))
    try:
        assert (await autonomy.ensure_model(desk))["verified"]
        with db.transaction() as session:
            assert len(list(session.scalars(select(ModelUsage)))) == 4
        assert (await autonomy.ensure_model(desk))["action"] == "reuse_current_probe"
    finally:
        await desk.close()


async def test_failed_probe_cooldown_survives_worker_restart(db, settings, monkeypatch):
    desk = Desk(settings, db)
    with db.transaction() as session:
        session.add(
            CapabilityRow(
                revision=settings.revision,
                verified=False,
                tested_at=utcnow(),
                result='{"verified":false}',
            )
        )

    async def forbidden():
        raise AssertionError("A denied model must not be hammered on each scheduler tick")

    monkeypatch.setattr(desk, "probe", forbidden)
    try:
        assert (await autonomy.ensure_model(desk))["reason"] == "MODEL_PROBE_RETRY_COOLDOWN"
    finally:
        await desk.close()


async def test_autonomous_collection_and_risk_do_not_wait_for_model(db, settings, monkeypatch):
    settings = settings.model_copy(update={"autonomous_enabled": True})
    desk = Desk(settings, db)

    calls = []

    async def collected(**kwargs):
        calls.append(kwargs)
        return {"count": 3, "errors": [], "snapshots": []}

    monkeypatch.setattr(desk, "collect", collected)
    scheduler = build_scheduler(desk, LeaseManager(db))
    jobs = {job.id: job for job in scheduler.get_jobs()}
    try:
        assert jobs["market-collect"].trigger.interval.total_seconds() == 60
        assert jobs["scan"].trigger.interval.total_seconds() == 900
        assert jobs["risk"].trigger.interval.total_seconds() == 300
        async with desk.runtime.lock:
            await asyncio.wait_for(jobs["market-collect"].func(*jobs["market-collect"].args), 2)
            await asyncio.wait_for(jobs["risk"].func(*jobs["risk"].args), 2)
        with db.transaction() as session:
            job = session.get(AutomationJob, "market-collect")
            assert job.state == "completed" and json.loads(job.result)["count"] == 3
        # The minute collection leaves trade windows to SCAN time.
        assert calls == [{"trades": False}]
    finally:
        await desk.close()


async def test_automation_status_requires_current_worker_and_preserves_funds(db, settings):
    settings = settings.model_copy(update={"autonomous_enabled": True})
    desk = Desk(settings, db)
    leases = LeaseManager(db)
    try:
        assert not autonomy.status(desk)["worker_running"]
        assert leases.acquire("worker:main", "test-owner", utcnow())
        assert not autonomy.status(desk)["worker_running"]
        autonomy.job_started(desk, "worker:main")
        value = autonomy.status(desk)
        assert value["worker_running"] and value["state"] == "awaiting_model"
        assert value["human_trade_approval_required"] is False
        assert value["paper_only"] and value["funds"] == "paper_money_only"
        with db.transaction() as session:
            assert session.get(Account, "main").cash == 1000
        changed = Desk(settings.model_copy(update={"llm_model": "changed"}), db)
        try:
            assert not autonomy.status(changed)["worker_configuration_current"]
        finally:
            await changed.close()
        autonomy.job_finished(desk, "worker:main", "stopped", {})
        assert not autonomy.status(desk)["worker_running"]
    finally:
        leases.release("worker:main", "test-owner")
        await desk.close()


async def test_cancellation_does_not_halt_trading_desk(db, settings):
    class CanceledProvider(ExampleProvider):
        async def generate(self, *_args):
            raise asyncio.CancelledError

    desk = Desk(settings, db, runtime=LLMRuntime(settings, CanceledProvider()))
    try:
        for _ in range(4):
            with pytest.raises(asyncio.CancelledError):
                await desk.runtime.generate("SCAN", {}, ProbeOutput, "shutdown")
        with db.transaction() as session:
            assert not session.get(Account, "main").killed
            assert {x.status for x in session.scalars(select(ModelUsage))} == {"CANCELLED"}
    finally:
        await desk.close()


async def test_current_worker_cannot_finish_new_configuration_job(db, settings):
    first = Desk(settings, db)
    second = Desk(settings.model_copy(update={"llm_model": "new"}), db)
    try:
        autonomy.job_started(first, "job")
        autonomy.job_started(second, "job")
        autonomy.job_finished(first, "job", "completed", {"stale": True})
        with db.transaction() as session:
            assert session.get(AutomationJob, "job").state == "running"
    finally:
        await first.close()
        await second.close()


async def test_local_cpu_http_requires_no_api_key_and_preserves_model(tmp_path):
    cfg = Settings(
        _env_file=None, llm_provider="llama_cpp", llm_model="one-local-model", state_dir=tmp_path
    )
    assert "LLM_API_KEY_UNSET" not in cfg.model_blockers()

    def handler(request):
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == cfg.llm_model and "tools" not in body
        assert body["chat_template_kwargs"]["enable_thinking"] is False
        return httpx.Response(
            200,
            json={
                "model": cfg.llm_model,
                "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await HTTPProvider(cfg, client).generate([], ProbeOutput)
    assert reply.payload.ok and reply.usage["basis"] == "local_inference_usage"
    assert reply.usage["reported_models"] == [cfg.llm_model]


async def test_local_server_cannot_silently_change_model():
    cfg = Settings(_env_file=None, llm_provider="llama_cpp", llm_model="one-local-model")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "model": "other",
                    "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                },
            )
        )
    ) as client:
        with pytest.raises(LLMError, match="LOCAL_MODEL_MISMATCH"):
            await HTTPProvider(cfg, client).generate([], ProbeOutput)


@pytest.mark.parametrize("base", ["https://remote.example/v1", "http://remote.example/v1"])
def test_keyless_cpu_backend_is_loopback_only(base):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="llama_cpp", llm_model="m", llm_base_url=base)


def live_snapshot(snapshot):
    data = snapshot.model_dump(mode="json")
    data["provenance"] = "live"
    for side in ("buy_quote", "sell_quote"):
        if data.get(side):
            data[side]["provenance"] = "live"
    return data


async def test_verified_feed_binds_exact_dex_pool_and_keeps_unknown_times(settings, snapshot):
    data = live_snapshot(snapshot)
    data["observed_at"] = None
    cfg = settings.model_copy(
        update={
            "market_data_feed_url": "https://feed.example/snapshots",
            "market_data_feed_api_key": SecretStr("fake-feed-key"),
        }
    )

    def handler(request):
        assert request.headers["X-API-Key"] == "fake-feed-key"
        assert "fake-feed-key" not in str(request.url)
        return httpx.Response(200, json={"snapshots": [data]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        feed = MarketDataFeed(cfg, PublicDataClient(client=client))
        discovered = snapshot.model_copy(update={"provenance": "live"})
        result = (await feed.enrich([discovered]))[0]
        assert result.observed_at is None and not result.fresh(utcnow())
        assert result.pair_id == snapshot.pair_id


@pytest.mark.parametrize(
    "change", [{"asset_id": "solana:unknown"}, {"pair_id": "other-pool"}, {"provenance": "fixture"}]
)
async def test_feed_rejects_undiscovered_pool_or_fixture(change, settings, snapshot):
    data = live_snapshot(snapshot) | change
    cfg = settings.model_copy(update={"market_data_feed_url": "https://feed.example/snapshots"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"snapshots": [data]}))
    ) as client:
        with pytest.raises(DataError):
            await MarketDataFeed(cfg, PublicDataClient(client=client)).enrich([snapshot])


async def test_automatic_model_probe_can_refresh_after_expiry(db, settings):
    desk = Desk(settings, db, runtime=LLMRuntime(settings, ExampleProvider()))
    try:
        await autonomy.ensure_model(desk)
        with db.transaction() as session:
            session.get(CapabilityRow, settings.revision).tested_at = utcnow() - timedelta(hours=25)
        assert (await autonomy.ensure_model(desk))["verified"]
        with db.transaction() as session:
            assert len(list(session.scalars(select(ModelUsage)))) == 8
    finally:
        await desk.close()


def test_model_input_keeps_execution_depth_in_deterministic_layer(snapshot):
    from trading_team.engine import analysis_snapshot

    data = analysis_snapshot(snapshot)
    assert "buy_quote" not in data and "sell_quote" not in data
    assert data["execution_observations"]["buy"]["present"]
    # Exit depth belongs to RISK; an absent sell side must not read as missing entry data.
    assert set(data["execution_observations"]) == {"buy"}
    assert data["derived"]["holder_growth_fraction"] == "0.1000"
    assert analysis_snapshot(snapshot, scan=True)["holder_count"] == snapshot.holder_count
    assert snapshot.buy_quote.levels


async def test_daily_retention_job_is_scheduled_and_can_be_disabled(db, settings):
    from trading_team.worker import retention

    desk = Desk(settings.model_copy(update={"snapshot_retention_days": 0}), db)
    try:
        assert await retention(desk) == {"status": "disabled"}
        jobs = {job.id: job for job in build_scheduler(desk, LeaseManager(db)).get_jobs()}
        assert "retention" in jobs
        enabled = Desk(settings, db)
        result = await retention(enabled)
        await enabled.close()
        assert result["raw_responses"] == 0 and result["market"] == 0
    finally:
        await desk.close()
