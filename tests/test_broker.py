from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from trading_team.broker import MARK_MAX_AGE_SECONDS, PaperBroker
from trading_team.domain import Level
from trading_team.metrics import performance
from trading_team.storage import Account, LedgerEntry, PaperOrder, Position


def buy(db, snapshot, order_id="buy", requested=D(60)):
    return PaperBroker(db).execute(
        order_id=order_id,
        cycle_id="test",
        account_id="paper",
        snapshot=snapshot,
        quote=snapshot.buy_quote,
        side="buy",
        requested=requested,
        now=snapshot.fetched_at,
    )


def test_fill_idempotency_persists_and_ledger_reconciles(db, snapshot):
    first = buy(db, snapshot)
    assert first["status"] == "filled" and D(first["quantity"]) == D(".6")
    assert buy(db, snapshot) == first
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntry)) == 1
        assert s.get(Account, "paper").cash == D("939.05")
    assert all(PaperBroker(db).integrity("paper").values())


def test_concurrent_duplicate_is_one_cash_mutation(db, snapshot):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: buy(db, snapshot), range(2)))
    assert results[0] == results[1]
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntry)) == 1
    assert PaperBroker(db).integrity("paper")["cash_matches_ledger"]


def test_atomic_rollback(db, snapshot, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("injected failure after ledger mutation")

    monkeypatch.setattr("trading_team.broker.audit", fail)
    with pytest.raises(RuntimeError):
        buy(db, snapshot)
    with db.transaction() as s:
        assert s.get(Account, "paper").cash == 1000
        assert s.scalar(select(func.count()).select_from(PaperOrder)) == 0
        assert s.scalar(select(func.count()).select_from(LedgerEntry)) == 0
        assert s.scalar(select(func.count()).select_from(Position)) == 0


def test_partial_depth_and_fee_floor(db, snapshot):
    quote = snapshot.buy_quote.model_copy(update={"levels": [Level(price="100", quantity=".55")]})
    partial = snapshot.model_copy(update={"buy_quote": quote})
    order = buy(db, partial)
    assert order["status"] == "partial" and D(order["notional"]) == 55
    assert D(order["fee"]) == D(".95")
    assert order["reason"] == "PAPER_IOC_PARTIAL_REMAINDER_CANCELLED"
    assert PaperBroker(db).integrity("paper")["positions_match_ledger"]


def test_tiny_partial_not_faked_as_full(db, snapshot):
    quote = snapshot.buy_quote.model_copy(update={"levels": [Level(price="100", quantity=".2")]})
    result = buy(db, snapshot.model_copy(update={"buy_quote": quote}))
    assert result["reason"] == "FEE_FLOOR" and D(result["quantity"]) == 0


def test_slippage_and_minimum(db, snapshot):
    quote = snapshot.buy_quote.model_copy(
        update={"levels": [Level(price="102.00001", quantity="1")]}
    )
    assert (
        buy(db, snapshot.model_copy(update={"buy_quote": quote}))["reason"]
        == "NO_EXECUTABLE_DEPTH_WITHIN_SLIPPAGE"
    )
    assert buy(db, snapshot, "small", D(20))["reason"] == "MINIMUM_ENTRY_50_USD"
    assert buy(db, snapshot, "large", D(61))["reason"] == "POSITION_CAP_6_PERCENT"


def test_unknown_depth_stale_provenance_binding_and_kill(db, snapshot):
    broker = PaperBroker(db)
    assert (
        buy(db, snapshot.model_copy(update={"buy_quote": None}))["reason"]
        == "EXECUTION_DEPTH_UNAVAILABLE"
    )
    assert (
        buy(db, snapshot.model_copy(update={"observed_at": None}), "stale")["reason"]
        == "MARKET_DATA_STALE"
    )
    assert (
        buy(db, snapshot.model_copy(update={"provenance": "live"}), "wrong")["reason"]
        == "PROVENANCE_MISMATCH"
    )
    changed = snapshot.buy_quote.model_copy(update={"source_reference": "unbound"})
    assert (
        broker.execute(
            order_id="unbound",
            cycle_id="t",
            account_id="paper",
            snapshot=snapshot,
            quote=changed,
            side="buy",
            requested=D(60),
            now=snapshot.fetched_at,
        )["reason"]
        == "QUOTE_NOT_BOUND_TO_SNAPSHOT"
    )
    broker.kill("paper", True, "test")
    assert buy(db, snapshot, "killed")["reason"] == "KILL_SWITCH_ACTIVE"


def test_risk_exit_fee_and_closed_trade_metrics(db, snapshot, case):
    buy(db, snapshot)
    closing = case.events[1].snapshots[0]
    result = PaperBroker(db).execute(
        order_id="sell",
        cycle_id="risk",
        account_id="paper",
        snapshot=closing,
        quote=closing.sell_quote,
        side="sell",
        requested=D(".6"),
        now=closing.fetched_at,
    )
    assert D(result["realized_pnl"]) == D("4.1")
    metrics = performance(db, "paper", now=closing.fetched_at)
    assert metrics["closed_trades"] == 1 and D(metrics["win_rate"]) == 1
    assert D(metrics["cash"]) == D("1004.1")
    assert metrics["profit_factor"] is None
    assert all(PaperBroker(db).integrity("paper").values())


def test_stale_equity_never_zero_filled(db, snapshot):
    buy(db, snapshot)
    # Marks stay valid for two 5-minute RISK runs, so one run's delay does not unprice equity.
    between_runs = performance(db, "paper", now=snapshot.fetched_at + timedelta(seconds=400))
    assert between_runs["equity"] is not None
    metrics = performance(
        db, "paper", now=snapshot.fetched_at + timedelta(seconds=MARK_MAX_AGE_SECONDS + 1)
    )
    assert metrics["equity"] is None and metrics["net_pnl"] is None
    assert metrics["cash"] is not None


def test_add_on_once_after_peak_and_pullback(db, snapshot):
    broker = PaperBroker(db)
    buy(db, snapshot)
    assert (
        buy(db, snapshot, "tooearly")["reason"] == "POSITION_CAP_6_PERCENT"
    )  # Equity already includes the entry fee.
    peak = snapshot.model_copy(update={"price": D(160)})
    broker.mark_and_request_close(
        "paper", snapshot.asset_id, peak, snapshot.fetched_at, False, "HOLD"
    )
    quote = snapshot.buy_quote.model_copy(
        update={"reference_price": D(140), "levels": [Level(price="140", quantity="1")]}
    )
    pulled = snapshot.model_copy(update={"price": D(140), "buy_quote": quote})
    added = buy(db, pulled, "add", D(51))
    assert added["status"] == "filled"
    assert buy(db, pulled, "secondadd", D(51))["reason"] == "ADD_ON_NOT_PERMITTED"


def test_decimal_small_price_exact_roundtrip(db, snapshot):
    price = D(".0000037")
    quote = snapshot.buy_quote.model_copy(
        update={"reference_price": price, "levels": [Level(price=price, quantity="100000000")]}
    )
    sample = snapshot.model_copy(update={"price": price, "buy_quote": quote})
    result = buy(db, sample)
    assert D(result["notional"]) == D(result["quantity"]) * price
    assert D(result["notional"]) <= 60
    assert all(PaperBroker(db).integrity("paper").values())


def test_paper_initial_cash_applies_only_to_a_new_main_ledger(db, tmp_path):
    from trading_team.storage import Database

    db.initialize(D("5000"))
    with db.transaction() as s:
        assert s.get(Account, "main").cash == s.get(Account, "main").initial_cash == 1000
    fresh = Database(f"sqlite:///{tmp_path}/fresh.sqlite3")
    fresh.initialize(D("5000"))
    with fresh.transaction() as s:
        assert s.get(Account, "main").cash == s.get(Account, "main").initial_cash == 5000
    assert all(PaperBroker(fresh).integrity("main").values())
    fresh.engine.dispose()
    with pytest.raises(ValueError):
        db.initialize(D("0"))


def test_paper_initial_cash_setting_is_bounded():
    from pydantic import ValidationError

    from trading_team.settings import Settings

    assert Settings(_env_file=None, paper_initial_cash="2500").paper_initial_cash == D("2500")
    assert Settings(_env_file=None).public()["paper_initial_cash"] == "1000"
    for value in ("0", "-1", "NaN", "100000001"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, paper_initial_cash=value)
