from datetime import timedelta
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from trading_team.domain import PredictionPrice, PredictionQuestion, TransactionWindow
from trading_team.policy import (
    book_gate,
    fee,
    kelly_fraction,
    risk_decision,
    scan_gate,
    size_quote,
    vet_gate,
)
from trading_team.settings import Settings


@pytest.mark.parametrize(
    ("amount", "expected"), [("0", "0"), ("20", ".95"), ("60", ".95"), ("1000", "4.5")]
)
def test_fee(amount, expected):
    assert fee(D(amount)) == D(expected)


def test_kelly_and_cash_caps():
    assert kelly_fraction(D(".7"), D(2)) == D(".55")
    kwargs = {"probability": D(".7"), "payoff_ratio": D(2), "liquidity_usd": D(20000)}
    assert size_quote(cash=D(1000), equity=D(1000), **kwargs) == 60
    assert size_quote(cash=D(50), equity=D(1000), **kwargs) == 0
    assert size_quote(cash=D(51), equity=D(1000), **kwargs) == D("50.05")
    assert size_quote(cash=D(10), equity=D(1000), **kwargs) == 0
    assert (
        size_quote(
            cash=D(1000),
            equity=D(1000),
            probability=D(".2"),
            payoff_ratio=D(2),
            liquidity_usd=D(20000),
        )
        == 0
    )
    with pytest.raises(ValueError):
        kelly_fraction(D("1.01"), D(2))


@pytest.mark.parametrize(
    ("h6", "expected"), [("49.9999999", True), ("50", False), ("50.0000001", False)]
)
def test_risk_strict_boundary(snapshot, h6, expected):
    sample = snapshot.model_copy(update={"volume_h6": D(h6), "volume_h24": D(1000)})
    assert risk_decision(sample, sample.observed_at).allowed is expected


def test_source_risk_example(snapshot):
    sample = snapshot.model_copy(update={"volume_h6": D("167.75"), "volume_h24": D("4717.70")})
    result = risk_decision(sample, sample.observed_at)
    assert result.allowed
    assert D(result.details["avg_6h"]) == D("1179.425")
    assert D(result.details["ratio"]) < D(".2")


@pytest.mark.parametrize(
    "change", [{"volume_h24": D(0)}, {"volume_h6": None}, {"observed_at": None}]
)
def test_risk_missing_retries(snapshot, change):
    sample = snapshot.model_copy(update=change)
    assert not risk_decision(sample, snapshot.fetched_at, 2).allowed
    assert risk_decision(sample, snapshot.fetched_at, 3).allowed


@pytest.mark.parametrize(("share", "passes"), [(".05", True), (".0500001", False)])
def test_vet_concentration(snapshot, case, share, passes):
    sample = snapshot.model_copy(update={"largest_token_account_share": D(share)})
    analysis = (
        case.events[0].vet[snapshot.asset_id].model_copy(update={"evidence": [sample.digest]})
    )
    assert vet_gate(sample, analysis, D(60), sample.fetched_at).allowed is passes


@pytest.mark.parametrize(
    ("age", "liquidity", "expected"), [(29, 18000, False), (30, 18000, True), (29, 20000, True)]
)
def test_young_and_declining_liquidity(snapshot, case, age, liquidity, expected):
    sample = snapshot.model_copy(
        update={
            "pair_created_at": snapshot.fetched_at - timedelta(minutes=age),
            "liquidity_usd": D(liquidity),
        }
    )
    analysis = (
        case.events[0].vet[snapshot.asset_id].model_copy(update={"evidence": [sample.digest]})
    )
    assert vet_gate(sample, analysis, D(60), sample.fetched_at).allowed is expected


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"onchain_verified": False}, "ONCHAIN_UNAVAILABLE"),
        ({"holder_count": None}, "HOLDER_HISTORY_UNAVAILABLE"),
        ({"holder_count": 1000}, "PRICE_OUTRUNS_HOLDERS"),
        ({"contract_safe": None}, "CONTRACT_SAFETY_UNVERIFIED"),
        ({"windows": []}, "TWO_DISJOINT_WINDOWS_REQUIRED"),
        ({"large_holder_selling": True}, "LARGE_HOLDER_SELLING_WAIT"),
    ],
)
def test_vet_missing_or_reject(snapshot, case, changes, reason):
    sample = snapshot.model_copy(update=changes)
    analysis = (
        case.events[0].vet[snapshot.asset_id].model_copy(update={"evidence": [sample.digest]})
    )
    assert vet_gate(sample, analysis, D(60), sample.fetched_at).reason == reason


def test_partial_and_evidence_forgery_blocked(snapshot, case):
    analysis = case.events[0].vet[snapshot.asset_id]
    assert (
        vet_gate(
            snapshot,
            analysis.model_copy(update={"verdict": "PASS_PARTIAL"}),
            D(60),
            snapshot.fetched_at,
        ).reason
        == "PARTIAL_VET_BLOCKED"
    )
    assert (
        vet_gate(
            snapshot,
            analysis.model_copy(update={"evidence": ["invented"]}),
            D(60),
            snapshot.fetched_at,
        ).reason
        == "EVIDENCE_MISMATCH"
    )


def test_scan_sell_windows_and_unknown_holders(snapshot):
    at = snapshot.fetched_at
    windows = [
        TransactionWindow(
            start=at - timedelta(minutes=10), end=at - timedelta(minutes=5), buys=0, sells=1
        ),
        TransactionWindow(start=at - timedelta(minutes=5), end=at, buys=1, sells=2),
    ]
    assert (
        scan_gate(snapshot.model_copy(update={"windows": windows}), at).reason
        == "SELLS_DOMINATE_BOTH_WINDOWS"
    )
    assert scan_gate(snapshot.model_copy(update={"holder_count": None}), at).allowed
    assert not scan_gate(snapshot.model_copy(update={"observed_at": None}), at).allowed


@pytest.mark.parametrize(
    ("edge", "expected"), [(".08", False), (".0800001", True), (".0799999", False)]
)
def test_book_edge_boundary(snapshot, edge, expected):
    at = snapshot.fetched_at
    question = PredictionQuestion(
        question_id="q",
        title="question",
        resolution_rules="rules",
        closes_at=at + timedelta(days=1),
        evidence_observed_at=at,
        source_ids=["s"],
        evidence_text=["fact"],
    )
    price = PredictionPrice(
        question_id="q",
        venue="kalshi",
        venue_market_id="m",
        outcome="yes",
        ask=".5",
        observed_at=at,
        matching_verified=True,
        resolution_hash="hash",
    )
    assert book_gate(question, price, D(".5") + D(edge), now=at, open_bets=0).allowed is expected
    assert not book_gate(question, price, D(".9"), now=at, open_bets=7).allowed
    assert not book_gate(
        question, price, D(".9"), now=at, open_bets=0, losing_position=True
    ).allowed
    assert not book_gate(
        question.model_copy(update={"evidence_observed_at": at - timedelta(hours=48, seconds=1)}),
        price,
        D(".9"),
        now=at,
        open_bets=0,
    ).allowed


def test_configuration_is_single_and_paper_only(monkeypatch):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trading_mode="live")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worldmonitor_base_url="https://api.worldmonitor.app")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_base_url="https://api.example.com?api_key=secret")
    monkeypatch.setenv("VET_MODEL", "different-model")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_dotenv_agent_override_rejected(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("VET_MODEL=override\n")
    with pytest.raises(ValueError, match="per-agent"):
        Settings(_env_file=path)
