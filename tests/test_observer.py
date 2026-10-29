from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest

from trading_team.broker import PaperBroker
from trading_team.data import DataError, DexScreener, PublicDataClient, SolanaOnchain, b58encode
from trading_team.domain import MarketSnapshot, VetOutput
from trading_team.observer import (
    USDC_MINT,
    JupiterQuotes,
    MarketObserver,
    Pacer,
    TradeWindows,
)
from trading_team.policy import vet_gate

POOL_KEY, WHALE_KEY, OTHER_KEY = b"\x01" * 32, b"\x02" * 32, b"\x03" * 32
POOL, WHALE, OTHER = b58encode(POOL_KEY), b58encode(WHALE_KEY), b58encode(OTHER_KEY)
MINT = "Mint" + "1" * 36
T0 = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)


class Market:
    """Constant-product pool behind mocked Jupiter, Solana RPC, Dex and GeckoTerminal."""

    def __init__(self):
        self.now, self.holders, self.trades, self.requests = T0, 2, [], []
        self.pair: dict = {}
        self.usd, self.tokens = D(50_000), D(50_000_000)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "api.jup.ag":
            q = request.url.params
            amount = D(q["amount"])
            if q["inputMint"] == USDC_MINT:
                out = self.tokens * amount / (self.usd * 10**6 + amount)
            else:
                out = self.usd * amount / (self.tokens * 10**6 + amount)
            return httpx.Response(
                200,
                json={
                    "inputMint": q["inputMint"],
                    "outputMint": q["outputMint"],
                    "inAmount": q["amount"],
                    "outAmount": str(int(out * 10**6)),
                    "swapMode": "ExactIn",
                    "contextSlot": 500,
                },
            )
        if host == "api.dexscreener.com":
            if path.startswith(("/token-profiles/", "/token-boosts/")):
                return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": MINT}])
            if path.startswith("/tokens/v1/solana/"):
                return httpx.Response(200, json=[dex_pair() | self.pair])
            return httpx.Response(200, json={"pairs": [dex_pair() | self.pair]})
        if host == "api.geckoterminal.com":
            assert path.endswith(f"/pools/{POOL}/trades")
            return httpx.Response(200, json={"data": self.trades})
        body = json.loads(request.content)
        if body["method"] == "getAccountInfo":
            info = {
                "isInitialized": True,
                "mintAuthority": None,
                "freezeAuthority": None,
                "supply": str(1_000_000 * 10**6),
                "decimals": 6,
            }
            value = {
                "owner": SolanaOnchain.TOKEN_PROGRAM,
                "data": {"parsed": {"type": "mint", "info": info}},
            }
            result = {"context": {"slot": 500}, "value": value}
        elif body["method"] == "getProgramAccounts":
            rows = [(POOL_KEY, 600_000), (WHALE_KEY, 40_000)]
            rows += [(bytes([9, i]) + b"\x00" * 30, 10) for i in range(self.holders - 1)]
            encoded = [
                base64.b64encode(owner + (amount * 10**6).to_bytes(8, "little")).decode()
                for owner, amount in rows
            ]
            result = {
                "context": {"slot": 501},
                "value": [{"account": {"data": [x]}} for x in encoded],
            }
        else:
            result = int(self.now.timestamp())
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def trade(self, minutes_ago: float, side: str, trader: str) -> None:
        tokens = (MINT, USDC_MINT) if side == "sell" else (USDC_MINT, MINT)
        at = self.now - timedelta(minutes=minutes_ago)
        self.trades.append(
            {
                "attributes": {
                    "block_timestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "kind": side,
                    "tx_from_address": trader,
                    "from_token_address": tokens[0],
                    "to_token_address": tokens[1],
                }
            }
        )


def dex_pair() -> dict:
    return {
        "chainId": "solana",
        "pairAddress": POOL,
        "baseToken": {"address": MINT, "symbol": "TEST"},
        "priceUsd": "0.001",
        "liquidity": {"usd": 100_000},
        "volume": {"h6": 500, "h24": 2000},
        "marketCap": 1_000_000,
        "pairCreatedAt": int((T0 - timedelta(days=1)).timestamp() * 1000),
    }


@pytest.fixture
def market(monkeypatch):
    async def no_wait(self):
        return None

    monkeypatch.setattr(Pacer, "wait", no_wait)
    monkeypatch.setattr(JupiterQuotes, "RETRY_AFTER", 0)
    monkeypatch.setattr(TradeWindows, "RETRY_AFTER", 0)
    return Market()


