from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from trading_team.broker import PaperBroker
from trading_team.data import (
    DataError,
    DexScreener,
    MarketDataFeed,
    PredictionMarkets,
    PublicDataClient,
    SolanaOnchain,
    WorldMonitor,
)
from trading_team.domain import (
    BookOutput,
    ChiefOutput,
    GateResult,
    MarketSnapshot,
    PredictionPrice,
    PredictionQuestion,
    ScanOutput,
    VetOutput,
    cited,
    stable_hash,
    utcnow,
)
from trading_team.llm import Attempt, LLMError, LLMRuntime
from trading_team.notify import Notifier, fill_text
from trading_team.observer import MarketObserver
from trading_team.operations import Supervisor, intraday_check
from trading_team.policy import (
    POLICY_VERSION,
    SLIPPAGE_CAP,
    book_gate,
    risk_decision,
    scan_gate,
    size_quote,
    vet_precheck,
)
from trading_team.policy import vet_gate as deterministic_vet
from trading_team.settings import Settings
from trading_team.storage import (
    Account,
    AgentRecord,
    CapabilityRow,
    Cycle,
    Database,
    Decision,
    GlobalConfig,
    ModelUsage,
    Position,
    SnapshotRow,
    as_dict,
    audit,
    lock_account,
)

D = Decimal


SCAN_TASK = (
    "Rank at most 10 candidates by derived.holder_growth_fraction, computed by Python. A "
    "candidate whose price grows faster than its holders must not rank above one whose holders "
    "grow faster. Cite each chosen candidate's evidence_id in evidence. No orders or sizes."
)
VET_TASK = (
    "Python has already verified this candidate's required observations; do not reject for "
    "missing data. Judge only what needs judgement: signal_priced_in (is the holder growth "
    "already reflected in price_change_h1_percent and derived.price_growth_fraction?), "
    "uses_world_news, and forecasts for a short holding period: win_probability, "
    "win_payoff_ratio (average win / average loss) and expected_return_fraction. Paper fees are "
    "max(0.45%, $0.95) per fill, paid on entry and exit. Forecasts are estimates, never observed "
    "facts; use null when you cannot estimate. Cite evidence_id in evidence."
)


def growth(current, before) -> str | None:
    if current is None or before is None or before <= 0:
        return None
    return str(((D(current) - D(before)) / D(before)).quantize(D("0.0001")))


def derived(snapshot: MarketSnapshot) -> dict:
    """Comparisons computed in Python, so the model ranks numbers instead of deriving them."""
    holders = growth(snapshot.holder_count, snapshot.previous_holder_count)
    price = growth(snapshot.price, snapshot.previous_price)
    current, before = snapshot.holder_observed_at, snapshot.previous_holder_observed_at
    return {
        "holder_growth_fraction": holders,
        "price_growth_fraction": price,
        "minutes_between_holder_observations": round((current - before).total_seconds() / 60, 1)
        if current and before
        else None,
        "holders_outpace_price": None
        if holders is None or price is None
        else D(holders) > D(price),
        "window_buys_sells": [[w.buys, w.sells] for w in snapshot.windows],
    }


def analysis_snapshot(snapshot: MarketSnapshot, *, scan: bool = False) -> dict:
    # The full immutable snapshot remains the deterministic execution evidence.
    # Model roles receive relevant observations without thousands of executable depth levels.
    if scan:
        # Ranking needs growth, not raw timestamps or windows: derived carries those compactly,
        # which keeps the prompt short enough for CPU models.
        fields = {
            "asset_id",
            "symbol",
            "price",
            "previous_price",
            "liquidity_usd",
            "holder_count",
            "previous_holder_count",
            "price_change_h1_percent",
        }
        return snapshot.model_dump(mode="json", include=fields) | {"derived": derived(snapshot)}
    data = snapshot.model_dump(mode="json", exclude={"buy_quote", "sell_quote"})
    data["derived"] = derived(snapshot)
    # Entry depth only: exit depth is quoted by RISK for the held quantity, and an absent
    # sell side read as "missing data" to the model.
    quote = snapshot.buy_quote
    data["execution_observations"] = {
        "buy": {
            "present": quote is not None,
            "observed_at": quote.observed_at.isoformat() if quote else None,
            "source": quote.source if quote else None,
        }
    }
    return data


