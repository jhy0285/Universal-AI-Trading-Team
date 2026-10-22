from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal, localcontext

from sqlalchemy import select

from trading_team.domain import ExecutionQuote, MarketSnapshot, PredictionResolution, stable_hash
from trading_team.policy import MIN_ORDER, POSITION_CAP, SLIPPAGE_CAP, fee
from trading_team.storage import (
    Account,
    AuditEvent,
    Cycle,
    Database,
    EquityPoint,
    GlobalConfig,
    LedgerEntry,
    PaperFill,
    PaperOrder,
    Position,
    QuoteUse,
    SnapshotRow,
    TradeEpisode,
    as_dict,
    audit,
    lock_account,
)

D = Decimal
STEP = D("0.000000001")


class PaperBroker:
    """The sole execution implementation. No live broker, wallet, signing or exchange credentials."""

    def __init__(self, database: Database, quote_max_age: int = 60):
        self.db = database
        self.quote_max_age = quote_max_age

    @staticmethod
    def equity(session, account: Account, now: datetime) -> Decimal | None:
        equity = account.cash
        for p in session.scalars(select(Position).where(Position.account_id == account.id)):
            if p.quantity <= 0:
                continue
            if (
                p.last_price is None
                or p.marked_at is None
                or not 0 <= (now - p.marked_at).total_seconds() <= 300
            ):
                return None
            equity += p.quantity * p.last_price
        return equity

    def save_snapshot(self, snapshot: MarketSnapshot):
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        insert = sqlite_insert if self.db.engine.dialect.name == "sqlite" else pg_insert
        with self.db.transaction() as s:
            s.execute(
                insert(SnapshotRow)
                .values(
                    id=snapshot.digest,
                    asset_id=snapshot.asset_id,
                    source=snapshot.source,
                    kind="market",
                    provenance=snapshot.provenance,
                    fetched_at=snapshot.fetched_at,
                    observed_at=snapshot.observed_at,
                    payload=snapshot.model_dump_json(),
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )

    def execute(
        self,
        *,
        order_id: str,
        cycle_id: str,
        account_id: str,
        snapshot: MarketSnapshot,
        quote: ExecutionQuote | None,
        side: str,
        requested: Decimal,
        now: datetime,
        market_kind: str = "meme",
    ) -> dict:
        if side not in {"buy", "sell"} or requested <= 0 or not requested.is_finite():
            raise ValueError("invalid paper order")
        self.save_snapshot(snapshot)
        with self.db.transaction() as s:
            account = lock_account(s, account_id)
            old = s.get(PaperOrder, order_id)
            if old:
                if (
                    old.account_id != account_id
                    or old.asset_id != snapshot.asset_id
                    or old.side != side
                    or old.requested != requested
                ):
                    raise ValueError("idempotency key reused for a different order")
                return as_dict(old)
            order = PaperOrder(
                id=order_id,
                account_id=account_id,
                cycle_id=cycle_id,
                asset_id=snapshot.asset_id,
                side=side,
                requested=requested,
                status="rejected",
                reason="UNVALIDATED",
                quantity=D("0"),
                notional=D("0"),
                fee=D("0"),
                realized_pnl=D("0"),
                created_at=now,
            )
            s.add(order)

            def reject(reason):
                order.reason = reason
                audit(s, account, "PAPER_ORDER_REJECTED", {"order_id": order_id, "reason": reason})
                s.flush()
                return as_dict(order)

            if account.provenance != snapshot.provenance or (
                quote and quote.provenance != account.provenance
            ):
                return reject("PROVENANCE_MISMATCH")
            if side == "buy" and account.killed:
                return reject("KILL_SWITCH_ACTIVE")
            cycle = s.get(Cycle, cycle_id)
            active = s.get(GlobalConfig, "global")
            if (
                side == "buy"
                and cycle
                and cycle.provider != "recorded-fixture"
                and active
                and active.revision != cycle.config_revision
            ):
                return reject("GLOBAL_CONFIGURATION_CHANGED")
            if not snapshot.fresh(now):
                return reject("MARKET_DATA_STALE")
            if quote is None:
                return reject("EXECUTION_DEPTH_UNAVAILABLE")
            if quote.asset_id != snapshot.asset_id or quote.side != side:
                return reject("QUOTE_ASSET_OR_SIDE_MISMATCH")
            embedded = snapshot.buy_quote if side == "buy" else snapshot.sell_quote
            if embedded is None or embedded.digest != quote.digest:
                return reject("QUOTE_NOT_BOUND_TO_SNAPSHOT")
            if (
                snapshot.price is None
                or abs(quote.reference_price - snapshot.price) / snapshot.price > SLIPPAGE_CAP
            ):
                return reject("QUOTE_REFERENCE_PRICE_MISMATCH")
            if not 0 <= (now - quote.observed_at).total_seconds() <= self.quote_max_age:
                return reject("QUOTE_STALE")
            order.quote_id = quote.digest
            position = s.scalar(
                select(Position).where(
                    Position.account_id == account_id, Position.asset_id == snapshot.asset_id
                )
            )
            if side == "buy":
                equity = self.equity(s, account, now)
                if equity is None:
                    return reject("PORTFOLIO_UNPRICED")
                if requested < MIN_ORDER:
                    return reject("MINIMUM_ENTRY_50_USD")
                if requested > equity * POSITION_CAP:
                    return reject("POSITION_CAP_6_PERCENT")
                if requested + fee(requested) > account.cash:
                    return reject("INSUFFICIENT_CASH")
                if market_kind == "meme" and (
                    snapshot.liquidity_usd is None
                    or snapshot.liquidity_usd < max(D("10000"), requested * 50)
                ):
                    return reject("FINAL_LIQUIDITY_RECHECK")
                if market_kind == "meme" and snapshot.large_holder_selling is not False:
                    return reject("LARGE_HOLDER_SALES_NOT_CLEAR")
                if market_kind == "prediction":
                    open_bets = sum(
                        1
                        for p in s.scalars(
                            select(Position).where(
                                Position.account_id == account_id,
                                Position.market_kind == "prediction",
                            )
                        )
                        if p.quantity > 0
                    )
                    if open_bets >= 7 or position and position.quantity > 0:
                        return reject("BOOK_POSITION_LIMIT_OR_EXISTING_BET")
                if position and position.quantity > 0:
                    if (
                        position.status != "open"
                        or position.add_count >= 1
                        or position.peak_price < position.initial_price * D("1.5")
                        or snapshot.price is None
                        or not position.initial_price < snapshot.price < position.peak_price
                    ):
                        return reject("ADD_ON_NOT_PERMITTED")
            elif position is None or position.quantity <= 0 or requested > position.quantity:
                return reject("INSUFFICIENT_POSITION")

            fills = []
            total_qty = D("0")
            notional = D("0")
            with localcontext() as ctx:
                ctx.prec = 50
                for i, level in enumerate(quote.levels):
                    price_slip = (
                        (level.price - quote.reference_price) / quote.reference_price
                        if side == "buy"
                        else (quote.reference_price - level.price) / quote.reference_price
                    )
                    if price_slip > SLIPPAGE_CAP:
                        break
                    usage_id = f"{account_id}:{quote.digest}:{i}"
                    use = s.get(QuoteUse, usage_id)
                    available = max(D("0"), level.quantity - (use.quantity if use else D("0")))
                    remaining = (
                        (requested - notional) / level.price
                        if side == "buy"
                        else requested - total_qty
                    )
                    amount = min(available, remaining).quantize(STEP, rounding=ROUND_DOWN)
                    if amount <= 0:
                        continue
                    fills.append((i, level.price, amount, usage_id, use))
                    total_qty += amount
                    notional += amount * level.price
            if total_qty <= 0:
                return reject("NO_EXECUTABLE_DEPTH_WITHIN_SLIPPAGE")
            trading_fee = fee(notional)
            if side == "buy" and (trading_fee / notional > D("0.02") or notional < MIN_ORDER):
                return reject("FEE_FLOOR")
            cash_delta = -notional - trading_fee if side == "buy" else notional - trading_fee
            if account.cash + cash_delta < 0:
                return reject("FEE_UNFUNDED")
            pnl = D("0")
            episode = s.scalar(
                select(TradeEpisode).where(
                    TradeEpisode.account_id == account_id,
                    TradeEpisode.asset_id == snapshot.asset_id,
                    TradeEpisode.closed_at.is_(None),
                )
            )
            if side == "buy":
                if position is None or position.quantity == 0:
                    episode = TradeEpisode(
                        id=order_id,
                        account_id=account_id,
                        asset_id=snapshot.asset_id,
                        opened_at=now,
                        realized_pnl=D("0"),
                    )
                    s.add(episode)
                if position is None:
                    position = Position(
                        account_id=account_id,
                        asset_id=snapshot.asset_id,
                        market_kind=market_kind,
                        quantity=D("0"),
                        cost_basis=D("0"),
                        initial_price=notional / total_qty,
                        peak_price=notional / total_qty,
                        initial_market_cap=snapshot.market_cap,
                        add_count=0,
                        realized_pnl=D("0"),
                    )
                    s.add(position)
                elif position.quantity > 0:
                    position.add_count += 1
                else:
                    position.initial_price = notional / total_qty
                    position.peak_price = position.initial_price
                    position.initial_market_cap = snapshot.market_cap
                    position.add_count = 0
                position.quantity += total_qty
                position.cost_basis += notional + trading_fee
                position.status = "open"
            else:
                removed_basis = (
                    position.cost_basis
                    if total_qty == position.quantity
                    else position.cost_basis * total_qty / position.quantity
                )
                pnl = notional - trading_fee - removed_basis
                position.quantity -= total_qty
                position.cost_basis -= removed_basis
                position.realized_pnl += pnl
                position.status = "closed" if position.quantity == 0 else "close_requested"
                if episode is None:
                    raise ValueError("missing trade episode; refusing inconsistent ledger mutation")
                episode.realized_pnl += pnl
                if position.quantity == 0:
                    episode.closed_at = now
            position.last_price = snapshot.price
            position.marked_at = snapshot.observed_at
            account.cash += cash_delta
            average_price = notional / total_qty
            order.quantity = total_qty
            order.notional = notional
            order.fee = trading_fee
            order.realized_pnl = pnl
            order.slippage = (
                (average_price - quote.reference_price) / quote.reference_price
                if side == "buy"
                else (quote.reference_price - average_price) / quote.reference_price
            )
            completed = (
                (requested - notional <= quote.levels[-1].price * STEP)
                if side == "buy"
                else total_qty == requested
            )
            order.status = "filled" if completed else "partial"
            order.reason = (
                "PAPER_IOC_FILLED" if completed else "PAPER_IOC_PARTIAL_REMAINDER_CANCELLED"
            )
            for index, price, amount, usage_id, use in fills:
                s.add(PaperFill(order_id=order_id, level_index=index, price=price, quantity=amount))
                if use is None:
                    s.add(QuoteUse(id=usage_id, quantity=amount))
                else:
                    use.quantity += amount
            s.add(
                LedgerEntry(
                    order_id=order_id,
                    account_id=account_id,
                    asset_id=snapshot.asset_id,
                    cash_delta=cash_delta,
                    quantity_delta=total_qty if side == "buy" else -total_qty,
                    fee=trading_fee,
                    realized_pnl=pnl,
                    created_at=now,
                )
            )
            s.flush()
            s.add(
                EquityPoint(
                    account_id=account_id,
                    observed_at=now,
                    cash=account.cash,
                    equity=self.equity(s, account, now),
                )
            )
            audit(
                s,
                account,
                "PAPER_FILL_COMMITTED",
                {
                    "order_id": order_id,
                    "side": side,
                    "snapshot_id": snapshot.digest,
                    "quote_id": quote.digest,
                    "quantity": str(total_qty),
                    "notional": str(notional),
                    "fee": str(trading_fee),
                    "cash_after": str(account.cash),
                    "status": order.status,
                },
            )
            s.flush()
            return as_dict(order)

    def settle_prediction(
        self, account_id: str, resolution: PredictionResolution, now: datetime
    ) -> dict | None:
        if not 0 <= (now - resolution.fetched_at).total_seconds() <= 60 or (
            resolution.resolved_at and resolution.resolved_at > now
        ):
            raise ValueError("prediction resolution is stale or future-dated")
        with self.db.transaction() as s:
            account = lock_account(s, account_id)
            if resolution.provenance != account.provenance:
                raise ValueError("resolution/account provenance mismatch")
            position = s.scalar(
                select(Position).where(
                    Position.account_id == account_id, Position.asset_id == resolution.asset_id
                )
            )
            if position is None or position.quantity <= 0:
                return None
            if position.market_kind != "prediction":
                raise ValueError("binary settlement is restricted to prediction positions")
            episode = s.scalar(
                select(TradeEpisode).where(
                    TradeEpisode.account_id == account_id,
                    TradeEpisode.asset_id == resolution.asset_id,
                    TradeEpisode.closed_at.is_(None),
                )
            )
            if episode is None:
                raise ValueError("missing trade episode")
            order_id = stable_hash({"settlement": episode.id, "account": account_id})
            quantity = position.quantity
            payout = quantity * resolution.payout
            pnl = payout - position.cost_basis
            account.cash += payout
            position.quantity, position.cost_basis, position.status = D(0), D(0), "closed"
            position.realized_pnl += pnl
            position.last_price, position.marked_at = resolution.payout, now
            episode.realized_pnl += pnl
            episode.closed_at = now
            order = PaperOrder(
                id=order_id,
                account_id=account_id,
                cycle_id="risk:resolution",
                asset_id=resolution.asset_id,
                side="sell",
                status="filled",
                reason="PAPER_BINARY_RESOLUTION",
                quote_id=resolution.digest,
                requested=quantity,
                quantity=quantity,
                notional=payout,
                fee=D(0),
                realized_pnl=pnl,
                slippage=None,
                created_at=now,
            )
            s.add(order)
            s.add(
                PaperFill(
                    order_id=order_id, level_index=0, quantity=quantity, price=resolution.payout
                )
            )
            s.add(
                LedgerEntry(
                    order_id=order_id,
                    account_id=account_id,
                    asset_id=resolution.asset_id,
                    cash_delta=payout,
                    quantity_delta=-quantity,
                    fee=D(0),
                    realized_pnl=pnl,
                    created_at=now,
                )
            )
            if not s.get(SnapshotRow, resolution.digest):
                s.add(
                    SnapshotRow(
                        id=resolution.digest,
                        asset_id=resolution.asset_id,
                        kind="prediction_resolution",
                        source=resolution.source,
                        provenance=resolution.provenance,
                        fetched_at=resolution.fetched_at,
                        observed_at=resolution.resolved_at,
                        payload=resolution.model_dump_json(),
                    )
                )
            s.flush()
            s.add(
                EquityPoint(
                    account_id=account_id,
                    observed_at=now,
                    cash=account.cash,
                    equity=self.equity(s, account, now),
                )
            )
            audit(
                s,
                account,
                "PAPER_PREDICTION_RESOLVED",
                {
                    "order_id": order_id,
                    "resolution_id": resolution.digest,
                    "quantity": str(quantity),
                    "payout_per_contract": str(resolution.payout),
                    "cash_delta": str(payout),
                    "fee_policy": "zero simulated redemption fee; entry trading fees already paid",
                    "source": resolution.source,
                    "source_reference": resolution.source_reference,
                },
            )
            s.flush()
            return as_dict(order)

    def mark_and_request_close(
        self,
        account_id: str,
        asset_id: str,
        snapshot: MarketSnapshot | None,
        now: datetime,
        close: bool,
        reason: str,
    ):
        with self.db.transaction() as s:
            account = lock_account(s, account_id)
            position = s.scalar(
                select(Position).where(
                    Position.account_id == account_id, Position.asset_id == asset_id
                )
            )
            if position is None or position.quantity <= 0:
                return
            fresh = snapshot is not None and snapshot.fresh(now) and snapshot.price is not None
            position.last_price = snapshot.price if fresh else None
            position.marked_at = snapshot.observed_at if fresh else None
            if fresh:
                position.peak_price = max(position.peak_price, snapshot.price)
            if close:
                position.status = (
                    "close_requested"
                    if fresh and snapshot.sell_quote
                    else "close_requested_unpriced"
                )
                audit(
                    s,
                    account,
                    "RISK_CLOSE_REQUESTED",
                    {"asset_id": asset_id, "reason": reason, "status": position.status},
                )
            s.flush()
            s.add(
                EquityPoint(
                    account_id=account_id,
                    observed_at=now,
                    cash=account.cash,
                    equity=self.equity(s, account, now),
                )
            )

    def kill(self, account_id: str, active: bool, reason: str):
        if not reason.strip():
            raise ValueError("reason required")
        with self.db.transaction() as s:
            account = lock_account(s, account_id)
            account.killed = active
            account.kill_reason = reason
            audit(
                s,
                account,
                "KILL_SWITCH_ENABLED" if active else "KILL_SWITCH_CLEARED",
                {"reason": reason},
            )

    def integrity(self, account_id: str) -> dict:
        with self.db.transaction() as s:
            account = s.get(Account, account_id)
            if account is None:
                raise ValueError("unknown account")
            cash = account.initial_cash
            quantities = {}
            for item in s.scalars(select(LedgerEntry).where(LedgerEntry.account_id == account_id)):
                cash += item.cash_delta
                quantities[item.asset_id] = (
                    quantities.get(item.asset_id, D("0")) + item.quantity_delta
                )
            actual = {
                p.asset_id: p.quantity
                for p in s.scalars(select(Position).where(Position.account_id == account_id))
            }
            previous = "0" * 64
            audit_ok = True
            for row in s.scalars(
                select(AuditEvent)
                .where(AuditEvent.account_id == account_id)
                .order_by(AuditEvent.id)
            ):
                expected = stable_hash(
                    {
                        "account_id": account_id,
                        "event_type": row.event_type,
                        "payload": row.payload,
                        "previous_hash": previous,
                    }
                )
                audit_ok = audit_ok and row.previous_hash == previous and row.event_hash == expected
                previous = row.event_hash
            return {
                "cash_matches_ledger": cash == account.cash,
                "positions_match_ledger": all(
                    actual.get(k, D("0")) == v for k, v in quantities.items()
                )
                and all(v == quantities.get(k, D("0")) for k, v in actual.items()),
                "audit_chain_valid": audit_ok,
                "paper_only": True,
            }