async def observe(db, settings, market, client):
    data = PublicDataClient(db, client)
    observer = MarketObserver(settings, db, data, DexScreener(data), SolanaOnchain(settings, data))
    discovered = DexScreener.normalize(dex_pair(), market.now)
    observed, notes = await observer.observe([discovered])
    PaperBroker(db).save_snapshot(observed[0])
    return observed[0], notes


def analysis(snapshot: MarketSnapshot) -> VetOutput:
    return VetOutput(
        verdict="PASS",
        signal_priced_in=False,
        uses_world_news=False,
        win_probability=D("0.7"),
        win_payoff_ratio=D(2),
        expected_return_fraction=D("0.1"),
        evidence=[snapshot.digest],
        explanation="test",
    )


async def test_observed_pool_passes_vet_and_fills_from_jupiter_depth(db, settings, market):
    async with httpx.AsyncClient(transport=httpx.MockTransport(market.handle)) as client:
        first, notes = await observe(db, settings, market, client)
        assert notes == [] and first.observed_at == T0 and first.previous_holder_count is None
        # Without an earlier observation of our own, holder growth is unknown.
        assert vet_gate(first, analysis(first), D(60), T0).reason == "HOLDER_HISTORY_UNAVAILABLE"
        market.now, market.holders = T0 + timedelta(minutes=6), 5
        market.trade(1, "buy", OTHER)
        market.trade(2, "sell", OTHER)
        market.trade(7, "buy", OTHER)
        market.trade(30, "sell", WHALE)  # Outside both windows.
        second, notes = await observe(db, settings, market, client)
    assert notes == []
    assert second.observed_at == market.now and second.fresh(market.now)
    assert (second.previous_holder_count, second.holder_count) == (2, 5)
    assert second.previous_holder_observed_at == T0 and second.previous_liquidity_usd == 100_000
    # The pool vault is excluded; the whale owns 4% of supply.
    assert second.largest_token_account_share == D("0.04") and second.contract_safe
    windows = [(w.start, w.end, w.buys, w.sells) for w in second.windows]
    assert windows == [
        (market.now - timedelta(minutes=10), market.now - timedelta(minutes=5), 1, 0),
        (market.now - timedelta(minutes=5), market.now, 1, 1),
    ]
    assert second.large_holder_selling is False
    quote = second.buy_quote
    prices = [level.price for level in quote.levels]
    assert prices == sorted(prices) and quote.reference_price == second.price
    assert abs(second.price - D("0.001")) / D("0.001") < D("0.001")
    gate = vet_gate(second, analysis(second), D(60), market.now)
    assert gate.allowed and gate.reason == "VET_PASS"
    order = PaperBroker(db).execute(
        order_id="observed-buy",
        cycle_id="test",
        account_id="main",
        snapshot=second,
        quote=quote,
        side="buy",
        requested=D(60),
        now=market.now + timedelta(seconds=20),
    )
    assert order["status"] == "filled" and D(order["notional"]) <= 60
    assert D(order["slippage"]) < D("0.02")
    assert all(PaperBroker(db).integrity("main").values())
    # No order, wallet or signing endpoint was contacted; only read-only quotes.
    assert {r.url.path for r in market.requests if r.url.host == "api.jup.ag"} == {"/swap/v1/quote"}


async def test_large_holder_sale_in_window_blocks_entry(db, settings, market):
    market.trade(3, "sell", WHALE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(market.handle)) as client:
        snapshot, _ = await observe(db, settings, market, client)
    assert snapshot.large_holder_selling is True
    # Give it history so the earlier checks pass and the sale decides the outcome.
    blocked = snapshot.model_copy(
        update={
            "previous_holder_count": 1,
            "previous_price": D("0.0009"),
            "previous_holder_observed_at": T0 - timedelta(minutes=10),
            "previous_liquidity_usd": D(90_000),
        }
    )
    gate = vet_gate(blocked, analysis(blocked), D(60), T0)
    assert gate.reason == "LARGE_HOLDER_SELLING_WAIT"


async def test_unavailable_trades_leave_windows_and_sales_unknown(db, settings, market):
    def throttled(request):
        if request.url.host == "api.geckoterminal.com":
            return httpx.Response(429, json={})
        return market.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(throttled)) as client:
        snapshot, notes = await observe(db, settings, market, client)
    assert snapshot.windows == [] and snapshot.large_holder_selling is None
    assert notes == [
        {"asset_id": f"solana:{MINT}", "source": "geckoterminal", "reason": "DATA_HTTP_429"}
    ]
    # The executable observation itself is still recorded.
    assert snapshot.buy_quote is not None and snapshot.holder_count == 2


