from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select

from trading_team import learning
from trading_team.api import create_app
from trading_team.broker import PaperBroker
from trading_team.domain import stable_hash, utcnow
from trading_team.engine import Desk
from trading_team.operations import LeaseManager
from trading_team.storage import (
    Account,
    AgentRecord,
    AuditEvent,
    AutomationJob,
    Cycle,
    Decision,
    ModelUsage,
    PaperOrder,
    SnapshotRow,
    TradeEpisode,
)
from trading_team.worker import build_scheduler

D = Decimal
ACCOUNT = "paper"  # fixture provenance, matching the demo snapshot


def cycle(s, cycle_id: str):
    s.add(
        Cycle(
            id=cycle_id,
            account_id=ACCOUNT,
            status="completed",
            config_revision="r",
            provider="p",
            model="m",
            policy_version="v",
            provenance="fixture",
        )
    )


def seed_moves(db, snapshot, reason: str, *, assets: int, per_asset: int, move: str, now):
    """Rejections at a base price of 100, each followed 24h later by price 100 * (1 + move)."""
    broker = PaperBroker(db)
    start = now - timedelta(days=3)
    for a in range(assets):
        asset_id = f"solana:ASSET{a}"
        for i in range(per_asset):
            decided = start + timedelta(minutes=15 * i)
            base = snapshot.model_copy(
                update=dict(
                    asset_id=asset_id,
                    fetched_at=decided,
                    observed_at=decided,
                    buy_quote=None,
                    sell_quote=None,
                )
            )
            later = base.model_copy(
                update=dict(
                    fetched_at=decided + timedelta(hours=24, minutes=1),
                    price=D(100) * (1 + D(move)),
                )
            )
            broker.save_snapshot(base)
            broker.save_snapshot(later)
            with db.transaction() as s:
                s.add(
                    Decision(
                        cycle_id="c",
                        account_id=ACCOUNT,
                        asset_id=asset_id,
                        snapshot_id=base.digest,
                        allowed=False,
                        reason=reason,
                        created_at=decided,
                    )
                )


def test_forward_returns_by_gate_reason_and_matured_horizons_only(db, snapshot):
    now = utcnow()
    seed_moves(db, snapshot, "SCAN_LIQUIDITY", assets=6, per_asset=6, move="0.10", now=now)
    # Decided 2h ago: its 24h outcome does not exist yet and must not be guessed.
    recent = snapshot.model_copy(update=dict(asset_id="solana:NEW", buy_quote=None))
    PaperBroker(db).save_snapshot(recent)
    with db.transaction() as s:
        s.add(
            Decision(
                cycle_id="c",
                account_id=ACCOUNT,
                asset_id="solana:NEW",
                snapshot_id=recent.digest,
                allowed=False,
                reason="SCAN_LIQUIDITY",
                created_at=now - timedelta(hours=2),
            )
        )
    report = learning.evaluate(db, ACCOUNT, now)
    group = next(x for x in report["gate_regret"] if x["reason"] == "SCAN_LIQUIDITY")
    day = group["horizons"]["86400"]
    assert group["decisions"] == 37 and not group["allowed"]
    assert day["n"] == 36 and day["assets"] == 6 and day["sufficient"]
    assert D(day["mean"]) == D("0.1") and D(day["hit_rate"]) == 1
    # +1h: only the first two decisions per asset have a later snapshot (the next ones, at an
    # unchanged price). +6h: no snapshot exists, and none is invented.
    assert group["horizons"]["3600"]["n"] == 12 and D(group["horizons"]["3600"]["mean"]) == 0
    assert group["horizons"]["21600"]["n"] == 0


def test_a_later_snapshot_from_another_pool_is_not_a_price_path(db, snapshot):
    now = utcnow()
    decided = now - timedelta(days=2)
    base = snapshot.model_copy(
        update=dict(fetched_at=decided, observed_at=decided, buy_quote=None, sell_quote=None)
    )
    other_pool = base.model_copy(
        update=dict(pair_id="other-pool", fetched_at=decided + timedelta(hours=24), price=D(200))
    )
    broker = PaperBroker(db)
    broker.save_snapshot(base)
    broker.save_snapshot(other_pool)
    with db.transaction() as s:
        s.add(
            Decision(
                cycle_id="c",
                account_id=ACCOUNT,
                asset_id=base.asset_id,
                snapshot_id=base.digest,
                allowed=False,
                reason="SCAN_LIQUIDITY",
                created_at=decided,
            )
        )
    group = learning.evaluate(db, ACCOUNT, now)["gate_regret"][0]
    assert group["horizons"]["86400"]["n"] == 0


