from datetime import timedelta
from decimal import Decimal

from trading_team.broker import PaperBroker
from trading_team.operations import intraday_check
from trading_team.storage import Account


def test_intraday_strict_ten_percent_once_per_day(db, snapshot):
    at = snapshot.fetched_at
    assert intraday_check(db, "paper", at)["status"] == "baseline_recorded"
    with db.transaction() as session:
        session.get(Account, "paper").cash = Decimal("1100")
    assert not intraday_check(db, "paper", at)["notified"]
    with db.transaction() as session:
        session.get(Account, "paper").cash = Decimal("1100.01")
    assert intraday_check(db, "paper", at)["notified"]
    assert not intraday_check(db, "paper", at)["notified"]
    assert intraday_check(db, "paper", at + timedelta(days=1))["status"] == "baseline_recorded"


def test_reused_order_id_with_different_intent_rejected(db, snapshot):
    import pytest

    broker = PaperBroker(db)
    arguments = dict(
        order_id="same",
        cycle_id="t",
        account_id="paper",
        snapshot=snapshot,
        quote=snapshot.buy_quote,
        side="buy",
        now=snapshot.fetched_at,
    )
    broker.execute(**arguments, requested=Decimal(60))
    with pytest.raises(ValueError, match="idempotency key reused"):
        broker.execute(**arguments, requested=Decimal(50))