async def test_full_trade_page_short_of_window_is_incomplete(market):
    market.trades = []
    for _ in range(300):
        market.trade(0.5, "buy", OTHER)
    async with httpx.AsyncClient(transport=httpx.MockTransport(market.handle)) as client:
        with pytest.raises(DataError, match="TRADES_COVERAGE_INCOMPLETE"):
            await TradeWindows(PublicDataClient(client=client)).observe(
                POOL, MINT, market.now, [WHALE]
            )


async def test_exit_quote_marks_at_bid_for_held_quantity(db, settings, market):
    async with httpx.AsyncClient(transport=httpx.MockTransport(market.handle)) as client:
        data = PublicDataClient(db, client)
        observer = MarketObserver(
            settings, db, data, DexScreener(data), SolanaOnchain(settings, data)
        )
        snapshot = await observer.observe_exit(
            DexScreener.normalize(dex_pair(), market.now), D(60_000)
        )
    quote = snapshot.sell_quote
    prices = [level.price for level in quote.levels]
    assert prices == sorted(prices, reverse=True) and snapshot.price == quote.reference_price
    assert sum(level.quantity for level in quote.levels) <= D(60_000)
    assert snapshot.buy_quote is None and snapshot.fresh(market.now)


async def test_observer_respects_budget_and_can_be_disabled(db, settings, market):
    low = DexScreener.normalize(dex_pair() | {"liquidity": {"usd": 9_999}}, market.now)
    cfg = settings.model_copy(update={"market_observer_max_assets": 1})
    observer = MarketObserver(cfg, db, None, None, None)
    ranked = [
        DexScreener.normalize(
            dex_pair() | {"baseToken": {"address": f"Mint{i}" + "1" * 35}}, market.now
        ).model_copy(update={"liquidity_usd": D(20_000 + i)})
        for i in range(3)
    ]
    assert [x.liquidity_usd for x in observer.select([low, *ranked])] == [D(20_002)]
    off = MarketObserver(
        settings.model_copy(update={"market_observer_enabled": False}), db, None, None, None
    )
    assert await off.observe([low]) == ([low], [])


async def test_desk_collects_observed_pools_and_risk_exits_on_observed_bids(db, settings, market):
    from trading_team.domain import utcnow
    from trading_team.engine import Desk
    from trading_team.llm import LLMRuntime

    class NoLLM:
        async def generate(self, *_args):
            raise AssertionError("collection and RISK never call the model")

        async def close(self):
            pass

    # DexScreener collection is stamped with the real clock, so the chain clock follows it.
    market.now = utcnow().replace(microsecond=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(market.handle)) as client:
        desk = Desk(settings, db, LLMRuntime(settings, NoLLM()), PublicDataClient(db, client))
        collected = await desk.collect()
        assert collected["errors"] == [] and collected["count"] == 1
        stored = desk.latest_snapshots()[0]
        assert stored.observed_at == market.now and stored.buy_quote is not None
        entry = PaperBroker(db).execute(
            order_id="entry",
            cycle_id="test",
            account_id="main",
            snapshot=stored,
            quote=stored.buy_quote,
            side="buy",
            requested=D(60),
            now=utcnow(),
        )
        assert entry["status"] == "filled"
        market.now = utcnow().replace(microsecond=0)
        held = await desk.run_risk("main")
        assert held["results"][0]["decision"] == "HOLD" and held["results"][0]["order"] is None
        # The 6-hour volume collapses below a fifth of its 24-hour average: RISK exits.
        market.pair = {"volume": {"h6": 10, "h24": 2000}}
        market.now = utcnow().replace(microsecond=0)
        closed = await desk.run_risk("main")
        await desk.close()
    result = closed["results"][0]
    assert result["decision"] == "CLOSE_VOLUME_COLLAPSE" and result["order"]["status"] == "filled"
    assert all(PaperBroker(db).integrity("main").values())


async def test_jupiter_rate_limit_retries_then_leaves_pool_unobserved(
    db, settings, market, monkeypatch
):
    monkeypatch.setattr(JupiterQuotes, "RETRY_AFTER", 0)
    calls, limit = [], {"failures": 1}

    def throttled(request):
        if request.url.host == "api.jup.ag":
            calls.append(request)
            if len(calls) <= limit["failures"]:
                return httpx.Response(429, json={})
        return market.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(throttled)) as client:
        snapshot, notes = await observe(db, settings, market, client)
        assert notes == [] and snapshot.buy_quote is not None and len(calls) == 5
        calls.clear()
        limit["failures"] = 99
        snapshot, notes = await observe(db, settings, market, client)
    # Three attempts, then the pool keeps its unknown observation time and cannot pass SCAN.
    assert len(calls) == 3 and snapshot.observed_at is None and snapshot.buy_quote is None
    assert notes == [
        {"asset_id": f"solana:{MINT}", "source": "execution_quote", "reason": "DATA_HTTP_429"}
    ]


