from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator

from trading_team.domain import stable_hash, utcnow


class ExactDecimal(TypeDecorator):
    """Persist decimal text losslessly on PostgreSQL and SQLite; never round via binary float."""

    impl = String(120)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timezone-aware timestamp required")
        return value

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provenance: Mapped[str] = mapped_column(String(20), default="live")
    cash: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("1000"))
    initial_cash: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("1000"))
    killed: Mapped[bool] = mapped_column(default=False)
    kill_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SnapshotRow(Base):
    __tablename__ = "data_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(250), index=True)
    source: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(50), default="market")
    provenance: Mapped[str] = mapped_column(String(20))
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    payload: Mapped[str] = mapped_column(Text)


class Cycle(Base):
    __tablename__ = "cycles"
    id: Mapped[str] = mapped_column(String(150), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(50), default="running")
    config_revision: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(150))
    policy_version: Mapped[str] = mapped_column(String(100))
    provenance: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    result: Mapped[str] = mapped_column(Text, default="{}")


class AgentRecord(Base):
    __tablename__ = "agent_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(150), index=True)
    role: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(150))
    config_revision: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30))
    payload: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ModelUsage(Base):
    __tablename__ = "model_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(150), index=True)
    role: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(30))
    latency_ms: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(ExactDecimal, nullable=True)
    usage_basis: Mapped[str] = mapped_column(String(50), default="provider_reported")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CapabilityRow(Base):
    __tablename__ = "model_capabilities"
    revision: Mapped[str] = mapped_column(String(64), primary_key=True)
    verified: Mapped[bool] = mapped_column(default=False)
    tested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    result: Mapped[str] = mapped_column(Text)


class GlobalConfig(Base):
    __tablename__ = "global_configuration"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[str] = mapped_column(String(64))


class AutomationJob(Base):
    __tablename__ = "automation_jobs"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    config_revision: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    result: Mapped[str] = mapped_column(Text, default="{}")


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(150), index=True)
    account_id: Mapped[str] = mapped_column(String(100), index=True)
    asset_id: Mapped[str] = mapped_column(String(250))
    snapshot_id: Mapped[str] = mapped_column(String(64))
    allowed: Mapped[bool] = mapped_column()
    reason: Mapped[str] = mapped_column(String(150))
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_id", "asset_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(100), index=True)
    asset_id: Mapped[str] = mapped_column(String(250))
    market_kind: Mapped[str] = mapped_column(String(30), default="meme")
    quantity: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("0"))
    cost_basis: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("0"))
    initial_price: Mapped[Decimal] = mapped_column(ExactDecimal)
    peak_price: Mapped[Decimal] = mapped_column(ExactDecimal)
    initial_market_cap: Mapped[Decimal | None] = mapped_column(ExactDecimal, nullable=True)
    last_price: Mapped[Decimal | None] = mapped_column(ExactDecimal, nullable=True)
    marked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    add_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(50), default="open")
    realized_pnl: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("0"))


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(100), index=True)
    cycle_id: Mapped[str] = mapped_column(String(150))
    asset_id: Mapped[str] = mapped_column(String(250))
    side: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(String(150))
    quote_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested: Mapped[Decimal] = mapped_column(ExactDecimal)
    quantity: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("0"))
    notional: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("0"))
    fee: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("0"))
    slippage: Mapped[Decimal | None] = mapped_column(ExactDecimal, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PaperFill(Base):
    __tablename__ = "paper_fills"
    __table_args__ = (UniqueConstraint("order_id", "level_index"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(180), index=True)
    level_index: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[Decimal] = mapped_column(ExactDecimal)
    price: Mapped[Decimal] = mapped_column(ExactDecimal)


class QuoteUse(Base):
    __tablename__ = "quote_consumption"
    id: Mapped[str] = mapped_column(String(250), primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("0"))


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(180), unique=True)
    account_id: Mapped[str] = mapped_column(String(100), index=True)
    asset_id: Mapped[str] = mapped_column(String(250))
    cash_delta: Mapped[Decimal] = mapped_column(ExactDecimal)
    quantity_delta: Mapped[Decimal] = mapped_column(ExactDecimal)
    fee: Mapped[Decimal] = mapped_column(ExactDecimal)
    realized_pnl: Mapped[Decimal] = mapped_column(ExactDecimal)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(100), index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[str] = mapped_column(Text)
    previous_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Lease(Base):
    __tablename__ = "worker_leases"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner: Mapped[str] = mapped_column(String(150))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TradeEpisode(Base):
    __tablename__ = "trade_episodes"
    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(100), index=True)
    asset_id: Mapped[str] = mapped_column(String(250))
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal("0"))


