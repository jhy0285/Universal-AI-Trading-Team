"""Free observations that DexScreener does not publish, for Dex-discovered Solana pools.

- Jupiter quotes: executable depth, with the on-chain time of the quoted slot.
- Solana RPC: holders, the largest non-pool owner and mint authorities.
- GeckoTerminal trades: two disjoint recent windows and sales by large holders.
- Previous holder, price and liquidity values come from this ledger's own snapshots.

A missing source leaves its fields unknown; the deterministic gates then block the entry.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from trading_team.data import DataError, DexScreener, PublicDataClient, SolanaOnchain
from trading_team.domain import ExecutionQuote, Level, MarketSnapshot, TransactionWindow
from trading_team.policy import MIN_ORDER, POSITION_CAP
from trading_team.settings import Settings
from trading_team.storage import Database, SnapshotRow

D = Decimal
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_UNIT = D(10) ** 6
# Cumulative quote sizes as fractions of the largest size; differences become depth levels.
LADDER = (D(1) / 16, D(1) / 4, D(1) / 2, D(1))
WINDOW = timedelta(minutes=5)
HISTORY_MIN = timedelta(minutes=5)
HISTORY_MAX = timedelta(minutes=30)
OVERSIZED_RETRY_SECONDS = 3600


class Pacer:
    """Client-side spacing for a keyless per-caller rate limit."""

    def __init__(self, interval: float):
        self.interval, self.next, self.lock = interval, 0.0, asyncio.Lock()

    async def wait(self):
        async with self.lock:
            delay = self.next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next = time.monotonic() + self.interval


class JupiterQuotes:
    """Read-only swap quotes. No swap transaction is ever requested or signed."""

    URL = "https://api.jup.ag/swap/v1/quote"
    # The keyless bucket was observed refilling within 10 seconds; a shared IP may drain it.
    RETRY_AFTER = 10.0

    def __init__(self, settings: Settings, data: PublicDataClient, solana: SolanaOnchain):
        self.key = settings.jupiter_api_key.get_secret_value()
        self.data, self.solana = data, solana
        # Jupiter allows 30 requests/minute keyless and 60 with a free key, in one bucket.
        self.pacer = Pacer(1.1 if self.key else 2.2)

    async def quote(self, input_mint: str, output_mint: str, amount: int) -> tuple[int, int, int]:
        for attempt in range(3):
            await self.pacer.wait()
            try:
                body = await self.data.request(
                    "GET",
                    self.URL,
                    kind="jupiter_quote",
                    params={
                        "inputMint": input_mint,
                        "outputMint": output_mint,
                        "amount": str(amount),
                        "swapMode": "ExactIn",
                        "slippageBps": "200",
                        "restrictIntermediateTokens": "true",
                    },
                    headers={"x-api-key": self.key} if self.key else None,
                )
                break
            except DataError as exc:
                if exc.code != "DATA_HTTP_429" or attempt == 2:
                    raise
                await asyncio.sleep(self.RETRY_AFTER)
        try:
            if (
                body["inputMint"] != input_mint
                or body["outputMint"] != output_mint
                or body["swapMode"] != "ExactIn"
                or int(body["inAmount"]) != amount
            ):
                raise ValueError
            received, slot = int(body["outAmount"]), int(body["contextSlot"])
        except (KeyError, TypeError, ValueError):
            raise DataError("JUPITER_QUOTE_INVALID") from None
        if received <= 0 or slot <= 0:
            raise DataError("JUPITER_NO_ROUTE")
        return amount, received, slot

    async def ladder(self, mint: str, decimals: int, side: str, size: Decimal) -> ExecutionQuote:
        """Depth from cumulative quotes: buy sizes are USD, sell sizes are tokens.

        Each level is the marginal price between two quote sizes. Prices are made monotonic in the
        conservative direction, so a level is never better than one already observed.
        """
        token_unit = D(10) ** decimals
        if side == "buy":
            pair, units = (USDC_MINT, mint), (USDC_UNIT, token_unit)
        else:
            pair, units = (mint, USDC_MINT), (token_unit, USDC_UNIT)
        amounts = sorted({int(size * fraction * units[0]) for fraction in LADDER} - {0})
        if not amounts:
            raise DataError("JUPITER_SIZE_TOO_SMALL")
        quotes = [await self.quote(*pair, amount) for amount in amounts]
        levels, spent, got, bound = [], 0, 0, None
        for given, received, _slot in quotes:
            if given <= spent or received <= got:
                break
            paid, bought = D(given - spent) / units[0], D(received - got) / units[1]
            spent, got = given, received
            if side == "buy":
                quantity, price = bought, paid / bought
                bound = price if bound is None else max(bound, price)
            else:
                quantity, price = paid, bought / paid
                bound = price if bound is None else min(bound, price)
            levels.append(Level(price=bound, quantity=quantity))
        if not levels:
            raise DataError("JUPITER_DEPTH_UNAVAILABLE")
        observed_at = await self.solana.block_time(min(slot for *_, slot in quotes))
        return ExecutionQuote(
            asset_id=f"solana:{mint}",
            side=side,
            reference_price=levels[0].price,
            levels=levels,
            observed_at=observed_at,
            source="jupiter_quote_ladder",
            source_reference=f"{self.URL}?inputMint={pair[0]}&outputMint={pair[1]}",
            provenance="live",
        )


class TradeWindows:
    """Recent pool trades from GeckoTerminal's keyless API (30 calls/minute)."""

    URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{}/trades"
    PAGE = 300

    def __init__(self, data: PublicDataClient):
        self.data = data
        self.pacer = Pacer(2.2)

    @staticmethod
    def _summary(body):
        rows = body.get("data") if isinstance(body, dict) else None
        return {"trades": len(rows) if isinstance(rows, list) else None}

    async def observe(self, pool: str, mint: str, end: datetime, holders: list[str] | None) -> dict:
        if not pool.isalnum() or len(pool) > 60:
            raise DataError("TRADES_POOL_INVALID")
        await self.pacer.wait()
        body = await self.data.request(
            "GET",
            self.URL.format(pool),
            kind="geckoterminal_trades",
            headers={"Accept": "application/json"},
            summarize=self._summary,
        )
        trades = []
        try:
            rows = body["data"]
            for row in rows:
                item = row["attributes"]
                at = datetime.fromisoformat(item["block_timestamp"].replace("Z", "+00:00"))
                if at.utcoffset() is None:
                    raise ValueError
                # Direction relative to this mint, whichever token the venue calls the base.
                if item["to_token_address"] == mint:
                    side = "buy"
                elif item["from_token_address"] == mint:
                    side = "sell"
                else:
                    raise ValueError
                trades.append((at, side, str(item["tx_from_address"])))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise DataError("TRADES_INVALID") from None
        start, middle = end - 2 * WINDOW, end - WINDOW
        # A full page that does not reach the first window's start may omit trades.
        if len(trades) >= self.PAGE and min(at for at, _, _ in trades) > start:
            raise DataError("TRADES_COVERAGE_INCOMPLETE")
        counts = [{"buy": 0, "sell": 0}, {"buy": 0, "sell": 0}]
        sellers = set()
        for at, side, trader in trades:
            if start <= at < middle:
                counts[0][side] += 1
            elif middle <= at <= end:
                counts[1][side] += 1
            else:
                continue
            if side == "sell":
                sellers.add(trader)
        large_sellers = sorted(sellers & set(holders)) if holders is not None else None
        return {
            "windows": [
                TransactionWindow(
                    start=start, end=middle, buys=counts[0]["buy"], sells=counts[0]["sell"]
                ),
                TransactionWindow(
                    start=middle, end=end, buys=counts[1]["buy"], sells=counts[1]["sell"]
                ),
            ],
            "large_holder_selling": None if large_sellers is None else bool(large_sellers),
            "large_holder_sellers": large_sellers,
        }


