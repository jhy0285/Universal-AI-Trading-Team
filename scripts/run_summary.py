#!/usr/bin/env python3
"""Summarise what an autonomous paper run did, from the ledger database.

Read-only. Answers the questions every run raises: did the jobs run, how many cycles and
decisions, which gate blocked what, did any candidate reach the model, were there fills,
and is the ledger intact. Uses DATABASE_URL, or pass --database-url.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from trading_team.broker import PaperBroker
from trading_team.settings import Settings
from trading_team.storage import (
    Account,
    AutomationJob,
    Cycle,
    Database,
    Decision,
    ModelUsage,
    PaperOrder,
    Position,
    SnapshotRow,
    TradeEpisode,
)

# Rejected before any observation exists for the pool; not a verdict about the market.
UNOBSERVED = "STALE_OR_UNKNOWN_MARKET_TIME"


def summarize(db: Database, account_id: str = "main") -> dict:
    with db.transaction() as s:
        account = s.get(Account, account_id)
        if account is None:
            raise SystemExit(f"Unknown account: {account_id}")
        cycles = Counter(
            s.execute(
                select(Cycle.status).where(Cycle.account_id == account_id, ~Cycle.id.like("book%"))
            ).scalars()
        )
        rows = s.execute(
            select(Decision.allowed, Decision.reason, Decision.asset_id).where(
                Decision.account_id == account_id
            )
        ).all()
        reasons = Counter((allowed, reason) for allowed, reason, _ in rows)
        observed = {asset for _, reason, asset in rows if reason != UNOBSERVED}
        usage = s.execute(
            select(
                ModelUsage.role, ModelUsage.status, func.count(), func.avg(ModelUsage.latency_ms)
            )
            .join(Cycle, Cycle.id == ModelUsage.cycle_id)
            .where(Cycle.account_id == account_id)
            .group_by(ModelUsage.role, ModelUsage.status)
        ).all()
        jobs = {
            row.name: {
                "state": row.state,
                "finished_at": str(row.finished_at)[:19] if row.finished_at else None,
                "result": json.loads(row.result or "{}"),
            }
            for row in s.scalars(select(AutomationJob).order_by(AutomationJob.name))
        }
        orders = Counter(
            (o.side, o.status)
            for o in s.scalars(select(PaperOrder).where(PaperOrder.account_id == account_id))
        )
        open_positions = [
            {"asset_id": p.asset_id, "quantity": str(p.quantity)}
            for p in s.scalars(
                select(Position).where(Position.account_id == account_id, Position.quantity > 0)
            )
        ]
        # Decimals are stored as text for exactness, so a SQL SUM fails on PostgreSQL.
        pnl = list(
            s.scalars(
                select(TradeEpisode.realized_pnl).where(
                    TradeEpisode.account_id == account_id, TradeEpisode.closed_at.is_not(None)
                )
            )
        )
        snapshots = {
            kind: n
            for kind, n in s.execute(
                select(SnapshotRow.kind, func.count()).group_by(SnapshotRow.kind)
            )
        }
        result = {
            "account": {
                "id": account_id,
                "provenance": account.provenance,
                "cash": str(account.cash),
                "initial_cash": str(account.initial_cash),
                "killed": account.killed,
            },
            "jobs": jobs,
            "scan_cycles": dict(cycles),
            "decisions": {
                "total": len(rows),
                "distinct_assets": len({asset for _, _, asset in rows}),
                "observed_pools": len(observed),
                "by_reason": {
                    f"{'ALLOW' if allowed else 'block'} {reason}": count
                    for (allowed, reason), count in reasons.most_common()
                },
            },
            "model_calls_in_cycles": [
                {"role": role, "status": status, "n": n, "avg_s": round(float(avg or 0) / 1000, 1)}
                for role, status, n, avg in usage
            ],
            "orders": {f"{side} {status}": n for (side, status), n in orders.items()},
            "open_positions": open_positions,
            "closed_trades": {"count": len(pnl), "realized_pnl": str(sum(pnl, Decimal(0)))},
            "snapshots_by_kind": snapshots,
        }
    result["ledger_integrity"] = PaperBroker(db).integrity(account_id)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default="main")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--json", action="store_true", help="print the raw summary as JSON")
    args = parser.parse_args()
    url = args.database_url or Settings().database_url
    db = Database(url)
    summary = summarize(db, args.account)
    if url.startswith("sqlite:///"):
        path = Path(url.removeprefix("sqlite:///"))
        if path.is_file():
            summary["database_kb"] = path.stat().st_size // 1024
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return
    a = summary["account"]
    print(
        f"account {a['id']} ({a['provenance']}): cash {a['cash']} of {a['initial_cash']}, killed={a['killed']}"
    )
    print("ledger integrity:", summary["ledger_integrity"])
    d = summary["decisions"]
    print(
        f"scan cycles {summary['scan_cycles']} | decisions {d['total']} over {d['distinct_assets']} assets,"
        f" {d['observed_pools']} observed pools"
    )
    for reason, count in d["by_reason"].items():
        print(f"  {count:5}  {reason}")
    print("model calls inside cycles:", summary["model_calls_in_cycles"] or "none")
    print(
        "orders:",
        summary["orders"] or "none",
        "| open positions:",
        summary["open_positions"] or "none",
    )
    print("closed trades:", summary["closed_trades"])
    print("jobs:")
    for name, job in summary["jobs"].items():
        print(f"  {name:20} {job['state']:10} {job['finished_at']}")
    if "database_kb" in summary:
        print("database size:", summary["database_kb"], "KB")


if __name__ == "__main__":
    main()