def test_strict_gate_that_rejected_movers_becomes_a_proposal_but_data_gaps_do_not(db, snapshot):
    now = utcnow()
    seed_moves(db, snapshot, "SCAN_LIQUIDITY", assets=6, per_asset=6, move="0.10", now=now)
    seed_moves(
        db, snapshot, "HOLDER_HISTORY_UNAVAILABLE", assets=6, per_asset=6, move="0.10", now=now
    )
    found = {x["key"]: x for x in learning.derive_proposals(learning.evaluate(db, ACCOUNT, now))}
    assert "gate-review:0:SCAN_LIQUIDITY" in found
    # A missing observation is a data problem to fix, not a threshold to loosen.
    assert "gate-review:0:HOLDER_HISTORY_UNAVAILABLE" not in found
    coverage = found["data-coverage:HOLDER_HISTORY_UNAVAILABLE"]
    assert coverage["evidence"]["forward_24h"]["n"] == 36
    assert "Do not relax the gate" in coverage["action"]


def test_small_or_flat_samples_propose_nothing(db, snapshot):
    now = utcnow()
    seed_moves(db, snapshot, "SCAN_LIQUIDITY", assets=2, per_asset=3, move="0.10", now=now)
    seed_moves(
        db, snapshot, "CONCENTRATION_OVER_5_PERCENT", assets=6, per_asset=6, move="0.01", now=now
    )
    kinds = {x["kind"] for x in learning.derive_proposals(learning.evaluate(db, ACCOUNT, now))}
    assert "GATE_REVIEW" not in kinds


def seed_book(db, *, n: int, probability: str, yes_outcomes: int, paid: str = "0.5"):
    """n entered BOOK bets at the given probability; the first `yes_outcomes` resolve YES."""
    with db.transaction() as s:
        for i in range(n):
            cycle_id, asset = f"book{i}", f"prediction:kalshi:M{i}:yes"
            cycle(s, cycle_id)
            s.add(
                AgentRecord(
                    cycle_id=cycle_id,
                    role="BOOK",
                    provider="p",
                    model="m",
                    config_revision="r",
                    status="passed",
                    payload=json.dumps(
                        {"probability_yes": probability, "abstain": False, "evidence": []}
                    ),
                )
            )
            s.add(
                PaperOrder(
                    id=f"o{i}",
                    account_id=ACCOUNT,
                    cycle_id=cycle_id,
                    asset_id=asset,
                    side="buy",
                    status="filled",
                    reason="x",
                    requested=D(10),
                    quantity=D(20),
                    notional=D(10) * D(paid) / D("0.5"),
                )
            )
            s.add(
                SnapshotRow(
                    id=f"res{i}",
                    asset_id=asset,
                    kind="prediction_resolution",
                    source="kalshi_public_resolution",
                    provenance="fixture",
                    fetched_at=utcnow(),
                    payload=json.dumps({"payout": "1" if i < yes_outcomes else "0"}),
                )
            )


def test_book_brier_is_compared_with_the_market_price_it_bet_against(db):
    seed_book(db, n=20, probability="0.8", yes_outcomes=14)
    book = learning.calibration(db, ACCOUNT)["BOOK"]
    # Hand check: 14 * 0.2^2 + 6 * 0.8^2 = 4.4 -> 0.22. Paying 0.5 implies 0.5 -> 0.25.
    assert book["n"] == 20 and book["status"] == "scored"
    assert D(book["brier"]) == D("0.22") and D(book["market_brier"]) == D("0.25")
    assert D(book["base_rate"]) == D("0.7")
    assert [x["n"] for x in book["bins"]] == [20]
    report = learning.evaluate(db, ACCOUNT)
    assert not [x for x in learning.derive_proposals(report) if x["kind"] == "CALIBRATION"]


def test_book_worse_than_market_is_flagged(db):
    seed_book(db, n=20, probability="0.2", yes_outcomes=14)
    found = learning.derive_proposals(learning.evaluate(db, ACCOUNT))
    assert [x["key"] for x in found if x["kind"] == "CALIBRATION"] == ["calibration:BOOK"]


