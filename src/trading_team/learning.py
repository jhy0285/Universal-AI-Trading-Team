"""Closed-loop evaluation of the desk's own decisions.

This module measures; it never edits strategy parameters. Policy interpretation is versioned in
docs/implementation-decisions.md, so a finding becomes a stored proposal that a person reviews.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import select

from trading_team.domain import stable_hash, utcnow
from trading_team.policy import FEE_RATE, POLICY_VERSION, SLIPPAGE_CAP
from trading_team.storage import (
    Account,
    AgentRecord,
    Cycle,
    Database,
    Decision,
    ImprovementProposal,
    LearningReport,
    ModelUsage,
    PaperOrder,
    SnapshotRow,
    TradeEpisode,
    as_dict,
    audit,
    lock_account,
)

D = Decimal
HORIZONS = (3600, 6 * 3600, 24 * 3600)
PRIMARY_HORIZON = 24 * 3600
LOOKBACK_DAYS = 7
DECISION_LIMIT = 1500
CHUNK = 100
MIN_SAMPLES = 30
MIN_ASSETS = 5
MIN_RESOLVED = 20
# A price move that only pays the round trip at the policy's own fee and slippage caps.
HURDLE = 2 * (FEE_RATE + SLIPPAGE_CAP)
# Rejections caused by a missing observation rather than by what the market did.
DATA_GAP_REASONS = frozenset(
    {
        "STALE_OR_UNKNOWN_MARKET_TIME",
        "ONCHAIN_UNAVAILABLE",
        "ONCHAIN_STALE",
        "HOLDER_HISTORY_UNAVAILABLE",
        "HOLDER_HISTORY_STALE",
        "PRICE_UNAVAILABLE",
        "PAIR_HISTORY_UNAVAILABLE",
        "CONTRACT_SAFETY_UNVERIFIED",
        "TWO_DISJOINT_WINDOWS_REQUIRED",
        "LARGE_HOLDER_SALES_UNKNOWN",
    }
)
REVIEW_STATUSES = frozenset({"acknowledged", "dismissed", "applied"})
NOTES = [
    "Forward returns are price-only, read from this system's own stored snapshots, and ignore "
    "execution depth and fills.",
    "The same asset is re-evaluated every cycle, so samples overlap; n overstates independent "
    "evidence.",
    "Calibration covers entered trades only and is biased toward cases that passed every gate.",
    "Fixture and recorded accounts hold synthetic data and say nothing about the model or market.",
    "Nothing here changes trading behaviour. Proposals need a person to review and apply them.",
]


def q(value: Decimal) -> str:
    return str(value.quantize(D("0.000001")))


def _ratio(part: int | Decimal, whole: int | Decimal) -> str | None:
    return q(D(part) / D(whole)) if whole else None


def reliability(db: Database, since: datetime) -> dict:
    with db.transaction() as s:
        rows = list(
            s.scalars(
                select(ModelUsage).where(
                    ModelUsage.created_at >= since, ModelUsage.cycle_id != "capability-probe"
                )
            )
        )
    by_role: dict[str, list[ModelUsage]] = defaultdict(list)
    for row in rows:
        by_role[row.role].append(row)
    return {
        role: {
            "attempts": len(items),
            "passed": sum(x.status == "passed" for x in items),
            "pass_rate": _ratio(sum(x.status == "passed" for x in items), len(items)),
            "schema_failures": sum(x.status.endswith("_SCHEMA_INVALID") for x in items),
            "statuses": dict(Counter(x.status for x in items)),
            "mean_latency_ms": round(sum(x.latency_ms for x in items) / len(items)),
        }
        for role, items in sorted(by_role.items())
    }


def funnel(db: Database, account_id: str, since: datetime) -> dict:
    with db.transaction() as s:
        rows = s.execute(
            select(Decision.allowed, Decision.reason, Decision.asset_id).where(
                Decision.account_id == account_id, Decision.created_at >= since
            )
        ).all()
    groups: dict[tuple[bool, str], list] = defaultdict(lambda: [0, set()])
    for allowed, reason, asset_id in rows:
        groups[(allowed, reason)][0] += 1
        groups[(allowed, reason)][1].add(asset_id)
    return {
        "decisions": len(rows),
        "groups": sorted(
            (
                {
                    "allowed": allowed,
                    "reason": reason,
                    "count": count,
                    "share": _ratio(count, len(rows)),
                    "assets": len(assets),
                }
                for (allowed, reason), (count, assets) in groups.items()
            ),
            key=lambda x: (-x["count"], x["reason"]),
        ),
    }


def _base(s, snapshot_id: str, cache: dict) -> tuple[Decimal, str] | None:
    if snapshot_id not in cache:
        row = s.get(SnapshotRow, snapshot_id)
        data = json.loads(row.payload) if row else {}
        cache[snapshot_id] = (
            (D(str(data["price"])), data.get("pair_id")) if data.get("price") else None
        )
    return cache[snapshot_id]


def _later(s, asset_id: str, provenance: str, pair_id, target: datetime, horizon: int):
    tolerance = timedelta(seconds=900 if horizon <= 3600 else 3600)
    rows = s.scalars(
        select(SnapshotRow)
        .where(
            SnapshotRow.asset_id == asset_id,
            SnapshotRow.kind == "market",
            SnapshotRow.provenance == provenance,
            SnapshotRow.fetched_at >= target,
            SnapshotRow.fetched_at <= target + tolerance,
        )
        .order_by(SnapshotRow.fetched_at)
        .limit(5)
    )
    for row in rows:
        data = json.loads(row.payload)
        if data.get("pair_id") == pair_id and data.get("price"):
            return D(str(data["price"]))
    return None


def _summary(samples: list[tuple[str, Decimal]]) -> dict:
    values = [x[1] for x in samples]
    assets = {x[0] for x in samples}
    if not values:
        return {"n": 0, "assets": 0, "mean": None, "median": None, "hit_rate": None}
    return {
        "n": len(values),
        "assets": len(assets),
        "mean": q(sum(values) / len(values)),
        "median": q(median(values)),
        "hit_rate": _ratio(sum(v > HURDLE for v in values), len(values)),
        "sufficient": len(values) >= MIN_SAMPLES and len(assets) >= MIN_ASSETS,
    }


def gate_regret(db: Database, account: Account, since: datetime, now: datetime) -> list[dict]:
    """What the market did after each gate decision, by gate outcome and reason."""
    with db.transaction() as s:
        decisions = list(
            s.scalars(
                select(Decision)
                .where(Decision.account_id == account.id, Decision.created_at >= since)
                .order_by(Decision.created_at.desc())
                .limit(DECISION_LIMIT)
            )
        )
    groups: dict[tuple[bool, str], dict] = defaultdict(
        lambda: {"decisions": 0, "returns": {h: [] for h in HORIZONS}}
    )
    cache: dict = {}
    for start in range(0, len(decisions), CHUNK):
        with db.transaction() as s:  # Short transactions keep the SQLite writer lock free.
            for decision in decisions[start : start + CHUNK]:
                group = groups[(decision.allowed, decision.reason)]
                group["decisions"] += 1
                base = _base(s, decision.snapshot_id, cache)
                if base is None:
                    continue
                price, pair_id = base
                for horizon in HORIZONS:
                    target = decision.created_at + timedelta(seconds=horizon)
                    if target > now:
                        continue
                    later = _later(
                        s, decision.asset_id, account.provenance, pair_id, target, horizon
                    )
                    if later is not None:
                        group["returns"][horizon].append((decision.asset_id, later / price - 1))
    return sorted(
        (
            {
                "allowed": allowed,
                "reason": reason,
                "decisions": group["decisions"],
                "horizons": {str(h): _summary(group["returns"][h]) for h in HORIZONS},
            }
            for (allowed, reason), group in groups.items()
        ),
        key=lambda x: (-x["decisions"], x["reason"]),
    )


def _score(samples: list[tuple[Decimal, int, Decimal | None]]) -> dict:
    n = len(samples)
    if not n:
        return {"n": 0, "status": "insufficient_data"}
    base_rate = D(sum(y for _, y, _ in samples)) / n
    result = {
        "n": n,
        "status": "scored" if n >= MIN_RESOLVED else "insufficient_data",
        "brier": q(sum((p - y) ** 2 for p, y, _ in samples) / n),
        "base_rate": q(base_rate),
        "base_rate_brier": q(base_rate * (1 - base_rate)),
        "mean_probability": q(sum(p for p, _, _ in samples) / n),
    }
    if all(m is not None for _, _, m in samples):
        result["market_brier"] = q(sum((m - y) ** 2 for _, y, m in samples) / n)
    bins: dict[int, list] = defaultdict(list)
    for p, y, _ in samples:
        bins[min(int(p * 5), 4)].append((p, y))
    result["bins"] = [
        {
            "range": f"{index / 5:.1f}-{(index + 1) / 5:.1f}",
            "n": len(items),
            "mean_probability": q(sum(p for p, _ in items) / len(items)),
            "frequency": q(D(sum(y for _, y in items)) / len(items)),
        }
        for index, items in sorted(bins.items())
    ]
    return result


def _book_samples(db: Database, account_id: str) -> tuple[list, int]:
    samples, pending = [], 0
    with db.transaction() as s:
        records = s.scalars(
            select(AgentRecord)
            .join(Cycle, Cycle.id == AgentRecord.cycle_id)
            .where(
                Cycle.account_id == account_id,
                AgentRecord.role == "BOOK",
                AgentRecord.status == "passed",
            )
            .order_by(AgentRecord.id.desc())
            .limit(1000)
        )
        for record in records:
            payload = json.loads(record.payload)
            if payload.get("abstain") or payload.get("probability_yes") is None:
                continue
            order = s.scalar(
                select(PaperOrder).where(
                    PaperOrder.cycle_id == record.cycle_id,
                    PaperOrder.account_id == account_id,
                    PaperOrder.side == "buy",
                    PaperOrder.quantity > 0,
                )
            )
            if order is None:
                continue  # No entered position, so no market to resolve against.
            resolution = s.scalar(
                select(SnapshotRow).where(
                    SnapshotRow.asset_id == order.asset_id,
                    SnapshotRow.kind == "prediction_resolution",
                )
            )
            if resolution is None:
                pending += 1
                continue
            payout = D(str(json.loads(resolution.payload)["payout"]))
            held_yes = order.asset_id.rsplit(":", 1)[-1] == "yes"
            outcome_yes = 1 if (payout == 1) == held_yes else 0
            paid = order.notional / order.quantity
            samples.append(
                (D(str(payload["probability_yes"])), outcome_yes, paid if held_yes else 1 - paid)
            )
    return samples, pending


def _vet_samples(db: Database, account_id: str) -> tuple[list, int]:
    samples, pending = [], 0
    with db.transaction() as s:
        records = s.scalars(
            select(AgentRecord)
            .join(Cycle, Cycle.id == AgentRecord.cycle_id)
            .where(
                Cycle.account_id == account_id,
                AgentRecord.role == "VET",
                AgentRecord.status == "passed",
            )
            .order_by(AgentRecord.id.desc())
            .limit(1000)
        )
        for record in records:
            payload = json.loads(record.payload)
            if payload.get("win_probability") is None:
                continue
            decision = s.scalar(
                select(Decision).where(
                    Decision.cycle_id == record.cycle_id,
                    Decision.account_id == account_id,
                    Decision.allowed.is_(True),
                    Decision.snapshot_id.in_(payload.get("evidence", [])),
                )
            )
            if decision is None:
                continue
            episode = s.get(
                TradeEpisode,
                stable_hash({"cycle": record.cycle_id, "asset": decision.asset_id, "side": "buy"}),
            )
            if episode is None:
                continue
            if episode.closed_at is None:
                pending += 1
                continue
            samples.append(
                (D(str(payload["win_probability"])), int(episode.realized_pnl > 0), None)
            )
    return samples, pending


def calibration(db: Database, account_id: str) -> dict:
    book, book_pending = _book_samples(db, account_id)
    vet, vet_pending = _vet_samples(db, account_id)
    return {
        "BOOK": _score(book) | {"unresolved": book_pending, "baseline": "market-implied price"},
        "VET": _score(vet) | {"unresolved": vet_pending, "baseline": "own base rate"},
    }


def evaluate(
    db: Database,
    account_id: str,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> dict:
    now = now or utcnow()
    since = now - timedelta(days=lookback_days)
    with db.transaction() as s:
        account = s.get(Account, account_id)
        if account is None:
            raise ValueError("unknown account")
    return {
        "account_id": account_id,
        "provenance": account.provenance,
        "generated_at": now.isoformat(),
        "window_days": lookback_days,
        "paper_only": True,
        "policy_version": POLICY_VERSION,
        "automatic_policy_changes": False,
        "round_trip_hurdle": q(HURDLE),
        "reliability": reliability(db, since),
        "funnel": funnel(db, account_id, since),
        "gate_regret": gate_regret(db, account, since, now),
        "calibration": calibration(db, account_id),
        "notes": NOTES,
    }


def derive_proposals(report: dict) -> list[dict]:
    found = []
    for role, item in report["reliability"].items():
        rate = D(item["pass_rate"]) if item["pass_rate"] is not None else None
        if item["attempts"] >= 10 and rate is not None and rate < D("0.8"):
            failures = Counter({k: v for k, v in item["statuses"].items() if k != "passed"})
            top = failures.most_common(1)[0][0]
            found.append(
                {
                    "key": f"model-reliability:{role}",
                    "kind": "MODEL_RELIABILITY",
                    "severity": "warn",
                    "title": f"{role} output is accepted in {rate:.0%} of {item['attempts']} "
                    f"attempts, mostly failing as {top}",
                    "action": "Read the `detail` field on blocked cycle results to see which "
                    "fields fail. If it persists, review the schema and prompt, or change the "
                    "single global LLM_MODEL, restart every process and run a probe.",
                    "evidence": item,
                }
            )
    regret = {(x["allowed"], x["reason"]): x for x in report["gate_regret"]}
    total = report["funnel"]["decisions"]
    for group in report["funnel"]["groups"]:
        share = D(group["share"]) if group["share"] else D(0)
        if (
            not group["allowed"]
            and group["reason"] in DATA_GAP_REASONS
            and total >= MIN_SAMPLES
            and share >= D("0.25")
        ):
            found.append(
                {
                    "key": f"data-coverage:{group['reason']}",
                    "kind": "DATA_COVERAGE",
                    "severity": "warn",
                    "title": f"{share:.0%} of {total} decisions were blocked by "
                    f"{group['reason']}, a missing observation",
                    "action": "Supply this observation from a verified source, for example "
                    "MARKET_DATA_FEED_URL. Do not relax the gate: the original strategy "
                    "requires it.",
                    "evidence": group
                    | {
                        "forward_24h": regret.get((False, group["reason"]), {})
                        .get("horizons", {})
                        .get(str(PRIMARY_HORIZON))
                    },
                }
            )
    for (allowed, reason), item in regret.items():
        outcome = item["horizons"][str(PRIMARY_HORIZON)]
        if not outcome.get("sufficient"):
            continue
        mean = D(outcome["mean"])
        if not allowed and reason not in DATA_GAP_REASONS and mean > HURDLE:
            title = (
                f"Gate {reason} rejected candidates that moved {mean:+.1%} on average after "
                f"24h, above the {HURDLE:.1%} round-trip hurdle"
            )
            action = "Review whether this threshold is too strict. This is correlation in a "
            action += "small, overlapping sample; test it in a replay before any policy change."
        elif allowed and mean < 0:
            title = f"Entries allowed by {reason} fell {mean:+.1%} on average after 24h"
            action = "Review whether the entry gates are too loose or the exit is too slow."
        else:
            continue
        found.append(
            {
                "key": f"gate-review:{int(allowed)}:{reason}",
                "kind": "GATE_REVIEW",
                "severity": "info",
                "title": title,
                "action": action,
                "evidence": item,
            }
        )
    book = report["calibration"]["BOOK"]
    if (
        book["status"] == "scored"
        and "market_brier" in book
        and D(book["brier"]) > D(book["market_brier"])
    ):
        found.append(
            {
                "key": "calibration:BOOK",
                "kind": "CALIBRATION",
                "severity": "warn",
                "title": f"BOOK probabilities (Brier {book['brier']}) are less accurate than "
                f"the market prices they bet against ({book['market_brier']}, n={book['n']})",
                "action": "Do not increase BOOK exposure. Consider requiring a larger edge "
                "than 8 points, or pausing BOOK, until the model beats the market.",
                "evidence": book,
            }
        )
    vet = report["calibration"]["VET"]
    if vet["status"] == "scored" and D(vet["brier"]) > D(vet["base_rate_brier"]):
        found.append(
            {
                "key": "calibration:VET",
                "kind": "CALIBRATION",
                "severity": "warn",
                "title": f"VET win probabilities (Brier {vet['brier']}) are no better than "
                f"always predicting the base rate ({vet['base_rate_brier']}, n={vet['n']})",
                "action": "VET probabilities feed Kelly sizing. Treat sizing as uninformed and "
                "consider a lower position cap until calibration improves.",
                "evidence": vet,
            }
        )
    return found


def proposals(db: Database, account_id: str) -> list[dict]:
    with db.transaction() as s:
        rows = s.scalars(
            select(ImprovementProposal)
            .where(ImprovementProposal.account_id == account_id)
            .order_by(ImprovementProposal.last_seen_at.desc(), ImprovementProposal.id)
        )
        return [as_dict(x) | {"evidence": json.loads(x.evidence)} for x in rows]


def review(
    db: Database,
    account_id: str,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> dict:
    """Measure, store the report, and keep the proposal list in step with what is observed."""
    now = now or utcnow()
    report = evaluate(db, account_id, now, lookback_days)
    found = derive_proposals(report)
    created = []
    with db.transaction() as s:
        account = lock_account(s, account_id)
        stored = s.get(LearningReport, account_id)
        if stored is None:
            stored = LearningReport(account_id=account_id)
            s.add(stored)
        stored.generated_at, stored.report = now, json.dumps(report, default=str)
        seen = set()
        for item in found:
            proposal_id = stable_hash({"account_id": account_id, "key": item["key"]})[:32]
            seen.add(proposal_id)
            row = s.get(ImprovementProposal, proposal_id)
            evidence = json.dumps(item["evidence"], default=str)
            if row is None:
                s.add(
                    ImprovementProposal(
                        id=proposal_id,
                        account_id=account_id,
                        kind=item["kind"],
                        severity=item["severity"],
                        title=item["title"],
                        action=item["action"],
                        evidence=evidence,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                created.append(item["key"])
                continue
            row.title, row.action, row.evidence = item["title"], item["action"], evidence
            row.severity, row.last_seen_at, row.occurrences = (
                item["severity"],
                now,
                row.occurrences + 1,
            )
            if row.status == "withdrawn":
                row.status = "proposed"
        for row in s.scalars(
            select(ImprovementProposal).where(
                ImprovementProposal.account_id == account_id,
                ImprovementProposal.status == "proposed",
            )
        ):
            if row.id not in seen:
                row.status = "withdrawn"  # The condition cleared; a person decision is untouched.
        if created:
            audit(s, account, "LEARNING_PROPOSALS_CREATED", {"keys": created})
    return {
        "report": report,
        "proposals": proposals(db, account_id),
        "created": created,
    }


def latest(db: Database, account_id: str) -> dict:
    with db.transaction() as s:
        stored = s.get(LearningReport, account_id)
        report = json.loads(stored.report) if stored else None
    return {"report": report, "proposals": proposals(db, account_id)}


def set_status(db: Database, account_id: str, proposal_id: str, status: str, note: str) -> dict:
    if status not in REVIEW_STATUSES:
        raise ValueError("invalid proposal status")
    with db.transaction() as s:
        account = lock_account(s, account_id)
        row = s.get(ImprovementProposal, proposal_id)
        if row is None or row.account_id != account_id:
            raise LookupError("unknown proposal")
        audit(
            s,
            account,
            "LEARNING_PROPOSAL_REVIEWED",
            {"proposal_id": proposal_id, "from": row.status, "to": status, "note": note},
        )
        row.status = status
        return as_dict(row)