class EquityPoint(Base):
    __tablename__ = "equity_points"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(100), index=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    cash: Mapped[Decimal] = mapped_column(ExactDecimal)
    equity: Mapped[Decimal | None] = mapped_column(ExactDecimal, nullable=True)


class DailyBaseline(Base):
    __tablename__ = "daily_baselines"
    id: Mapped[str] = mapped_column(String(150), primary_key=True)
    equity: Mapped[Decimal] = mapped_column(ExactDecimal)
    notified: Mapped[bool] = mapped_column(default=False)


class WorkerHealth(Base):
    __tablename__ = "worker_health"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_success: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_failure: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    restarts: Mapped[str] = mapped_column(Text, default="[]")
    state: Mapped[str] = mapped_column(String(40), default="unknown")
    reason: Mapped[str] = mapped_column(String(150), default="")


class ReplayRun(Base):
    __tablename__ = "replay_runs"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(100), unique=True)
    dataset_hash: Mapped[str] = mapped_column(String(64), index=True)
    policy_version: Mapped[str] = mapped_column(String(100))
    initial_cash: Mapped[Decimal] = mapped_column(ExactDecimal)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(150))
    mode: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite:///") and not url.endswith(":memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(url, **options)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _record):
                connection.isolation_level = None
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")

            @event.listens_for(self.engine, "begin")
            def begin_sqlite(connection):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self, initial_cash: Decimal = Decimal("1000")):
        # The paper balance applies only when the main ledger is first created; never rewritten.
        if not initial_cash.is_finite() or initial_cash <= 0:
            raise ValueError("invalid paper balance")
        Base.metadata.create_all(self.engine)
        with self.transaction() as session:
            if session.get(Account, "main") is None:
                session.add(
                    Account(
                        id="main",
                        provenance="live",
                        cash=initial_cash,
                        initial_cash=initial_cash,
                    )
                )

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.sessions.begin() as session:
            yield session

    def create_account(self, account_id: str, provenance: str, cash: Decimal = Decimal("1000")):
        if (
            not account_id
            or len(account_id) > 100
            or provenance not in {"live", "recorded", "fixture"}
            or not cash.is_finite()
            or cash <= 0
        ):
            raise ValueError("invalid paper account")
        with self.transaction() as s:
            if s.get(Account, account_id):
                raise ValueError("account already exists; refusing to overwrite its history")
            s.add(Account(id=account_id, provenance=provenance, cash=cash, initial_cash=cash))


def lock_account(session: Session, account_id: str) -> Account:
    account = session.scalar(select(Account).where(Account.id == account_id).with_for_update())
    if account is None:
        raise ValueError("unknown account")
    return account


def audit(session: Session, account: Account, event_type: str, payload: dict):
    # Callers hold the account row lock, serializing each account's hash chain.
    previous = session.scalar(
        select(AuditEvent)
        .where(AuditEvent.account_id == account.id)
        .order_by(AuditEvent.id.desc())
        .limit(1)
    )
    previous_hash = previous.event_hash if previous else "0" * 64
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = stable_hash(
        {
            "account_id": account.id,
            "event_type": event_type,
            "payload": text,
            "previous_hash": previous_hash,
        }
    )
    session.add(
        AuditEvent(
            account_id=account.id,
            event_type=event_type,
            payload=text,
            previous_hash=previous_hash,
            event_hash=digest,
        )
    )
    session.flush()


def as_dict(row) -> dict:
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = (
            str(value)
            if isinstance(value, Decimal)
            else value.isoformat()
            if isinstance(value, datetime)
            else value
        )
    return result