class DeskModel:
    """Cites exactly the IDs it is offered; optionally moves the market while 'thinking'."""

    def __init__(self, during_vet=None):
        self.during_vet, self.schemas = during_vet, []

    async def generate(self, messages, schema):
        from trading_team.domain import ChiefOutput, ScanOutput
        from trading_team.llm import Reply

        payload = json.loads(messages[-1]["content"])
        self.schemas.append(schema.__name__)
        if "example" in payload:
            return Reply(schema.model_validate(payload["example"]), {})
        if issubclass(schema, ScanOutput):
            rows = payload["snapshots"]
            return Reply(
                schema(
                    candidate_ids=[x["candidate_id"] for x in rows],
                    evidence=[x["evidence_id"] for x in rows],
                    explanation="ranked by derived holder growth",
                ),
                {},
            )
        if issubclass(schema, VetOutput):
            if self.during_vet:
                self.during_vet()
            return Reply(
                schema.model_validate(
                    analysis_payload(payload["evidence_id"]) | {"explanation": "observed"}
                ),
                {},
            )
        return Reply(ChiefOutput(summary="recorded facts", evidence=[]), {})

    async def close(self):
        pass


def analysis_payload(digest: str) -> dict:
    return {
        "verdict": "PASS",
        "signal_priced_in": False,
        "uses_world_news": False,
        "win_probability": "0.7",
        "win_payoff_ratio": "2",
        "expected_return_fraction": "0.1",
        "evidence": [digest],
    }


async def live_desk(db, settings, market, client, model, *, history: bool):
    from trading_team.domain import utcnow
    from trading_team.engine import Desk
    from trading_team.llm import LLMRuntime

    market.now = utcnow().replace(microsecond=0)
    if history:
        earlier = market.now - timedelta(minutes=6)
        previous = DexScreener.normalize(dex_pair(), earlier).model_copy(
            update={
                "observed_at": earlier,
                "price": D("0.00099"),
                "holder_count": 2,
                "holder_observed_at": earlier,
            }
        )
        PaperBroker(db).save_snapshot(previous)
    market.holders = 5
    desk = Desk(settings, db, LLMRuntime(settings, model), PublicDataClient(db, client))
    assert (await desk.probe())["verified"]
    return desk


async def test_live_cycle_requotes_and_fills_on_fresh_depth(db, settings, market):
    model = DeskModel()
    async with httpx.AsyncClient(transport=httpx.MockTransport(market.handle)) as client:
        desk = await live_desk(db, settings, market, client, model, history=True)
        messages = []
        desk.notifier.send = messages.append
        result = await desk.run_cycle("main", "observed-entry")
        await desk.close()
    assert len(messages) == 1 and messages[0].startswith("매수 filled solana:")
    assert result["status"] == "completed", result
    assert [x["reason"] for x in result["decisions"]] == ["VET_PASS"]
    order = result["orders"][0]
    assert order["status"] == "filled" and D(order["notional"]) <= 60
    # SCAN offered only the supplied IDs: an enum the provider must cite from.
    assert model.schemas.count("ScanOutput") == 2 and model.schemas.count("VetOutput") == 2
    # Depth was quoted again for execution: two ladders of four quotes.
    quotes = [r for r in market.requests if r.url.host == "api.jup.ag"]
    assert len(quotes) == 8
    assert all(PaperBroker(db).integrity("main").values())


async def test_price_move_while_model_thinks_blocks_the_fill(db, settings, market):
    def pump():
        market.usd *= D("1.1")  # About 21% higher price on the constant-product curve.

    model = DeskModel(during_vet=pump)
    async with httpx.AsyncClient(transport=httpx.MockTransport(market.handle)) as client:
        desk = await live_desk(db, settings, market, client, model, history=True)
        result = await desk.run_cycle("main", "moved")
        await desk.close()
    assert [x["reason"] for x in result["decisions"]] == ["PRICE_MOVED_SINCE_ANALYSIS"]
    assert result["orders"] == []


async def test_data_rejects_are_decided_before_any_model_call(db, settings, market):
    model = DeskModel()
    async with httpx.AsyncClient(transport=httpx.MockTransport(market.handle)) as client:
        desk = await live_desk(db, settings, market, client, model, history=False)
        result = await desk.run_cycle("main", "no-history")
        await desk.close()
    assert [x["reason"] for x in result["decisions"]] == ["HOLDER_HISTORY_UNAVAILABLE"]
    # Only the probe and the factual CHIEF report used the model; no SCAN or VET time was spent.
    assert model.schemas.count("ScanOutput") == 1 and model.schemas.count("VetOutput") == 1
