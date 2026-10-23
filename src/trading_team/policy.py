"""Pure deterministic rules. Interpretations are explicit in docs/implementation-decisions.md."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from trading_team.domain import (
    GateResult,
    MarketSnapshot,
    PredictionPrice,
    PredictionQuestion,
    VetOutput,
)

D = Decimal
ZERO = D("0")
FEE_RATE = D("0.0045")
FEE_FLOOR = D("0.95")
MIN_ORDER = D("50")
POSITION_CAP = D("0.06")
SLIPPAGE_CAP = D("0.02")
POLICY_VERSION = "paper-interpretation-v1"


def fee(notional: Decimal) -> Decimal:
    if notional <= 0:
        return ZERO
    return max(FEE_RATE * notional, FEE_FLOOR).quantize(D("0.00000001"))


def kelly_fraction(probability: Decimal, payoff_ratio: Decimal) -> Decimal:
    if (
        not ZERO <= probability <= 1
        or payoff_ratio <= 0
        or not probability.is_finite()
        or not payoff_ratio.is_finite()
    ):
        raise ValueError("invalid Kelly inputs")
    return max(ZERO, (payoff_ratio * probability - (1 - probability)) / payoff_ratio)


def size_quote(
    *,
    cash: Decimal,
    equity: Decimal,
    probability: Decimal,
    payoff_ratio: Decimal,
    liquidity_usd: Decimal,
) -> Decimal:
    if min(cash, equity, liquidity_usd) <= 0:
        return ZERO
    budget = min(
        equity * kelly_fraction(probability, payoff_ratio),
        equity * POSITION_CAP,
        liquidity_usd / 50,
        cash - FEE_FLOOR,
        cash / (1 + FEE_RATE),
    )
    budget = max(ZERO, budget).quantize(D("0.01"), rounding=ROUND_DOWN)
    return budget if budget >= MIN_ORDER and budget + fee(budget) <= cash else ZERO


def scan_gate(snapshot: MarketSnapshot, now: datetime, max_age: int = 300) -> GateResult:
    if snapshot.chain != "solana":
        return GateResult(allowed=False, reason="ORIGINAL_SOLANA_ONLY")
    if not snapshot.fresh(now, max_age):
        return GateResult(allowed=False, reason="STALE_OR_UNKNOWN_MARKET_TIME")
    if snapshot.liquidity_usd is None or snapshot.liquidity_usd < 10000:
        return GateResult(allowed=False, reason="SCAN_LIQUIDITY")
    if len(snapshot.windows) == 2 and all(w.sells > w.buys for w in snapshot.windows):
        return GateResult(allowed=False, reason="SELLS_DOMINATE_BOTH_WINDOWS")
    return GateResult(allowed=True, reason="SCAN_CANDIDATE")


def vet_gate(
    snapshot: MarketSnapshot, analysis: VetOutput, order_usd: Decimal, now: datetime
) -> GateResult:
    # Preserve original cheap-to-expensive rejection ordering.
    if not snapshot.onchain_verified or snapshot.largest_token_account_share is None:
        return GateResult(allowed=False, reason="ONCHAIN_UNAVAILABLE")
    if (
        snapshot.onchain_observed_at is None
        or not 0 <= (now - snapshot.onchain_observed_at).total_seconds() <= 300
    ):
        return GateResult(allowed=False, reason="ONCHAIN_STALE")
    if snapshot.largest_token_account_share > D("0.05"):
        return GateResult(allowed=False, reason="CONCENTRATION_OVER_5_PERCENT")
    if snapshot.liquidity_usd is None or snapshot.liquidity_usd < max(D("10000"), order_usd * 50):
        return GateResult(allowed=False, reason="VET_LIQUIDITY")
    if (
        snapshot.holder_count is None
        or snapshot.previous_holder_count is None
        or snapshot.previous_price is None
        or snapshot.holder_observed_at is None
        or snapshot.previous_holder_observed_at is None
    ):
        return GateResult(allowed=False, reason="HOLDER_HISTORY_UNAVAILABLE")
    if not 0 <= (now - snapshot.holder_observed_at).total_seconds() <= 300:
        return GateResult(allowed=False, reason="HOLDER_HISTORY_STALE")
    if snapshot.price is None:
        return GateResult(allowed=False, reason="PRICE_UNAVAILABLE")
    if (
        snapshot.price > snapshot.previous_price
        and snapshot.holder_count <= snapshot.previous_holder_count
    ):
        return GateResult(allowed=False, reason="PRICE_OUTRUNS_HOLDERS")
    if analysis.signal_priced_in:
        return GateResult(allowed=False, reason="SIGNAL_ALREADY_PRICED_IN")
    if analysis.uses_world_news and len(set(snapshot.news_source_ids)) < 2:
        return GateResult(allowed=False, reason="NEWS_NOT_CORROBORATED")
    if (
        analysis.expected_return_fraction is None
        or order_usd <= 0
        or analysis.expected_return_fraction * order_usd <= 2 * fee(order_usd)
    ):
        return GateResult(allowed=False, reason="ROUND_TRIP_FEES_ERASE_EDGE")
    if snapshot.pair_created_at is None or snapshot.previous_liquidity_usd is None:
        return GateResult(allowed=False, reason="PAIR_HISTORY_UNAVAILABLE")
    age = (now - snapshot.pair_created_at).total_seconds()
    if age < 0:
        return GateResult(allowed=False, reason="PAIR_TIME_IN_FUTURE")
    if age < 1800 and snapshot.liquidity_usd < snapshot.previous_liquidity_usd:
        return GateResult(allowed=False, reason="YOUNG_PAIR_LOSING_LIQUIDITY")
    if snapshot.contract_safe is not True or not snapshot.contract_source:
        return GateResult(allowed=False, reason="CONTRACT_SAFETY_UNVERIFIED")
    if len(snapshot.windows) != 2:
        return GateResult(allowed=False, reason="TWO_DISJOINT_WINDOWS_REQUIRED")
    if snapshot.large_holder_selling is None:
        return GateResult(allowed=False, reason="LARGE_HOLDER_SALES_UNKNOWN")
    if snapshot.large_holder_selling:
        return GateResult(allowed=False, reason="LARGE_HOLDER_SELLING_WAIT")
    if analysis.verdict != "PASS":
        return GateResult(
            allowed=False,
            reason="PARTIAL_VET_BLOCKED"
            if analysis.verdict == "PASS_PARTIAL"
            else "MODEL_VET_REJECT",
        )
    if snapshot.digest not in analysis.evidence:
        return GateResult(allowed=False, reason="EVIDENCE_MISMATCH")
    return GateResult(allowed=True, reason="VET_PASS")


def vet_precheck(snapshot: MarketSnapshot, now: datetime) -> GateResult:
    """The VET checks that need no model, in VET's own order, before spending model time.

    Model judgements are held neutral (passing), so the first failure is a data failure that
    VET would reject anyway. The full vet_gate still runs after the model, with the real size.
    """
    neutral = VetOutput(
        verdict="PASS",
        signal_priced_in=False,
        uses_world_news=False,
        win_probability=None,
        win_payoff_ratio=None,
        expected_return_fraction=D(1),
        evidence=[snapshot.digest],
        explanation="data-only precheck",
    )
    result = vet_gate(snapshot, neutral, MIN_ORDER, now)
    return GateResult(allowed=True, reason="VET_DATA_READY") if result.allowed else result


def risk_decision(snapshot: MarketSnapshot | None, now: datetime, attempts: int = 3) -> GateResult:
    valid = (
        snapshot is not None
        and snapshot.fresh(now)
        and snapshot.volume_h6 is not None
        and snapshot.volume_h24 is not None
        and snapshot.volume_h24 > 0
    )
    if not valid:
        return GateResult(
            allowed=attempts >= 3,
            reason="CLOSE_DATA_UNAVAILABLE" if attempts >= 3 else "RETRY_RISK_DATA",
        )
    ratio = snapshot.volume_h6 / (snapshot.volume_h24 / 4)
    close = 20 * snapshot.volume_h6 < snapshot.volume_h24
    return GateResult(
        allowed=close,
        reason="CLOSE_VOLUME_COLLAPSE" if close else "HOLD",
        details={"avg_6h": str(snapshot.volume_h24 / 4), "ratio": str(ratio)},
    )


def book_gate(
    question: PredictionQuestion,
    price: PredictionPrice,
    probability_yes: Decimal,
    *,
    now: datetime,
    open_bets: int,
    losing_position: bool = False,
) -> GateResult:
    if price.question_id != question.question_id or not price.matching_verified:
        return GateResult(allowed=False, reason="MARKET_MATCH_UNVERIFIED")
    if not price.resolution_hash:
        return GateResult(allowed=False, reason="RESOLUTION_UNVERIFIED")
    if not 0 <= (now - question.evidence_observed_at).total_seconds() <= 48 * 3600:
        return GateResult(allowed=False, reason="BOOK_EVIDENCE_OLDER_THAN_48_HOURS")
    if not 0 <= (now - price.observed_at).total_seconds() <= 60 or now >= question.closes_at:
        return GateResult(allowed=False, reason="BOOK_PRICE_STALE_OR_MARKET_CLOSED")
    if open_bets >= 7 or losing_position:
        return GateResult(allowed=False, reason="BOOK_POSITION_LIMIT_OR_AVERAGING_LOSER")
    if not ZERO <= probability_yes <= 1:
        return GateResult(allowed=False, reason="INVALID_PROBABILITY")
    p = probability_yes if price.outcome == "yes" else 1 - probability_yes
    edge = p - price.ask
    return GateResult(
        allowed=edge > D("0.08"),
        reason="BOOK_EDGE_PASS" if edge > D("0.08") else "BOOK_EDGE_NOT_OVER_8_POINTS",
        details={"edge": str(edge), "outcome_probability": str(p)},
    )
