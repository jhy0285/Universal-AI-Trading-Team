from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from trading_team.api import create_app
from trading_team.domain import utcnow
from trading_team.engine import Desk
from trading_team.llm import LLMRuntime
from trading_team.operations import LeaseManager, Supervisor
from trading_team.storage import Account, Position
from trading_team.worker import build_scheduler


def test_leases_expiry_owner_and_release(db):
    lease = LeaseManager(db)
    now = utcnow()
    assert lease.acquire("job", "one", now)
    assert not lease.acquire("job", "two", now)
    lease.release("job", "two")
    assert not lease.acquire("job", "two", now)
    assert lease.acquire("job", "two", now + timedelta(seconds=46))
    lease.release("job", "one")
    assert not lease.acquire("job", "three", now + timedelta(seconds=46))


def test_supervisor_halts_entries_without_liquidating(db):
    supervisor = Supervisor(db)
    now = utcnow()
    assert not supervisor.observe("SCAN", False, "TIMEOUT", now)
    assert supervisor.observe("SCAN", False, "TIMEOUT", now)
    assert not supervisor.observe("SCAN", False, "TIMEOUT", now)
    assert not supervisor.observe("SCAN", False, "TIMEOUT", now)
    with db.transaction() as s:
        assert s.get(Account, "main").killed
        assert not list(s.scalars(select(Position)))


def test_scheduler_independent_cadences(db, settings):
    desk = Desk(settings, db)
    scheduler = build_scheduler(desk, LeaseManager(db))
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert jobs["risk"].trigger.interval.total_seconds() == 300
    assert jobs["scan"].trigger.interval.total_seconds() == 900
    assert str(jobs["daily-report"].trigger.timezone) == "Asia/Seoul"
    assert "hour='23'" in str(jobs["daily-report"].trigger)
    import asyncio

    asyncio.run(desk.close())


def test_api_authorization_paper_only_and_demo(db, settings, case):
    from pydantic import SecretStr

    settings = settings.model_copy(update={"api_admin_token": SecretStr("test-admin-token")})
    desk = Desk(settings, db)
    app = create_app(settings, desk)
    with TestClient(app) as client:
        assert client.get("/health").json()["paper_only"]
        assert client.post("/cycles/run", json={"request_id": "noauth"}).status_code == 401
        assert (
            client.post(
                "/kill",
                headers={"Authorization": "Bearer wrong"},
                json={"active": True, "reason": "test"},
            ).status_code
            == 401
        )
        headers = {"Authorization": "Bearer test-admin-token"}
        response = client.post(
            "/replay",
            headers=headers,
            json={"account_id": "api-demo", "case": case.model_dump(mode="json")},
        )
        assert response.status_code == 200 and all(response.json()["integrity"].values())
        assert client.get("/metrics?account_id=api-demo").json()["closed_trades"] == 1
        assert len(client.get("/orders?account_id=api-demo").json()) == 2
        assert (
            client.post(
                "/replay",
                headers=headers,
                json={"account_id": "api-demo", "case": case.model_dump(mode="json")},
            ).status_code
            == 422
        )
        assert client.post(
            "/kill", headers=headers, json={"active": True, "reason": "operator test"}
        ).json()["killed"]
        assert (
            client.post("/cycles/run", headers=headers, json={"request_id": "halted"}).json()[
                "reason"
            ]
            == "KILL_SWITCH_ACTIVE"
        )
        text = client.get("/config").text
        assert "test-admin-token" not in text and "SecretStr" not in text
        paths = client.get("/openapi.json").json()["paths"]
        assert not any(
            term in path
            for path in paths
            for term in ("/live", "/sign", "/withdraw", "/exchange/order")
        )
        for path in [
            "/accounts",
            "/positions",
            "/cycles",
            "/decisions",
            "/snapshots",
            "/agents",
            "/equity",
            "/events",
            "/costs",
            "/comparison",
            "/audit",
        ]:
            assert client.get(path).status_code == 200


def test_provider_inventory_is_read_only_and_contains_no_secrets(db, settings):
    from pydantic import SecretStr

    cfg = settings.model_copy(
        update={
            "llm_provider": "custom_cli",
            "llm_api_key": SecretStr("fake-global-key"),
            "api_admin_token": SecretStr("fake-admin-secret"),
            "llm_cli_command": ("private-command-path",),
        }
    )
    desk = Desk(cfg, db)
    with TestClient(create_app(cfg, desk)) as client:
        response = client.get("/providers")
        assert response.status_code == 200
        body = response.json()
        assert body["active"]["paper_only"]
        assert body["transport"]["authentication"] == "not_checked"
        assert len(body["cli_profiles"]) == 9
        assert all(item["inference_verified"] is False for item in body["cli_profiles"])
        assert all(
            secret not in response.text
            for secret in [
                "fake-global-key",
                "fake-admin-secret",
                "private-command-path",
            ]
        )
        assert client.get("/costs").json()["attempts"] == 0


def test_unconfigured_admin_token_denies_all_writes(db, settings):
    from pydantic import SecretStr

    settings = settings.model_copy(update={"api_admin_token": SecretStr("")})
    with TestClient(create_app(settings, Desk(settings, db))) as client:
        assert client.post("/model/probe").status_code == 503
        assert client.get("/health").status_code == 200


@pytest.mark.asyncio
async def test_risk_not_blocked_by_llm_lock(db, settings):
    desk = Desk(settings, db, LLMRuntime(settings))
    await desk.runtime.lock.acquire()
    try:
        import asyncio

        result = await asyncio.wait_for(desk.run_risk("main", observations={}), timeout=1)
        assert result["positions_checked"] == 0
    finally:
        desk.runtime.lock.release()
        await desk.close()