def test_book_with_too_few_resolutions_is_not_scored(db):
    seed_book(db, n=5, probability="0.2", yes_outcomes=5)
    book = learning.calibration(db, ACCOUNT)["BOOK"]
    assert book["status"] == "insufficient_data" and book["n"] == 5
    assert not learning.derive_proposals(learning.evaluate(db, ACCOUNT))


def test_vet_calibration_follows_decision_to_closed_trade(db, snapshot):
    with db.transaction() as s:
        for i in range(20):
            cycle_id, asset = f"vet{i}", f"solana:V{i}"
            cycle(s, cycle_id)
            s.add(
                AgentRecord(
                    cycle_id=cycle_id,
                    role="VET",
                    provider="p",
                    model="m",
                    config_revision="r",
                    status="passed",
                    payload=json.dumps({"win_probability": "0.9", "evidence": [f"snap{i}"]}),
                )
            )
            s.add(
                Decision(
                    cycle_id=cycle_id,
                    account_id=ACCOUNT,
                    asset_id=asset,
                    snapshot_id=f"snap{i}",
                    allowed=True,
                    reason="VET_PASS",
                )
            )
            s.add(
                TradeEpisode(
                    id=stable_hash({"cycle": cycle_id, "asset": asset, "side": "buy"}),
                    account_id=ACCOUNT,
                    asset_id=asset,
                    opened_at=utcnow(),
                    closed_at=None if i == 19 else utcnow(),
                    realized_pnl=D(5) if i < 10 else D(-5),
                )
            )
    vet = learning.calibration(db, ACCOUNT)["VET"]
    # The still-open trade is pending, not an outcome. 19 closed: 10 wins, 9 losses.
    assert vet["unresolved"] == 1 and vet["n"] == 19 and vet["status"] == "insufficient_data"
    with db.transaction() as s:
        episode = s.scalars(select(TradeEpisode).where(TradeEpisode.closed_at.is_(None))).one()
        episode.closed_at, episode.realized_pnl = utcnow(), D(-5)
    vet = learning.calibration(db, ACCOUNT)["VET"]
    # 10 wins: 10 * 0.1^2 + 10 * 0.9^2 = 8.2 -> 0.41, worse than the 0.25 base-rate baseline.
    assert (
        vet["n"] == 20 and D(vet["brier"]) == D("0.41") and D(vet["base_rate_brier"]) == D("0.25")
    )
    found = learning.derive_proposals(learning.evaluate(db, ACCOUNT))
    assert "calibration:VET" in [x["key"] for x in found]


def seed_usage(db, role: str, passed: int, schema_failed: int):
    with db.transaction() as s:
        for i in range(passed + schema_failed):
            s.add(
                ModelUsage(
                    cycle_id="c",
                    role=role,
                    provider="p",
                    model="m",
                    status="passed" if i < passed else "LLM_SCHEMA_INVALID",
                    latency_ms=100,
                )
            )
        s.add(  # Capability probes are excluded from reliability.
            ModelUsage(
                cycle_id="capability-probe",
                role=role,
                provider="p",
                model="m",
                status="passed",
                latency_ms=1,
            )
        )


def test_reliability_flags_a_role_that_mostly_fails_schema(db):
    seed_usage(db, "VET", passed=4, schema_failed=8)
    seed_usage(db, "SCAN", passed=12, schema_failed=0)
    report = learning.evaluate(db, ACCOUNT)
    vet = report["reliability"]["VET"]
    assert vet["attempts"] == 12 and vet["schema_failures"] == 8
    assert vet["statuses"] == {"passed": 4, "LLM_SCHEMA_INVALID": 8}
    found = learning.derive_proposals(report)
    assert [x["key"] for x in found] == ["model-reliability:VET"]
    assert "LLM_SCHEMA_INVALID" in found[0]["title"] and "detail" in found[0]["action"]


