"""Operational automation. Trading approvals remain in the existing deterministic gates."""

from __future__ import annotations

import json
import re

from sqlalchemy import select

from trading_team.data import DataError, epoch_millis
from trading_team.domain import PredictionQuestion, stable_hash, utcnow
from trading_team.storage import Account, AutomationJob, CapabilityRow, Lease, as_dict, lock_account


def job_started(desk, name: str):
    with desk.db.transaction() as session:
        lock_account(session, "main")
        row = session.get(AutomationJob, name)
        values = dict(
            config_revision=desk.settings.revision,
            state="running",
            started_at=utcnow(),
            finished_at=None,
            result="{}",
        )
        if row is None:
            session.add(AutomationJob(name=name, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)


def job_finished(desk, name: str, state: str, result: dict):
    with desk.db.transaction() as session:
        lock_account(session, "main")
        row = session.get(AutomationJob, name)
        if row and row.config_revision == desk.settings.revision:
            row.state, row.finished_at = state, utcnow()
            row.result = json.dumps(result, default=str, ensure_ascii=False)


async def ensure_model(desk) -> dict:
    cfg = desk.settings
    blockers = cfg.model_blockers()
    if blockers:
        return {"status": "blocked", "reason": blockers[0]}
    if desk.capabilities().get("verified"):
        return {"status": "verified", "action": "reuse_current_probe"}
    with desk.db.transaction() as session:
        last = session.get(CapabilityRow, cfg.revision)
        if (
            last
            and not last.verified
            and (utcnow() - last.tested_at).total_seconds() < cfg.autonomous_probe_retry_seconds
        ):
            return {"status": "blocked", "reason": "MODEL_PROBE_RETRY_COOLDOWN"}
    result = await desk.probe()
    return {
        "status": "verified" if result["verified"] else "blocked",
        "verified": result["verified"],
        "roles": result["roles"],
    }


async def collect(desk) -> dict:
    result = await desk.collect()
    # Persist bounded counts and error codes, rather than duplicating every raw snapshot.
    return {
        "status": "observed",
        "count": result["count"],
        "errors": result["errors"],
        "paper_only": True,
    }


def status(desk) -> dict:
    now = utcnow()
    with desk.db.transaction() as session:
        account = session.get(Account, "main")
        worker = session.get(Lease, "worker:main")
        worker_lease_active = bool(worker and worker.expires_at > now)
        worker_job = session.get(AutomationJob, "worker:main")
        worker_configuration_current = bool(
            worker_job and worker_job.config_revision == desk.settings.revision
        )
        worker_running = bool(
            worker_lease_active and worker_configuration_current and worker_job.state == "running"
        )
        jobs = []
        for row in session.scalars(select(AutomationJob).order_by(AutomationJob.name)):
            item = as_dict(row)
            item["result"] = json.loads(row.result)
            item["current_configuration"] = row.config_revision == desk.settings.revision
            jobs.append(item)
        killed = bool(account and account.killed)
    snapshots = desk.latest_snapshots(limit=100)
    missing: dict[str, int] = {}
    required = (
        "observed_at",
        "holder_count",
        "previous_holder_count",
        "holder_observed_at",
        "previous_holder_observed_at",
        "previous_price",
        "largest_token_account_share",
        "onchain_observed_at",
        "contract_safe",
        "large_holder_selling",
        "buy_quote",
        "sell_quote",
    )
    complete = 0
    for snapshot in snapshots:
        gaps = [field for field in required if getattr(snapshot, field) is None]
        if len(snapshot.windows) != 2:
            gaps.append("two_disjoint_transaction_windows")
        if not snapshot.fresh(now, desk.settings.market_max_age_seconds):
            gaps.append("fresh_market_observation")
        for field in gaps:
            missing[field] = missing.get(field, 0) + 1
        if not gaps:
            complete += 1
    model_verified = bool(desk.capabilities().get("verified"))
    return {
        "enabled": desk.settings.autonomous_enabled,
        "worker_running": worker_running,
        "worker_lease_active": worker_lease_active,
        "worker_configuration_current": worker_configuration_current,
        "state": "disabled"
        if not desk.settings.autonomous_enabled
        else "worker_stopped"
        if not worker_running
        else "entries_halted"
        if killed
        else "awaiting_model"
        if not model_verified
        else "observing"
        if not complete
        else "evaluating",
        "paper_only": True,
        "funds": "paper_money_only",
        "human_trade_approval_required": False,
        "model_verified": model_verified,
        "kill_switch_active": killed,
        "market_data": {
            "snapshots": len(snapshots),
            "complete_observations": complete,
            "missing_fields": missing,
        },
        "jobs": jobs,
        "scan_interval_seconds": 900,
        "risk_interval_seconds": 300,
    }


async def book(desk) -> dict:
    if not desk.capabilities().get("verified"):
        return {"status": "awaiting_model", "orders": []}
    readiness = await desk.wm.readiness()
    if readiness["status"] != "READY":
        return {"status": "degraded", "reason": "WORLD_MONITOR_DEGRADED", "orders": []}
    try:
        discovery, events = await desk.wm.discovery(), await desk.wm.events()
    except DataError as exc:
        return {"status": "degraded", "reason": exc.code, "orders": []}
    now = utcnow()
    signals = []
    for signal in events.get("signals", [])[:30]:
        if (
            not isinstance(signal, dict)
            or signal.get("type") == "CROSS_SOURCE_SIGNAL_TYPE_FORECAST_DETERIORATION"
        ):
            continue
        observed = epoch_millis(signal.get("detectedAt"))
        if (
            not observed
            or not 0 <= (now - observed).total_seconds() <= 48 * 3600
            or not isinstance(signal.get("summary"), str)
            or not signal.get("id")
        ):
            continue
        signals.append((signal, observed))
    if not signals:
        return {"status": "observing", "reason": "BOOK_NO_RECENT_EVIDENCE", "orders": []}
    from trading_team.storage import Position

    with desk.db.transaction() as session:
        open_assets = {
            row.asset_id.rsplit(":", 1)[0]
            for row in session.scalars(select(Position).where(Position.account_id == "main"))
            if row.quantity > 0 and row.market_kind == "prediction"
        }
    results = []
    ignored = {
        "will",
        "what",
        "when",
        "does",
        "have",
        "there",
        "this",
        "that",
        "before",
        "after",
        "than",
        "more",
        "from",
        "with",
        "next",
        "year",
        "which",
        "would",
        "could",
        "their",
        "under",
        "over",
        "about",
    }

    def keywords(text):
        return set(re.findall(r"[a-z]{4,}", text.lower())) - ignored

    for market in discovery.get("markets", [])[:30]:
        if len(results) >= 3:
            break
        if not isinstance(market, dict) or not isinstance(market.get("source"), str):
            continue
        venue = {
            "MARKET_SOURCE_KALSHI": "kalshi",
            "MARKET_SOURCE_POLYMARKET": "polymarket",
            "kalshi": "kalshi",
            "polymarket": "polymarket",
        }.get(market.get("source"))
        market_id = market.get("id")
        if (
            not venue
            or not isinstance(market_id, str)
            or f"prediction:{venue}:{market_id}" in open_assets
        ):
            continue
        try:
            metadata, targets = await desk.prediction.question_metadata(venue, market_id)
            selected = [
                (signal, time)
                for signal, time in signals
                if len(keywords(metadata["title"]) & keywords(signal["summary"])) >= 2
            ][:5]
            if not selected:
                continue
            question = PredictionQuestion(
                **metadata,
                evidence_observed_at=min(time for _, time in selected),
                source_ids=["wm:signal:" + str(signal["id"]) for signal, _ in selected],
                evidence_text=[
                    "Derived World Monitor signal; relevance and causal support must be evaluated, and uncertainty requires abstention: "
                    + signal["summary"][:4000]
                    for signal, _ in selected
                ],
            )
            if question.closes_at <= now:
                continue

            async def prices(question=question, targets=targets):
                return await desk.prediction.prices_for(question, targets)

            request_id = stable_hash(
                {
                    "config": desk.settings.revision,
                    "question": question.question_id,
                    "bucket": int(now.timestamp()) // 900,
                }
            )
            results.append(
                await desk.run_book("main", "autonomous:" + request_id, question, prices)
            )
        except (DataError, ValueError, KeyError, TypeError):
            continue
    return {
        "status": "evaluated" if results else "observing",
        "questions_evaluated": len(results),
        "cycles": results,
        "human_trade_approval_required": False,
        "paper_only": True,
    }