class Desk:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        runtime: LLMRuntime | None = None,
        data: PublicDataClient | None = None,
    ):
        self.settings = settings
        self.db = db
        self.broker = PaperBroker(db, settings.quote_max_age_seconds)
        self.supervisor = Supervisor(db)
        self.runtime = runtime or LLMRuntime(settings, record_attempt=self.record_usage)
        self.runtime.record_attempt = self.record_usage
        self.runtime.configuration_guard = self.assert_config
        self.data = data or PublicDataClient(db)
        self.dex = DexScreener(self.data)
        self.market_feed = MarketDataFeed(settings, self.data)
        self.wm = WorldMonitor(settings, self.data)
        self.prediction = PredictionMarkets(self.data)
        self.solana = SolanaOnchain(settings, self.data)
        self.observer = MarketObserver(settings, db, self.data, self.dex, self.solana)
        self.notifier = Notifier(settings)
        self.supervisor.on_halt = lambda role, reason: self.notifier.send(
            f"데스크 신규 진입 중단: {role} 반복 실패 ({reason}). 해제는 Kill Switch 해제로만 가능"
        )

    def notify_fill(self, order: dict):
        text = fill_text(order)
        if text:
            self.notifier.send(text)

    async def close(self):
        await self.notifier.drain()
        await self.runtime.close()
        await self.data.close()

    def assert_config(self):
        with self.db.transaction() as s:
            lock_account(s, "main")
            active = s.get(GlobalConfig, "global")
            if active is None:
                s.add(GlobalConfig(id="global", revision=self.settings.revision))
            elif active.revision != self.settings.revision:
                raise LLMError("GLOBAL_CONFIGURATION_CHANGED_RESTART_ALL_PROCESSES")

    def activate_config(self):
        with self.db.transaction() as s:
            account = lock_account(s, "main")
            active = s.get(GlobalConfig, "global")
            if active is None:
                s.add(GlobalConfig(id="global", revision=self.settings.revision))
            elif active.revision != self.settings.revision:
                active.revision = self.settings.revision
                audit(s, account, "GLOBAL_CONFIGURATION_ACTIVATED", self.settings.public())

    def record_usage(self, attempt: Attempt):
        cost = None
        cfg = self.settings
        if (
            attempt.basis in {"provider_reported", "cli_api_usage"}
            and attempt.input_tokens is not None
            and attempt.output_tokens is not None
            and not attempt.cached_tokens
            and cfg.input_price_per_million is not None
            and cfg.output_price_per_million is not None
        ):
            cost = (
                D(attempt.input_tokens) * D(str(cfg.input_price_per_million))
                + D(attempt.output_tokens) * D(str(cfg.output_price_per_million))
            ) / 1_000_000
        with self.db.transaction() as s:
            s.add(
                ModelUsage(
                    cycle_id=attempt.cycle_id,
                    role=attempt.role,
                    provider=attempt.provider,
                    model=attempt.model,
                    status=attempt.status,
                    latency_ms=attempt.latency_ms,
                    input_tokens=attempt.input_tokens,
                    output_tokens=attempt.output_tokens,
                    cached_tokens=attempt.cached_tokens,
                    estimated_cost=cost,
                    usage_basis=attempt.basis,
                )
            )
            cycle = s.get(Cycle, attempt.cycle_id)
            # Model replays on fixture or recorded ledgers must not halt the live desk.
            live = cycle is None or cycle.provenance == "live"
        if (
            live
            and attempt.status != "CANCELLED"
            and self.supervisor.observe(attempt.role, attempt.status == "passed", attempt.status)
        ):
            self.runtime.restart_requested = True

    def record_agent(
        self, cycle_id: str, role: str, status: str, payload: dict, *, recorded: bool = False
    ):
        with self.db.transaction() as s:
            s.add(
                AgentRecord(
                    cycle_id=cycle_id,
                    role=role,
                    provider="recorded-fixture"
                    if recorded
                    else self.settings.llm_provider
                    if role in {"CHIEF", "SCAN", "VET", "BOOK"}
                    else "deterministic",
                    model="fixture-not-a-live-model"
                    if recorded
                    else self.settings.llm_model
                    if role in {"CHIEF", "SCAN", "VET", "BOOK"}
                    else POLICY_VERSION,
                    config_revision=self.settings.revision,
                    status=status,
                    payload=json.dumps(payload, default=str, ensure_ascii=False),
                    evidence=json.dumps(payload.get("evidence", [])),
                )
            )

    async def probe(self) -> dict:
        result = await self.runtime.probe()
        with self.db.transaction() as s:
            row = s.get(CapabilityRow, self.settings.revision)
            if row is None:
                row = CapabilityRow(revision=self.settings.revision)
                s.add(row)
            row.verified = result["verified"]
            row.tested_at = utcnow()
            row.result = json.dumps(result)
        return result

    def capabilities(self) -> dict:
        with self.db.transaction() as s:
            row = s.get(CapabilityRow, self.settings.revision)
            if not row:
                return {"verified": False, "reason": "MODEL_PROBE_REQUIRED"}
            value = json.loads(row.result)
            if (utcnow() - row.tested_at).total_seconds() > 24 * 3600:
                value.update(verified=False, reason="MODEL_PROBE_EXPIRED")
            return value

    async def collect(self, *, trades: bool = True) -> dict:
        """Discover pools and observe the most liquid ones.

        Trade windows are needed only when SCAN evaluates; the minute collection skips them to
        spare GeckoTerminal's shared keyless limit.
        """
        results = []
        errors = []
        discovered = {}
        if self.settings.dex_discovery in {"launches", "both"}:
            try:
                for snapshot in await self.dex.launches():
                    discovered.setdefault((snapshot.asset_id, snapshot.pair_id), snapshot)
            except DataError as exc:
                errors.append({"source": "dexscreener_launches", "reason": exc.code})
        if self.settings.dex_discovery in {"search", "both"}:
            queries = [q.strip() for q in self.settings.dex_queries.split(",") if q.strip()][:5]
            for query in queries:
                try:
                    for snapshot in await self.dex.search(query):
                        discovered.setdefault((snapshot.asset_id, snapshot.pair_id), snapshot)
                except DataError as exc:
                    errors.append({"query": query, "reason": exc.code})
        observed, notes = await self.observer.observe(list(discovered.values()), trades=trades)
        errors.extend(notes)
        try:
            observed = await self.market_feed.enrich(observed)
        except DataError as exc:
            errors.append({"source": "market_data_feed", "reason": exc.code})
        for snapshot in observed:
            self.broker.save_snapshot(snapshot)
            results.append(snapshot)
        return {
            "snapshots": [x.model_dump(mode="json") | {"snapshot_id": x.digest} for x in results],
            "errors": errors,
            "count": len(results),
        }

    def latest_snapshots(self, provenance: str = "live", limit: int = 100) -> list[MarketSnapshot]:
        with self.db.transaction() as s:
            rows = s.scalars(
                select(SnapshotRow)
                .where(SnapshotRow.kind == "market", SnapshotRow.provenance == provenance)
                .order_by(SnapshotRow.fetched_at.desc())
                .limit(limit * 4)
            ).all()
        selected = {}
        for row in rows:
            item = MarketSnapshot.model_validate_json(row.payload)
            key = (item.asset_id, item.pair_id)
            if key not in selected:
                selected[key] = item
        return list(selected.values())[:limit]

    def _begin_cycle(
        self, account_id: str, request_id: str, recorded: bool, now: datetime
    ) -> tuple[str, dict | None]:
        cycle_id = stable_hash({"account_id": account_id, "request_id": request_id})
        with self.db.transaction() as s:
            account = lock_account(s, account_id)
            old = s.get(Cycle, cycle_id)
            if old:
                return cycle_id, {
                    "cycle_id": cycle_id,
                    "status": old.status,
                    "result": json.loads(old.result),
                    "idempotent_replay": True,
                }
            if recorded and account.provenance == "live":
                raise ValueError("recorded responses cannot be used in a live-data account")
            s.add(
                Cycle(
                    id=cycle_id,
                    account_id=account_id,
                    status="running",
                    config_revision=self.settings.revision,
                    provider="recorded-fixture" if recorded else self.settings.llm_provider,
                    model="fixture-not-a-live-model" if recorded else self.settings.llm_model,
                    policy_version=POLICY_VERSION,
                    provenance=account.provenance,
                    created_at=now,
                )
            )
            audit(
                s,
                account,
                "CYCLE_STARTED",
                {
                    "cycle_id": cycle_id,
                    "config_revision": self.settings.revision,
                    "recorded": recorded,
                },
            )
        return cycle_id, None

    def _finish_cycle(self, cycle_id: str, result: dict, now: datetime) -> dict:
        with self.db.transaction() as s:
            row = s.get(Cycle, cycle_id)
            row.status = result["status"]
            row.completed_at = now
            row.result = json.dumps(result, default=str)
        return {"cycle_id": cycle_id, **result}

    def _decision(self, cycle_id: str, account_id: str, snapshot: MarketSnapshot, gate: GateResult):
        with self.db.transaction() as s:
            account = lock_account(s, account_id)
            s.add(
                Decision(
                    cycle_id=cycle_id,
                    account_id=account_id,
                    asset_id=snapshot.asset_id,
                    snapshot_id=snapshot.digest,
                    allowed=gate.allowed,
                    reason=gate.reason,
                    details=json.dumps(gate.details, default=str),
                )
            )
            audit(
                s,
                account,
                "DETERMINISTIC_DECISION",
                {
                    "cycle_id": cycle_id,
                    "asset_id": snapshot.asset_id,
                    "allowed": gate.allowed,
                    "reason": gate.reason,
                    "snapshot_id": snapshot.digest,
                },
            )

    def _record_decision(
        self, cycle_id: str, account_id: str, snapshot: MarketSnapshot, gate, decisions: list
    ):
        self._decision(cycle_id, account_id, snapshot, gate)
        decisions.append(
            {"asset_id": snapshot.asset_id, "allowed": gate.allowed, "reason": gate.reason}
        )

    async def _requote(self, snapshot: MarketSnapshot) -> tuple[MarketSnapshot, GateResult]:
        """Fresh executable depth right before FILLS; model time must not age the quote."""
        try:
            execution = await self.observer.requote(snapshot)
        except (DataError, ValueError) as exc:
            reason = exc.code if isinstance(exc, DataError) else "OBSERVATION_INVALID"
            return snapshot, GateResult(
                allowed=False, reason="EXECUTION_REQUOTE_FAILED", details={"reason": reason}
            )
        moved = abs(execution.price - snapshot.price) / snapshot.price
        if moved > SLIPPAGE_CAP:
            return snapshot, GateResult(
                allowed=False, reason="PRICE_MOVED_SINCE_ANALYSIS", details={"moved": str(moved)}
            )
        return execution, GateResult(
            allowed=True, reason="VET_PASS", details={"requoted_snapshot": execution.digest}
        )

    async def run_cycle(
        self,
        account_id: str,
        request_id: str,
        *,
        snapshots: list[MarketSnapshot] | None = None,
        recorded_scan: ScanOutput | None = None,
        recorded_vet: dict[str, VetOutput] | None = None,
        now: datetime | None = None,
    ) -> dict:
        fixed_clock = now is not None
        now = now or utcnow()

        def clock():
            return now if fixed_clock else utcnow()

        recorded = recorded_scan is not None or recorded_vet is not None
        cycle_id, old = self._begin_cycle(account_id, request_id, recorded, now)
        if old:
            return old
        with self.db.transaction() as s:
            account = s.get(Account, account_id)
            killed = account.killed
        if killed:
            return self._finish_cycle(
                cycle_id,
                {"status": "blocked", "reason": "KILL_SWITCH_ACTIVE", "orders": []},
                clock(),
            )
        if not recorded and not self.capabilities().get("verified"):
            return self._finish_cycle(
                cycle_id, {"status": "blocked", "reason": "MODEL_PROBE_REQUIRED", "orders": []}, now
            )
        orders = []
        decisions = []
        try:
            if snapshots is None:
                await self.collect()
                snapshots = self.latest_snapshots()
            snapshots = list(snapshots)
            with self.db.transaction() as s:
                provenance = s.get(Account, account_id).provenance
            if any(x.provenance != provenance for x in snapshots):
                raise ValueError("snapshot/account provenance mismatch")
            for snapshot in snapshots:
                self.broker.save_snapshot(snapshot)
            eligible = []
            for snapshot in snapshots:
                gate = scan_gate(snapshot, clock(), self.settings.market_max_age_seconds)
                if gate.allowed:
                    # VET's model-free checks first: cheapest first, no model time on candidates
                    # that the observations already reject.
                    gate = vet_precheck(snapshot, clock())
                if gate.allowed:
                    eligible.append(snapshot)
                else:
                    self._record_decision(cycle_id, account_id, snapshot, gate, decisions)
            # One originating pool per asset; no cross-pool data splicing.
            best = {}
            for snapshot in eligible:
                if (
                    snapshot.asset_id not in best
                    or snapshot.liquidity_usd > best[snapshot.asset_id].liquidity_usd
                ):
                    best[snapshot.asset_id] = snapshot
            eligible = list(best.values())[:30]
            if eligible:
                scan = recorded_scan or await self.runtime.generate(
                    "SCAN",
                    {
                        "task": SCAN_TASK,
                        "snapshots": [
                            analysis_snapshot(x, scan=True) | {"evidence_id": x.digest}
                            for x in eligible
                        ],
                    },
                    cited(
                        ScanOutput,
                        candidate_ids=[x.asset_id for x in eligible],
                        evidence=[x.digest for x in eligible],
                    ),
                    cycle_id,
                )
                known = {x.asset_id: x for x in eligible}
                if not set(scan.candidate_ids) <= {x.asset_id for x in snapshots} or not set(
                    scan.evidence
                ) <= {x.digest for x in snapshots}:
                    raise LLMError("SCAN_UNKNOWN_EVIDENCE")
                self.record_agent(
                    cycle_id,
                    "SCAN",
                    "recorded" if recorded else "passed",
                    scan.model_dump(mode="json"),
                    recorded=recorded,
                )
                for asset_id in dict.fromkeys(scan.candidate_ids):
                    snapshot = known.get(asset_id)
                    if snapshot is None:
                        continue  # Rejected before ranking; that decision is already recorded.
                    if snapshot.digest not in scan.evidence:
                        gate = GateResult(allowed=False, reason="SCAN_EVIDENCE_NOT_CITED")
                        self._record_decision(cycle_id, account_id, snapshot, gate, decisions)
                        continue
                    if recorded:
                        analysis = (recorded_vet or {}).get(asset_id)
                        if analysis is None:
                            raise LLMError("RECORDED_VET_MISSING")
                    else:
                        try:
                            analysis = await self.runtime.generate(
                                "VET",
                                {
                                    "task": VET_TASK,
                                    "data_checks": "passed: Python verified every required "
                                    "observation, freshness and on-chain fact before this call",
                                    "snapshot": analysis_snapshot(snapshot),
                                    "evidence_id": snapshot.digest,
                                },
                                cited(VetOutput, required="evidence", evidence=[snapshot.digest]),
                                cycle_id,
                            )
                        except LLMError as exc:
                            if exc.code.startswith("GLOBAL_CONFIGURATION_CHANGED"):
                                raise
                            # One candidate's model failure rejects that candidate only.
                            self.record_agent(cycle_id, "VET", "failed", {"reason": exc.code})
                            gate = GateResult(allowed=False, reason="VET_MODEL_" + exc.code)
                            self._record_decision(cycle_id, account_id, snapshot, gate, decisions)
                            continue
                    self.record_agent(
                        cycle_id,
                        "VET",
                        "recorded" if recorded else "passed",
                        analysis.model_dump(mode="json"),
                        recorded=recorded,
                    )
                    with self.db.transaction() as s:
                        account = lock_account(s, account_id)
                        equity = self.broker.equity(s, account, clock())
                        if (
                            equity is None
                            or analysis.win_probability is None
                            or analysis.win_payoff_ratio is None
                            or analysis.win_payoff_ratio <= 0
                            or snapshot.liquidity_usd is None
                        ):
                            amount = D("0")
                        else:
                            amount = size_quote(
                                cash=account.cash,
                                equity=equity,
                                probability=analysis.win_probability,
                                payoff_ratio=analysis.win_payoff_ratio,
                                liquidity_usd=snapshot.liquidity_usd,
                            )
                    self.record_agent(
                        cycle_id,
                        "SIZE",
                        "calculated",
                        {"amount_usd": str(amount), "policy": POLICY_VERSION},
                    )
                    gate = deterministic_vet(snapshot, analysis, amount, clock())
                    if gate.allowed and amount <= 0:
                        gate = GateResult(allowed=False, reason="SIZE_ZERO")
                    execution = snapshot
                    if gate.allowed and not recorded and self.observer.can_requote(snapshot):
                        execution, gate = await self._requote(snapshot)
                    self._record_decision(cycle_id, account_id, snapshot, gate, decisions)
                    if gate.allowed:
                        order = self.broker.execute(
                            order_id=stable_hash(
                                {"cycle": cycle_id, "asset": asset_id, "side": "buy"}
                            ),
                            cycle_id=cycle_id,
                            account_id=account_id,
                            snapshot=execution,
                            quote=execution.buy_quote,
                            side="buy",
                            requested=amount,
                            now=clock(),
                        )
                        orders.append(order)
                        self.record_agent(cycle_id, "FILLS", order["status"], order)
                        self.notify_fill(order)
            else:
                self.record_agent(
                    cycle_id,
                    "SCAN",
                    "no_eligible_candidates",
                    {"count": 0, "reason": "NO_VERIFIED_FRESH_CANDIDATES"},
                )
            summary = {"orders": len(orders), "decisions": len(decisions), "paper_only": True}
            if not recorded:
                try:
                    chief = await self.runtime.generate(
                        "CHIEF",
                        {
                            "task": "Summarize only these recorded facts. Do not evaluate trades or issue instructions.",
                            "facts": summary,
                        },
                        ChiefOutput,
                        cycle_id,
                    )
                    self.record_agent(cycle_id, "CHIEF", "passed", chief.model_dump(mode="json"))
                except LLMError as exc:
                    self.record_agent(
                        cycle_id, "CHIEF", "report_degraded", {"facts": summary, "reason": exc.code}
                    )
            else:
                self.record_agent(cycle_id, "CHIEF", "recorded_facts", summary, recorded=True)
            return self._finish_cycle(
                cycle_id,
                {
                    "status": "completed",
                    "orders": orders,
                    "decisions": decisions,
                    "provenance": provenance,
                },
                clock(),
            )
        except asyncio.CancelledError:
            self._finish_cycle(
                cycle_id,
                {
                    "status": "interrupted",
                    "reason": "EXECUTION_CANCELLED",
                    "orders": orders,
                    "decisions": decisions,
                },
                clock(),
            )
            raise
        except (LLMError, DataError, ValueError) as exc:
            reason = (
                exc.code
                if isinstance(exc, (LLMError, DataError))
                else "INPUT_OR_PROVENANCE_INVALID"
            )
            self.record_agent(
                cycle_id, "CHIEF", "blocked", {"reason": reason, "committed_orders": len(orders)}
            )
            return self._finish_cycle(
                cycle_id,
                {
                    "status": "blocked" if not orders else "partial",
                    "reason": reason,
                    "orders": orders,
                    "decisions": decisions,
                },
                now,
            )
        except Exception:
            self._finish_cycle(
                cycle_id,
                {
                    "status": "failed",
                    "reason": "INTERNAL_FAILURE",
                    "orders": orders,
                    "decisions": decisions,
                },
                now,
            )
            raise

    async def run_risk(
        self,
        account_id: str,
        *,
        observations: dict[str, MarketSnapshot | None] | None = None,
        now: datetime | None = None,
    ) -> dict:
        fixed_clock = now is not None
        now = now or utcnow()

        def clock():
            return now if fixed_clock else utcnow()

        with self.db.transaction() as s:
            account = s.get(Account, account_id)
            if not account:
                raise ValueError("unknown account")
            positions = [
                as_dict(p)
                for p in s.scalars(select(Position).where(Position.account_id == account_id))
                if p.quantity > 0
            ]
        outcomes = []
        for position in positions:
            asset_id = position["asset_id"]
            snapshot = None
            attempts = 0
            if position["market_kind"] == "prediction" and observations is None:
                attempts = 1
                try:
                    snapshot, resolution = await self.prediction.risk_observation(asset_id)
                    if resolution is not None:
                        order = self.broker.settle_prediction(account_id, resolution, clock())
                        if order:
                            self.notify_fill(order)
                        result = {
                            "asset_id": asset_id,
                            "decision": "VERIFIED_BINARY_RESOLUTION",
                            "order": order,
                            "close_requested": False,
                            "attempts": attempts,
                        }
                        self.record_agent("risk", "RISK", "settled", result)
                        outcomes.append(result)
                        continue
                except (DataError, ValueError, KeyError, TypeError, ArithmeticError):
                    snapshot = None
            elif observations is not None:
                snapshot = observations.get(asset_id)
                attempts = 3
            else:
                for attempt in range(3):
                    attempts += 1
                    try:
                        chain, mint = asset_id.split(":", 1)
                        values = await self.dex.token(chain, mint)
                        # Use the same originating pool when available; never silently switch pools.
                        previous = next(
                            (x for x in self.latest_snapshots() if x.asset_id == asset_id), None
                        )
                        snapshot = next(
                            (x for x in values if previous and x.pair_id == previous.pair_id), None
                        )
                        if snapshot is not None and self.settings.market_observer_enabled:
                            snapshot = await self.observer.observe_exit(
                                snapshot, D(position["quantity"])
                            )
                        if snapshot is not None:
                            snapshot = (await self.market_feed.enrich([snapshot]))[0]
                        if snapshot is not None and snapshot.fresh(clock()):
                            break
                    except (DataError, ValueError):
                        snapshot = None
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt)
            if position["market_kind"] == "prediction":
                gate = GateResult(
                    allowed=False, reason="PREDICTION_EXIT_REQUIRES_VERIFIED_QUOTE_OR_RESOLUTION"
                )
            else:
                gate = risk_decision(snapshot, clock(), attempts)
            pending = str(position["status"]).startswith("close_requested")
            close = gate.allowed or pending
            if snapshot is not None and snapshot.provenance != account.provenance:
                raise ValueError("risk snapshot/account provenance mismatch")
            self.broker.mark_and_request_close(
                account_id, asset_id, snapshot, clock(), close, gate.reason
            )
            order = None
            if (
                close
                and snapshot is not None
                and snapshot.sell_quote is not None
                and snapshot.fresh(clock())
            ):
                order = self.broker.execute(
                    order_id=stable_hash(
                        {
                            "risk_position": position["id"],
                            "snapshot": snapshot.digest,
                            "quantity": position["quantity"],
                        }
                    ),
                    cycle_id="risk",
                    account_id=account_id,
                    snapshot=snapshot,
                    quote=snapshot.sell_quote,
                    side="sell",
                    requested=D(position["quantity"]),
                    now=clock(),
                    market_kind=position["market_kind"],
                )
                self.notify_fill(order)
            cap = snapshot.market_cap if snapshot is not None and snapshot.fresh(clock()) else None
            initial_cap = (
                D(position["initial_market_cap"])
                if position["initial_market_cap"] is not None
                else None
            )
            cap_flag = (
                "FLAG_60_PERCENT_NO_AUTOMATIC_TAKE_PROFIT"
                if cap and initial_cap and cap >= initial_cap * D("1.6")
                else "HOLD_50_PERCENT_NO_AUTOMATIC_TAKE_PROFIT"
                if cap and initial_cap and cap >= initial_cap * D("1.5")
                else None
            )
            result = {
                "asset_id": asset_id,
                "decision": gate.reason,
                "close_requested": close,
                "attempts": attempts,
                "market_cap_flag": cap_flag,
                "order": order,
            }
            self.record_agent("risk", "RISK", "close_requested" if close else "hold", result)
            outcomes.append(result)
        intraday = intraday_check(self.db, account_id, clock())
        if intraday.get("notified"):
            change = D(intraday["change_fraction"]) * 100
            self.notifier.send(f"장중 평가 자산 {change:+.1f}% 변동 (서울 날짜 첫 평가 대비)")
        return {
            "paper_only": True,
            "positions_checked": len(positions),
            "results": outcomes,
            "intraday": intraday,
        }

    async def run_book(
        self,
        account_id: str,
        request_id: str,
        question: PredictionQuestion,
        price_loader,
        *,
        now: datetime | None = None,
    ) -> dict:
        fixed_clock = now is not None
        now = now or utcnow()

        def clock():
            return now if fixed_clock else utcnow()

        cycle_id, old = self._begin_cycle(account_id, "book:" + request_id, False, now)
        if old:
            return old
        with self.db.transaction() as s:
            killed = s.get(Account, account_id).killed
        if killed:
            return self._finish_cycle(
                cycle_id,
                {"status": "blocked", "reason": "KILL_SWITCH_ACTIVE", "orders": []},
                clock(),
            )
        if not self.capabilities().get("verified"):
            return self._finish_cycle(
                cycle_id, {"status": "blocked", "reason": "MODEL_PROBE_REQUIRED", "orders": []}, now
            )
        readiness = await self.wm.readiness()
        if readiness["status"] != "READY":
            self.record_agent(cycle_id, "BOOK", "degraded", readiness)
            return self._finish_cycle(
                cycle_id,
                {"status": "blocked", "reason": "WORLD_MONITOR_DEGRADED", "orders": []},
                now,
            )
        try:
            # Critical sequence: persist the price-blind probability before invoking price_loader.
            if not question.evidence_text or not question.source_ids:
                return self._finish_cycle(
                    cycle_id,
                    {"status": "blocked", "reason": "BOOK_EVIDENCE_CONTENT_REQUIRED", "orders": []},
                    clock(),
                )
            output = await self.runtime.generate(
                "BOOK",
                {
                    "task": "Estimate P(YES) before seeing any market prices. Abstain if source evidence is insufficient. Cite evidence_id in evidence.",
                    "question": question.model_dump(mode="json"),
                    "evidence_id": stable_hash(question),
                },
                cited(BookOutput, required="evidence", evidence=[stable_hash(question)]),
                cycle_id,
            )
            self.record_agent(cycle_id, "BOOK", "passed", output.model_dump(mode="json"))
            if output.abstain or stable_hash(question) not in output.evidence:
                return self._finish_cycle(
                    cycle_id,
                    {
                        "status": "blocked",
                        "reason": "BOOK_ABSTAIN_OR_EVIDENCE_MISMATCH",
                        "orders": [],
                    },
                    now,
                )
            prices: list[PredictionPrice] = await price_loader()
            orders = []
            for price in prices:
                with self.db.transaction() as s:
                    account = lock_account(s, account_id)
                    equity = self.broker.equity(s, account, clock())
                    open_bets = len(
                        [
                            p
                            for p in s.scalars(
                                select(Position).where(
                                    Position.account_id == account_id,
                                    Position.market_kind == "prediction",
                                )
                            )
                            if p.quantity > 0
                        ]
                    )
                    existing = s.scalar(
                        select(Position).where(
                            Position.account_id == account_id,
                            Position.asset_id
                            == f"prediction:{price.venue}:{price.venue_market_id}:{price.outcome}",
                        )
                    )
                    losing = (
                        existing is not None
                        and existing.quantity > 0
                        and (
                            existing.last_price is None
                            or existing.last_price * existing.quantity < existing.cost_basis
                        )
                    )
                    cash = account.cash
                gate = book_gate(
                    question,
                    price,
                    output.probability_yes,
                    now=clock(),
                    open_bets=open_bets,
                    losing_position=losing,
                )
                if (
                    not gate.allowed
                    or price.resolution_hash != stable_hash(question.resolution_rules)
                    or price.quote is None
                    or equity is None
                ):
                    continue
                probability = (
                    output.probability_yes if price.outcome == "yes" else 1 - output.probability_yes
                )
                depth = sum(x.price * x.quantity for x in price.quote.levels)
                amount = size_quote(
                    cash=cash,
                    equity=equity,
                    probability=probability,
                    payoff_ratio=(1 - price.ask) / price.ask,
                    liquidity_usd=depth * 50,
                )
                if amount <= 0:
                    continue
                expected_asset = f"prediction:{price.venue}:{price.venue_market_id}:{price.outcome}"
                if price.quote.asset_id != expected_asset or price.quote.provenance != "live":
                    continue
                snapshot = MarketSnapshot(
                    asset_id=expected_asset,
                    symbol=question.title,
                    chain="prediction",
                    pair_id=price.venue_market_id,
                    source=price.venue,
                    source_reference=price.venue_market_id,
                    fetched_at=clock(),
                    observed_at=price.observed_at,
                    price=price.ask,
                    liquidity_usd=depth,
                    buy_quote=price.quote,
                )
                order = self.broker.execute(
                    order_id=stable_hash(
                        {
                            "book_cycle": cycle_id,
                            "venue": price.venue,
                            "market": price.venue_market_id,
                            "outcome": price.outcome,
                        }
                    ),
                    cycle_id=cycle_id,
                    account_id=account_id,
                    snapshot=snapshot,
                    quote=price.quote,
                    side="buy",
                    requested=amount,
                    now=clock(),
                    market_kind="prediction",
                )
                orders.append(order)
                self.notify_fill(order)
                break  # One outcome/venue for this question, not simultaneous contradictory bets.
            return self._finish_cycle(
                cycle_id,
                {
                    "status": "completed",
                    "reason": "BOOK_EVALUATED" if orders else "NO_VERIFIED_EXECUTABLE_OPPORTUNITY",
                    "orders": orders,
                },
                clock(),
            )
        except (LLMError, DataError) as exc:
            return self._finish_cycle(
                cycle_id, {"status": "blocked", "reason": exc.code, "orders": []}, clock()
            )
        except asyncio.CancelledError:
            self._finish_cycle(
                cycle_id,
                {"status": "interrupted", "reason": "EXECUTION_CANCELLED", "orders": []},
                clock(),
            )
            raise
        except Exception:
            self._finish_cycle(
                cycle_id, {"status": "failed", "reason": "INTERNAL_FAILURE", "orders": []}, clock()
            )
            raise