def test_proposal_lifecycle_keeps_human_decisions_and_withdraws_cleared_ones(db):
    seed_usage(db, "VET", passed=4, schema_failed=8)
    seed_usage(db, "SCAN", passed=4, schema_failed=8)
    first = learning.review(db, ACCOUNT)
    assert sorted(first["created"]) == ["model-reliability:SCAN", "model-reliability:VET"]
    assert learning.review(db, ACCOUNT)["created"] == []
    vet = next(x for x in learning.proposals(db, ACCOUNT) if x["title"].startswith("VET"))
    assert vet["occurrences"] == 2 and vet["status"] == "proposed"

    learning.set_status(db, ACCOUNT, vet["id"], "applied", "switched model")
    with db.transaction() as s:
        s.execute(delete(ModelUsage))
    learning.review(db, ACCOUNT)
    states = {x["title"].split()[0]: x["status"] for x in learning.proposals(db, ACCOUNT)}
    assert states == {"VET": "applied", "SCAN": "withdrawn"}

    seed_usage(db, "SCAN", passed=4, schema_failed=8)  # The problem comes back.
    learning.review(db, ACCOUNT)
    states = {x["title"].split()[0]: x["status"] for x in learning.proposals(db, ACCOUNT)}
    assert states == {"VET": "applied", "SCAN": "proposed"}

    with db.transaction() as s:
        events = [x.event_type for x in s.scalars(select(AuditEvent))]
    assert events.count("LEARNING_PROPOSALS_CREATED") == 1
    assert events.count("LEARNING_PROPOSAL_REVIEWED") == 1
    assert PaperBroker(db).integrity(ACCOUNT)["audit_chain_valid"]


def test_review_cannot_touch_another_account_or_use_unknown_status(db):
    seed_usage(db, "VET", passed=4, schema_failed=8)
    learning.review(db, ACCOUNT)
    proposal = learning.proposals(db, ACCOUNT)[0]
    with pytest.raises(LookupError):
        learning.set_status(db, "main", proposal["id"], "dismissed", "")
    with pytest.raises(ValueError):
        learning.set_status(db, ACCOUNT, proposal["id"], "approved_and_executed", "")
    with pytest.raises(ValueError):
        learning.evaluate(db, "no-such-account")


def test_learning_never_changes_ledger_or_policy(db, snapshot):
    now = utcnow()
    seed_moves(db, snapshot, "SCAN_LIQUIDITY", assets=6, per_asset=6, move="0.10", now=now)
    with db.transaction() as s:
        before = (
            [x.cash for x in s.scalars(select(Account))],
            len(list(s.scalars(select(PaperOrder)))),
        )
    result = learning.review(db, ACCOUNT, now)
    assert result["report"]["automatic_policy_changes"] is False
    with db.transaction() as s:
        after = (
            [x.cash for x in s.scalars(select(Account))],
            len(list(s.scalars(select(PaperOrder)))),
        )
    assert before == after


def test_api_requires_admin_to_run_or_review_and_serves_the_stored_report(db, settings):
    settings = settings.model_copy(update={"api_admin_token": SecretStr("test-admin-token")})
    seed_usage(db, "VET", passed=4, schema_failed=8)
    auth = {"Authorization": "Bearer test-admin-token"}
    with TestClient(create_app(settings, Desk(settings, db))) as client:
        assert client.get("/learning?account_id=paper").json() == {"report": None, "proposals": []}
        assert client.post("/learning/run", json={"account_id": "paper"}).status_code == 401
        run = client.post("/learning/run", json={"account_id": "paper"}, headers=auth)
        assert run.status_code == 200 and run.json()["created"] == ["model-reliability:VET"]
        stored = client.get("/learning?account_id=paper").json()
        assert stored["report"]["paper_only"] and len(stored["proposals"]) == 1
        pid = stored["proposals"][0]["id"]
        body = {"account_id": "paper", "status": "acknowledged", "note": "looking"}
        assert client.post(f"/learning/proposals/{pid}", json=body).status_code == 401
        assert client.post(f"/learning/proposals/{pid}", json=body, headers=auth).status_code == 200
        bad = client.post(f"/learning/proposals/{pid}", json=body | {"status": "x"}, headers=auth)
        assert bad.status_code == 422
        missing = client.post("/learning/proposals/nope", json=body, headers=auth)
        assert missing.status_code == 404


async def test_worker_schedules_hourly_review_only_when_autonomous(db, settings):
    off = Desk(settings, db)
    on = Desk(settings.model_copy(update={"autonomous_enabled": True}), db)
    try:
        assert "learning" not in {x.id for x in build_scheduler(off, LeaseManager(db)).get_jobs()}
        jobs = {x.id: x for x in build_scheduler(on, LeaseManager(db)).get_jobs()}
        assert jobs["learning"].trigger.interval.total_seconds() == 3600
        result = await jobs["learning"].func(*jobs["learning"].args)
        assert result is None  # `invoke` stores the outcome as the job record
        with db.transaction() as s:
            job = s.get(AutomationJob, "learning:main")
        assert job.state == "completed"
        assert json.loads(job.result)["automatic_policy_changes"] is False
    finally:
        await off.close()
        await on.close()
