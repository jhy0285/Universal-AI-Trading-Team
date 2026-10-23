from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from trading_team.domain import utcnow
from trading_team.storage import DailyBaseline, Database, Lease, WorkerHealth, audit, lock_account


def intraday_check(db: Database, account_id: str, now: datetime) -> dict:
    from trading_team.broker import PaperBroker

    day = now.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
    with db.transaction() as s:
        account = lock_account(s, account_id)
        equity = PaperBroker.equity(s, account, now)
        if equity is None:
            return {"status": "unpriced", "notified": False}
        key = account_id + ":" + day
        baseline = s.get(DailyBaseline, key)
        if baseline is None:
            s.add(DailyBaseline(id=key, equity=equity, notified=False))
            return {"status": "baseline_recorded", "equity": str(equity), "notified": False}
        change = equity / baseline.equity - 1
        notify = abs(change) > Decimal("0.10") and not baseline.notified
        if notify:
            baseline.notified = True
            audit(
                s,
                account,
                "INTRADAY_10_PERCENT_ALERT",
                {
                    "date_seoul": day,
                    "baseline": str(baseline.equity),
                    "equity": str(equity),
                    "change_fraction": str(change),
                    "delivery": "internal audit/dashboard only",
                },
            )
        return {"status": "observed", "change_fraction": str(change), "notified": notify}


class LeaseBusy(Exception):
    pass


class LeaseManager:
    def __init__(self, db: Database):
        self.db = db

    def acquire(self, name: str, owner: str, now: datetime, seconds: int = 45) -> bool:
        with self.db.transaction() as s:
            lock_account(s, "main")  # Also serializes creation of a missing lease on PostgreSQL.
            row = s.get(Lease, name)
            if row and row.owner != owner and row.expires_at > now:
                return False
            if row is None:
                s.add(Lease(name=name, owner=owner, expires_at=now + timedelta(seconds=seconds)))
            else:
                row.owner, row.expires_at = owner, now + timedelta(seconds=seconds)
            return True

    def release(self, name: str, owner: str):
        with self.db.transaction() as s:
            lock_account(s, "main")
            row = s.get(Lease, name)
            if row and row.owner == owner:
                s.delete(row)

    @asynccontextmanager
    async def hold(self, name: str):
        owner = uuid.uuid4().hex
        if not self.acquire(name, owner, utcnow()):
            raise LeaseBusy(name)
        task = asyncio.current_task()

        async def renew():
            while True:
                await asyncio.sleep(10)
                try:
                    if not self.acquire(name, owner, utcnow()):
                        task.cancel()
                        return
                except Exception:
                    task.cancel()  # Stop work if ownership cannot be established.
                    return

        heartbeat = asyncio.create_task(renew())
        try:
            yield
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.release(name, owner)


class Supervisor:
    """Two consecutive missed responses request one client restart; two restarts/hour halt entries."""

    def __init__(self, db: Database):
        self.db = db
        self.on_halt = None  # Optional callback(role, reason) after the desk is halted.

    def observe(
        self, role: str, success: bool, reason: str = "", now: datetime | None = None
    ) -> bool:
        now = now or utcnow()
        restart = halted = False
        with self.db.transaction() as s:
            account = lock_account(s, "main")
            health = s.get(WorkerHealth, role)
            if health is None:
                health = WorkerHealth(name=role, consecutive_failures=0, restarts="[]")
                s.add(health)
            if success:
                health.last_success, health.consecutive_failures, health.state, health.reason = (
                    now,
                    0,
                    "healthy",
                    "",
                )
                return False
            health.last_failure, health.reason = now, reason
            health.consecutive_failures += 1
            health.state = "degraded"
            if health.consecutive_failures >= 2:
                history = [
                    x
                    for x in json.loads(health.restarts)
                    if (now - datetime.fromisoformat(x)).total_seconds() < 3600
                ]
                history.append(now.isoformat())
                health.restarts, health.consecutive_failures = json.dumps(history), 0
                restart = len(history) == 1
                health.state = "restart_requested" if restart else "halted"
                audit(
                    s,
                    account,
                    "ROLE_RESTART_REQUESTED" if restart else "DESK_HALTED",
                    {"role": role, "reason": reason, "restart_count_1h": len(history)},
                )
                if not restart:
                    halted = True
                    account.killed, account.kill_reason = (
                        True,
                        f"SUPERVISOR_{role}_REPEATED_FAILURE",
                    )
        if halted and self.on_halt:
            self.on_halt(role, reason)
        return restart

    def status(self) -> list[dict]:
        from trading_team.storage import as_dict

        with self.db.transaction() as s:
            return [as_dict(row) for row in s.scalars(select(WorkerHealth))]