class MarketObserver:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        data: PublicDataClient,
        dex: DexScreener,
        solana: SolanaOnchain,
    ):
        self.settings, self.db, self.dex, self.solana = settings, db, dex, solana
        self.quotes = JupiterQuotes(settings, data, solana)
        self.trades = TradeWindows(data)
        self.decimals: dict[str, int] = {}
        self.oversized: dict[str, float] = {}

    def select(self, discovered: list[MarketSnapshot]) -> list[MarketSnapshot]:
        """The most liquid Solana pool per asset, as SCAN selects it, within the call budget."""
        best: dict[str, MarketSnapshot] = {}
        for item in discovered:
            if item.chain != "solana" or item.liquidity_usd is None or item.liquidity_usd < 10000:
                continue
            if item.asset_id not in best or item.liquidity_usd > best[item.asset_id].liquidity_usd:
                best[item.asset_id] = item
        ranked = sorted(best.values(), key=lambda x: x.liquidity_usd, reverse=True)
        return ranked[: self.settings.market_observer_max_assets]

    async def observe(
        self, discovered: list[MarketSnapshot]
    ) -> tuple[list[MarketSnapshot], list[dict]]:
        if not self.settings.market_observer_enabled:
            return discovered, []
        chosen = {(x.asset_id, x.pair_id) for x in self.select(discovered)}
        result, errors = [], []
        for item in discovered:
            if (item.asset_id, item.pair_id) in chosen:
                try:
                    item, notes = await self.observe_entry(item)
                    errors.extend(notes)
                except DataError as exc:
                    errors.append(
                        {"asset_id": item.asset_id, "source": "execution_quote", "reason": exc.code}
                    )
                except ValueError:
                    reason = "OBSERVATION_INVALID"
                    errors.append(
                        {"asset_id": item.asset_id, "source": "observer", "reason": reason}
                    )
            result.append(item)
        return result, errors

    async def _decimals(self, mint: str) -> int:
        if mint not in self.decimals:
            _account, _program, info = await self.solana.mint_account(mint)
            self.decimals[mint] = int(info["decimals"])
        return self.decimals[mint]

    def previous(self, snapshot: MarketSnapshot, holder_time: datetime) -> MarketSnapshot | None:
        """This ledger's own earlier observation of the same pool, 5 to 30 minutes before."""
        with self.db.transaction() as session:
            rows = session.scalars(
                select(SnapshotRow)
                .where(
                    SnapshotRow.kind == "market",
                    SnapshotRow.provenance == "live",
                    SnapshotRow.asset_id == snapshot.asset_id,
                    SnapshotRow.fetched_at >= holder_time - HISTORY_MAX - WINDOW,
                    SnapshotRow.fetched_at <= holder_time,
                )
                .order_by(SnapshotRow.fetched_at.desc())
                .limit(100)
            ).all()
        for row in rows:
            item = MarketSnapshot.model_validate_json(row.payload)
            if (
                item.pair_id == snapshot.pair_id
                and item.holder_count is not None
                and item.holder_observed_at is not None
                and item.price is not None
                and HISTORY_MIN <= holder_time - item.holder_observed_at <= HISTORY_MAX
            ):
                return item
        return None

    async def observe_entry(self, snapshot: MarketSnapshot) -> tuple[MarketSnapshot, list[dict]]:
        mint = snapshot.asset_id.split(":", 1)[1]
        notes = []
        decimals = await self._decimals(mint)
        target = 2 * max(MIN_ORDER, self.settings.paper_initial_cash * POSITION_CAP)
        buy = await self.quotes.ladder(mint, decimals, "buy", target)
        updates: dict = {
            "observed_at": buy.observed_at,
            "price": buy.reference_price,
            "buy_quote": buy,
        }
        holders = None
        if self.oversized.get(mint, 0) > time.monotonic():
            notes.append(
                {
                    "asset_id": snapshot.asset_id,
                    "source": "solana",
                    "reason": "HOLDER_SCAN_TOO_LARGE_RECENTLY",
                }
            )
        else:
            try:
                pools = {snapshot.pair_id}
                try:
                    pools |= {x.pair_id for x in await self.dex.token("solana", mint)}
                except DataError as exc:
                    notes.append(
                        {
                            "asset_id": snapshot.asset_id,
                            "source": "dexscreener_pools",
                            "reason": exc.code,
                        }
                    )
                scan = await self.solana.scan_mint(mint, pools)
                holders = scan["top_owners"]
                updates |= {
                    "holder_count": scan["holder_count"],
                    "holder_observed_at": scan["observed_at"],
                    "largest_token_account_share": scan["largest_token_account_share"],
                    "onchain_verified": True,
                    "onchain_observed_at": scan["observed_at"],
                    "contract_safe": scan["contract_safe"],
                    "contract_source": scan["contract_source"],
                }
                previous = self.previous(snapshot, scan["observed_at"])
                if previous is not None:
                    updates |= {
                        "previous_holder_count": previous.holder_count,
                        "previous_holder_observed_at": previous.holder_observed_at,
                        "previous_price": previous.price,
                        "previous_liquidity_usd": previous.liquidity_usd,
                    }
            except DataError as exc:
                if exc.code in {"DATA_RESPONSE_TOO_LARGE", "HOLDER_SCAN_INCOMPLETE_OR_TOO_LARGE"}:
                    self.oversized[mint] = time.monotonic() + OVERSIZED_RETRY_SECONDS
                notes.append(
                    {"asset_id": snapshot.asset_id, "source": "solana", "reason": exc.code}
                )
        try:
            trades = await self.trades.observe(snapshot.pair_id, mint, buy.observed_at, holders)
            updates |= {
                "windows": trades["windows"],
                "large_holder_selling": trades["large_holder_selling"],
            }
        except DataError as exc:
            notes.append(
                {"asset_id": snapshot.asset_id, "source": "geckoterminal", "reason": exc.code}
            )
        return MarketSnapshot.model_validate(snapshot.model_dump() | updates), notes

    async def observe_exit(self, snapshot: MarketSnapshot, quantity: Decimal) -> MarketSnapshot:
        """Executable bids for the held quantity; marks the position at the bid."""
        mint = snapshot.asset_id.split(":", 1)[1]
        sell = await self.quotes.ladder(mint, await self._decimals(mint), "sell", quantity)
        return MarketSnapshot.model_validate(
            snapshot.model_dump()
            | {"observed_at": sell.observed_at, "price": sell.reference_price, "sell_quote": sell}
        )
