from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

from trading_team.domain import utcnow
from trading_team.storage import AutomationJob, Cycle, Decision, ModelUsage, TradeEpisode

SPEC = importlib.util.spec_from_file_location(
    "run_summary", Path(__file__).resolve().parent.parent / "scripts" / "run_summary.py"
)
run_summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_summary)


def seed(db):
    with db.transaction() as s:
        s.add(
            Cycle(
                id="c1",
                account_id="main",
                status="completed",
                config_revision="r",
                provider="p",
                model="m",
                policy_version="v",
                provenance="live",
            )
        )
        for asset, reason in (
            ("a", "STALE_OR_UNKNOWN_MARKET_TIME"),
            ("b", "STALE_OR_UNKNOWN_MARKET_TIME"),
            ("c", "CONCENTRATION_OVER_5_PERCENT"),
            ("c", "PRICE_OUTRUNS_HOLDERS"),
        ):
            s.add(
                Decision(
                    cycle_id="c1",
                    account_id="main",
                    asset_id=asset,
                    snapshot_id="x",
                    allowed=False,
                    reason=reason,
                )
            )
        s.add(
            ModelUsage(
                cycle_id="c1", role="VET", provider="p", model="m", status="passed", latency_ms=5000
            )
        )
        s.add(
            ModelUsage(  # A startup probe has no cycle, so it must not count as a cycle call.
                cycle_id="capability-probe",
                role="VET",
                provider="p",
                model="m",
                status="passed",
                latency_ms=1,
            )
        )
        for name, pnl, closed in (("e1", "5.5", True), ("e2", "-2", True), ("e3", "0", False)):
            s.add(
                TradeEpisode(
                    id=name,
                    account_id="main",
                    asset_id=name,
                    opened_at=utcnow(),
                    closed_at=utcnow() if closed else None,
                    realized_pnl=Decimal(pnl),
                )
            )
        s.add(
            AutomationJob(
                name="market-collect",
                config_revision="r",
                state="completed",
                started_at=utcnow(),
                finished_at=utcnow(),
                result=json.dumps({"count": 3}),
            )
        )


def test_summary_separates_unobserved_pools_and_counts_only_cycle_model_calls(db):
    seed(db)
    summary = run_summary.summarize(db, "main")
    decisions = summary["decisions"]
    assert decisions["total"] == 4 and decisions["distinct_assets"] == 3
    # Only pool "c" was ever observed; a and b were rejected before any observation existed.
    assert decisions["observed_pools"] == 1
    assert decisions["by_reason"]["block STALE_OR_UNKNOWN_MARKET_TIME"] == 2
    assert summary["model_calls_in_cycles"] == [
        {"role": "VET", "status": "passed", "n": 1, "avg_s": 5.0}
    ]
    assert summary["scan_cycles"] == {"completed": 1}
    assert summary["orders"] == {} and summary["open_positions"] == []
    # Text-stored Decimals: summed in Python, and the still-open episode is not a closed trade.
    assert summary["closed_trades"] == {"count": 2, "realized_pnl": "3.5"}
    assert summary["jobs"]["market-collect"]["result"] == {"count": 3}
    assert all(summary["ledger_integrity"].values())


def test_summary_of_an_empty_ledger_is_all_zeros(db):
    summary = run_summary.summarize(db, "main")
    assert summary["decisions"]["total"] == 0 and summary["model_calls_in_cycles"] == []
    assert summary["account"]["cash"] == "1000" and not summary["account"]["killed"]
