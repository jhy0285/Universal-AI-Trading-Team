from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from trading_team.broker import PaperBroker
from trading_team.domain import utcnow
from trading_team.storage import (
    Account,
    Cycle,
    Database,
    Decision,
    EquityPoint,
    ModelUsage,
    PaperOrder,
    Position,
    ReplayRun,
    TradeEpisode,
    as_dict,
)

D = Decimal


def performance(db: Database, account_id: str, *, now=None) -> dict:
    now = now or utcnow()
    with db.transaction() as s:
        account = s.get(Account, account_id)
        if account is None:
            raise ValueError("unknown account")
        equity = PaperBroker.equity(s, account, now)
        positions = list(s.scalars(select(Position).where(Position.account_id == account_id)))
        episodes = list(
            s.scalars(
                select(TradeEpisode).where(
                    TradeEpisode.account_id == account_id, TradeEpisode.closed_at.is_not(None)
                )
            )
        )
        orders = list(s.scalars(select(PaperOrder).where(PaperOrder.account_id == account_id)))
        usage = list(
            s.scalars(
                select(ModelUsage)
                .join(Cycle, Cycle.id == ModelUsage.cycle_id)
                .where(Cycle.account_id == account_id)
            )
        )
        model_cost = (
            sum((x.estimated_cost for x in usage), D(0))
            if all(x.estimated_cost is not None for x in usage)
            else None
        )
        points = list(
            s.scalars(
                select(EquityPoint)
                .where(EquityPoint.account_id == account_id)
                .order_by(EquityPoint.id)
            )
        )
        gains = sum((max(D(0), x.realized_pnl) for x in episodes), D(0))
        losses = sum((max(D(0), -x.realized_pnl) for x in episodes), D(0))
        peak, drawdown = account.initial_cash, D(0)
        for point in points:
            if point.equity is not None:
                peak = max(peak, point.equity)
                drawdown = max(drawdown, (peak - point.equity) / peak)
        if equity is not None:
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
        return {
            "account_id": account_id,
            "provenance": account.provenance,
            "paper_only": True,
            "cash": str(account.cash),
            "initial_cash": str(account.initial_cash),
            "equity": None if equity is None else str(equity),
            "net_pnl": None if equity is None else str(equity - account.initial_cash),
            "model_estimated_cost_usd": str(model_cost) if model_cost is not None else None,
            "net_after_model_estimated_cost": str(equity - account.initial_cash - model_cost)
            if equity is not None and model_cost is not None
            else None,
            "model_cost_basis": "no model calls"
            if not usage
            else "estimated; capability probes excluded"
            if model_cost is not None
            else "unknown",
            "return_fraction": None if equity is None else str(equity / account.initial_cash - 1),
            "valuation_reason": "FRESH_MARKS"
            if equity is not None
            else "OPEN_POSITION_MARK_STALE_OR_MISSING",
            "realized_pnl": str(sum((x.realized_pnl for x in positions), D(0))),
            "fees": str(sum((x.fee for x in orders), D(0))),
            "filled_orders": sum(x.quantity > 0 for x in orders),
            "partial_orders": sum(x.status == "partial" for x in orders),
            "rejected_orders": sum(x.status == "rejected" for x in orders),
            "open_positions": sum(x.quantity > 0 for x in positions),
            "closed_trades": len(episodes),
            "win_rate": str(D(sum(x.realized_pnl > 0 for x in episodes)) / len(episodes))
            if episodes
            else None,
            "profit_factor": str(gains / losses) if losses else None,
            "profit_factor_reason": "DEFINED" if losses else "NO_CLOSED_LOSS_DENOMINATOR",
            "max_drawdown_fraction": str(drawdown),
            "drawdown_basis": "available equity samples; unpriced intervals excluded",
            "equity_samples": len(points),
            "unpriced_samples": sum(x.equity is None for x in points),
            "killed": account.killed,
            "kill_reason": account.kill_reason,
        }


def costs(db: Database) -> dict:
    with db.transaction() as s:
        rows = list(s.scalars(select(ModelUsage)))
    known = [x for x in rows if x.estimated_cost is not None]
    return {
        "attempts": len(rows),
        "failed_attempts": sum(x.status != "passed" for x in rows),
        "input_tokens": sum(x.input_tokens or 0 for x in rows),
        "output_tokens": sum(x.output_tokens or 0 for x in rows),
        "usage_unknown_attempts": sum(
            x.input_tokens is None or x.output_tokens is None for x in rows
        ),
        "estimated_cost_usd": str(sum((x.estimated_cost for x in known), D(0))) if known else None,
        "cost_unknown_attempts": len(rows) - len(known),
        "cost_is_estimate": True,
        "mean_latency_ms": sum(x.latency_ms for x in rows) / len(rows) if rows else None,
        "subscription_note": "Codex subscription usage is not API billing; unknown cost is never reported as zero.",
    }


def comparisons(db: Database) -> list[dict]:
    with db.transaction() as s:
        runs = [as_dict(x) for x in s.scalars(select(ReplayRun).order_by(ReplayRun.created_at))]
    results = []
    for run in runs:
        with db.transaction() as s:
            decisions = list(
                s.scalars(select(Decision).where(Decision.account_id == run["account_id"]))
            )
        results.append(
            {
                **run,
                "performance": performance(db, run["account_id"]),
                "decisions": [
                    {"snapshot_id": x.snapshot_id, "allowed": x.allowed, "reason": x.reason}
                    for x in decisions
                ],
                "comparison_group": f"{run['dataset_hash']}:{run['policy_version']}:{run['initial_cash']}",
                "statistical_superiority": "not established",
            }
        )
    return results
