from __future__ import annotations

import asyncio
import json
import signal
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from trading_team import autonomy
from trading_team.domain import ChiefOutput, utcnow
from trading_team.engine import Desk
from trading_team.llm import LLMError
from trading_team.metrics import costs, performance
from trading_team.operations import LeaseBusy, LeaseManager
from trading_team.storage import Decision, audit, lock_account

SEOUL = ZoneInfo("Asia/Seoul")


async def daily_report(desk: Desk) -> dict:
    now = utcnow()
    start = datetime.combine(now.astimezone(SEOUL).date(), time(), tzinfo=SEOUL).astimezone(UTC)
    with desk.db.transaction() as s:
        rejections = len(
            list(
                s.scalars(
                    select(Decision).where(
                        Decision.account_id == "main",
                        Decision.allowed.is_(False),
                        Decision.created_at >= start,
                    )
                )
            )
        )
    facts = {
        "date_seoul": now.astimezone(SEOUL).date().isoformat(),
        "performance": performance(desk.db, "main"),
        "costs": costs(desk.db),
        "rejections_today": rejections,
        "configuration_warning": "FEWER_THAN_10_REJECTIONS_REVIEW_DATA_OR_FILTERS"
        if rejections < 10
        else None,
    }
    try:
        if not desk.settings.autonomous_enabled:
            raise LLMError("AUTOMATION_DISABLED")
        summary = await desk.runtime.generate(
            "CHIEF",
            {
                "task": "Daily report. Summarize these recorded facts only. Do not issue trading instructions.",
                "facts": facts,
            },
            ChiefOutput,
            "daily-report",
        )
        facts["summary"] = summary.model_dump(mode="json")
    except LLMError as exc:
        facts["report_degraded"] = exc.code
    desk.record_agent("daily-report", "CHIEF", "daily_report", facts)
    with desk.db.transaction() as s:
        account = lock_account(s, "main")
        audit(s, account, "DAILY_REPORT", facts)
    return facts


def build_scheduler(desk: Desk, leases: LeaseManager) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone=SEOUL,
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
    )
    active_jobs = set()
    scheduler.trading_team_tasks = active_jobs

    async def invoke(name, operation):
        task = asyncio.current_task()
        active_jobs.add(task)
        try:
            async with leases.hold(name):
                autonomy.job_started(desk, name)
                try:
                    result = await operation()
                except asyncio.CancelledError:
                    autonomy.job_finished(
                        desk, name, "interrupted", {"reason": "EXECUTION_CANCELLED"}
                    )
                    raise
                except Exception:
                    autonomy.job_finished(desk, name, "failed", {"reason": "JOB_EXECUTION_FAILED"})
                    raise
                autonomy.job_finished(desk, name, "completed", result or {})
                desk.supervisor.observe(name, True)
        except LeaseBusy:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            desk.supervisor.observe(name, False, "JOB_EXECUTION_FAILED")
            desk.record_agent(
                name, "CHIEF", "job_failed", {"job": name, "reason": "JOB_EXECUTION_FAILED"}
            )
        finally:
            active_jobs.discard(task)

    async def scan():
        if not desk.settings.autonomous_enabled:
            return {"status": "disabled", "orders": [], "reason": "AUTOMATION_DISABLED"}
        if desk.settings.autonomous_enabled and not desk.capabilities().get("verified"):
            return {
                "status": "awaiting_model",
                "orders": [],
                "reason": "AUTOMATIC_MODEL_PROBE_PENDING",
            }
        bucket = int(utcnow().timestamp()) // 900
        return await desk.run_cycle("main", f"scheduled:{desk.settings.revision}:{bucket}")

    async def prepare_model():
        was_verified = bool(desk.capabilities().get("verified"))
        result = await autonomy.ensure_model(desk)
        if not was_verified and result.get("verified") and scheduler.running:
            scheduler.modify_job("scan", next_run_time=utcnow())
        return result

    async def world_monitor():
        readiness = await desk.wm.readiness()
        desk.record_agent(
            "world-monitor",
            "CHIEF",
            "ready" if readiness["status"] == "READY" else "degraded",
            readiness,
        )
        return readiness

    immediate = {"next_run_time": utcnow()} if desk.settings.autonomous_enabled else {}
    if desk.settings.autonomous_enabled:
        scheduler.add_job(
            invoke,
            "interval",
            seconds=desk.settings.autonomous_collect_seconds,
            args=["market-collect", lambda: autonomy.collect(desk)],
            id="market-collect",
            **immediate,
        )
        if desk.settings.worldmonitor_seed_enabled:
            from trading_team.seeding import seed_worldmonitor

            scheduler.add_job(
                invoke,
                "interval",
                minutes=15,
                args=["world-monitor-seed", lambda: seed_worldmonitor(desk.settings)],
                id="world-monitor-seed",
                **immediate,
            )
        scheduler.add_job(
            invoke,
            "interval",
            minutes=5,
            args=["model-probe", prepare_model],
            id="model-probe",
            **immediate,
        )
        scheduler.add_job(
            invoke,
            "interval",
            minutes=15,
            args=["book:main", lambda: autonomy.book(desk)],
            id="book",
            **immediate,
        )

    scheduler.add_job(
        invoke,
        "interval",
        minutes=15,
        args=["cycle:main", scan],
        id="scan",
        name="SCAN every 15 minutes",
        **immediate,
    )
    # RISK has its own job and lease; it never waits for the shared LLM semaphore.
    scheduler.add_job(
        invoke,
        "interval",
        minutes=5,
        args=["risk:main", lambda: desk.run_risk("main")],
        id="risk",
        name="RISK every 5 minutes",
        **immediate,
    )
    scheduler.add_job(
        invoke,
        "interval",
        minutes=15,
        args=["world-monitor", world_monitor],
        id="world-monitor",
        **immediate,
    )
    scheduler.add_job(
        invoke,
        "cron",
        hour=23,
        minute=0,
        args=["daily-report", lambda: daily_report(desk)],
        id="daily-report",
        name="CHIEF 23:00 Asia/Seoul",
    )
    return scheduler


async def run_worker(desk: Desk):
    leases = LeaseManager(desk.db)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with leases.hold("worker:main"):
        desk.assert_config()
        autonomy.job_started(desk, "worker:main")
        scheduler = build_scheduler(desk, leases)
        scheduler.start()
        print(
            json.dumps(
                {
                    "worker": "running",
                    "paper_only": True,
                    "timezone": "Asia/Seoul",
                    "jobs": [
                        {"id": job.id, "trigger": str(job.trigger)} for job in scheduler.get_jobs()
                    ],
                }
            ),
            flush=True,
        )
        try:
            await stop.wait()
        finally:
            scheduler.pause()
            await asyncio.sleep(0)
            tasks = list(scheduler.trading_team_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            scheduler.shutdown(wait=False)
            autonomy.job_finished(desk, "worker:main", "stopped", {"reason": "WORKER_SHUTDOWN"})
            await asyncio.sleep(0)
