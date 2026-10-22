from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx

from trading_team.domain import (
    ExecutionQuote,
    Level,
    MarketSnapshot,
    PredictionPrice,
    PredictionQuestion,
    PredictionResolution,
    VenueTarget,
    stable_hash,
    utcnow,
)
from trading_team.settings import Settings
from trading_team.storage import Database, SnapshotRow


class DataError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def decimal_or_none(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def epoch_millis(value) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        return None


class PublicDataClient:
    def __init__(self, db: Database | None = None, client: httpx.AsyncClient | None = None):
        self.db = db
        self.client = client or httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            trust_env=True,
            headers={"User-Agent": "Universal-AI-Trading-Team/0.1 paper-research"},
        )
        self.owns_client = client is None

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        kind: str,
        json_body=None,
        params=None,
        headers=None,
        summarize=None,
    ):
        body = bytearray()
        try:
            async with self.client.stream(
                method, url, json=json_body, params=params, headers=headers
            ) as response:
                if response.status_code != 200:
                    raise DataError(f"DATA_HTTP_{response.status_code}")
                # Stop reading oversized bodies instead of downloading them in full.
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 10_000_000:
                        raise DataError("DATA_RESPONSE_TOO_LARGE")
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise DataError("DATA_TRANSPORT_FAILED") from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise DataError("DATA_INVALID_JSON") from exc
        if not isinstance(data, (dict, list)):
            raise DataError("DATA_INVALID_SHAPE")
        if self.db:
            now = utcnow()
            envelope = {
                "source": kind,
                "url": url,
                "params": params,
                "fetched_at": now.isoformat(),
                "payload": data,
            }
            if summarize is not None:
                # Large payloads keep a digest and the derived counts, not every account row.
                envelope["payload"] = summarize(data)
                envelope["payload_sha256"] = stable_hash(data)
            digest = stable_hash(envelope)
            with self.db.transaction() as s:
                if not s.get(SnapshotRow, digest):
                    s.add(
                        SnapshotRow(
                            id=digest,
                            asset_id="",
                            kind=kind,
                            source=url,
                            provenance="live",
                            fetched_at=now,
                            observed_at=None,
                            payload=json.dumps(envelope, ensure_ascii=False),
                        )
                    )
        return data


class DexScreener:
    BASE = "https://api.dexscreener.com"

    def __init__(self, data: PublicDataClient):
        self.data = data

    @staticmethod
    def normalize(pair: dict, fetched_at: datetime) -> MarketSnapshot:
        chain = pair.get("chainId")
        mint = pair.get("baseToken", {}).get("address")
        pair_id = pair.get("pairAddress")
        if not all(isinstance(x, str) and x for x in [chain, mint, pair_id]):
            raise DataError("DEX_PAIR_ID_MISSING")
        liquidity = pair.get("liquidity") or {}
        volume = pair.get("volume") or {}
        # DexScreener does not publish a quote/observation timestamp or holder count here.
        # Keep it unknown; fetched_at is never presented as source freshness.
        return MarketSnapshot(
            asset_id=f"{chain}:{mint}",
            symbol=str(pair.get("baseToken", {}).get("symbol") or mint),
            chain=chain,
            pair_id=pair_id,
            source="dexscreener",
            source_reference=f"{DexScreener.BASE}/latest/dex/pairs/{chain}/{pair_id}",
            fetched_at=fetched_at,
            observed_at=None,
            price=decimal_or_none(pair.get("priceUsd")),
            liquidity_usd=decimal_or_none(liquidity.get("usd")),
            volume_h6=decimal_or_none(volume.get("h6")),
            volume_h24=decimal_or_none(volume.get("h24")),
            market_cap=decimal_or_none(pair.get("marketCap")),
            pair_created_at=epoch_millis(pair.get("pairCreatedAt")),
            price_change_h1_percent=decimal_or_none((pair.get("priceChange") or {}).get("h1")),
        )

    async def search(self, query: str) -> list[MarketSnapshot]:
        if not query.strip() or len(query) > 100:
            raise DataError("DEX_QUERY_INVALID")
        body = await self.data.request(
            "GET", self.BASE + "/latest/dex/search", kind="dex_search", params={"q": query}
        )
        if not isinstance(body, dict) or not isinstance(body.get("pairs"), list):
            raise DataError("DEX_PAIRS_UNAVAILABLE")
        now = utcnow()
        result = []
        for item in body["pairs"][:100]:
            try:
                result.append(self.normalize(item, now))
            except (DataError, ValueError, TypeError):
                continue
        return result

    async def token(self, chain: str, mint: str) -> list[MarketSnapshot]:
        if chain != "solana" or not mint.isalnum() or len(mint) > 60:
            raise DataError("DEX_ORIGINAL_TOKEN_INVALID")
        body = await self.data.request(
            "GET", self.BASE + "/latest/dex/tokens/" + quote(mint, safe=""), kind="dex_token"
        )
        if not isinstance(body, dict) or not isinstance(body.get("pairs"), list):
            raise DataError("DEX_PAIRS_UNAVAILABLE")
        now = utcnow()
        return [
            self.normalize(p, now)
            for p in body["pairs"]
            if p.get("chainId") == chain and p.get("baseToken", {}).get("address") == mint
        ]


class MarketDataFeed:
    """Trusted operator-selected observations, restricted to the Dex-discovered asset/pool IDs."""

    def __init__(self, settings: Settings, data: PublicDataClient):
        self.settings, self.data = settings, data

    async def enrich(self, discovered: list[MarketSnapshot]) -> list[MarketSnapshot]:
        if not self.settings.market_data_feed_url or not discovered:
            return discovered
        key = self.settings.market_data_feed_api_key.get_secret_value()
        body = await self.data.request(
            "GET",
            self.settings.market_data_feed_url,
            kind="verified_market_feed",
            params={"pairs": ",".join(sorted({x.pair_id for x in discovered}))},
            headers={"X-API-Key": key} if key else None,
        )
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("snapshots"), list)
            or len(body["snapshots"]) > 100
        ):
            raise DataError("MARKET_FEED_INVALID_SHAPE")
        expected = {(x.asset_id, x.pair_id): x for x in discovered}
        enriched = {}
        now = utcnow()
        for value in body["snapshots"]:
            try:
                sample = MarketSnapshot.model_validate(value)
            except (ValueError, TypeError):
                raise DataError("MARKET_FEED_SCHEMA_INVALID") from None
            identity = sample.asset_id, sample.pair_id
            if (
                identity not in expected
                or sample.chain != expected[identity].chain
                or sample.provenance != "live"
            ):
                raise DataError("MARKET_FEED_IDENTITY_OR_PROVENANCE_MISMATCH")
            if identity in enriched:
                raise DataError("MARKET_FEED_DUPLICATE_POOL")
            # Collection time is our clock. Provider observation times remain unchanged and can fail freshness gates.
            enriched[identity] = sample.model_copy(update={"fetched_at": now})
        return [enriched.get((sample.asset_id, sample.pair_id), sample) for sample in discovered]


class WorldMonitor:
    PATHS = {
        "fear_greed": "/api/market/v1/get-fear-greed-index",
        "prediction": "/api/prediction/v1/list-prediction-markets",
        "signals": "/api/intelligence/v1/list-cross-source-signals",
    }

    def __init__(self, settings: Settings, data: PublicDataClient):
        self.base = settings.worldmonitor_base_url
        self.data = data

    @staticmethod
    def recent(timestamp, seconds):
        observed = epoch_millis(timestamp)
        return observed is not None and 0 <= (utcnow() - observed).total_seconds() <= seconds

    @staticmethod
    def fear_inputs(value):
        def numeric(item):
            if isinstance(item, dict):
                return any(numeric(child) for child in item.values())
            if isinstance(item, list):
                return any(numeric(child) for child in item)
            return (
                not isinstance(item, bool)
                and isinstance(item, (int, float))
                and decimal_or_none(item) is not None
            )

        count = 0
        for name in (
            "sentiment",
            "volatility",
            "positioning",
            "trend",
            "breadth",
            "momentum",
            "liquidity",
            "credit",
            "macro",
            "crossAsset",
        ):
            category = value.get(name)
            if not isinstance(category, dict):
                continue
            try:
                inputs = json.loads(category.get("inputsJson", "{}"))
            except (ValueError, TypeError):
                continue
            if isinstance(inputs, dict) and numeric(inputs):
                count += 1
        return count

    async def readiness(self) -> dict:
        async def check(name, path):
            try:
                value = await self.data.request(
                    "GET",
                    self.base + path,
                    kind="wm_" + name,
                    params={"page_size": 1} if name == "prediction" else None,
                )
                if not isinstance(value, dict):
                    return name, {"ready": False, "reason": "WM_INVALID_SHAPE"}
                if name == "prediction":
                    ready = (
                        value.get("dataAvailable") is True
                        and bool(value.get("fetchedAt"))
                        and bool(value.get("markets"))
                        and self.recent(value.get("fetchedAt"), 90 * 60)
                    )
                elif name == "fear_greed":
                    try:
                        timestamp = datetime.fromisoformat(
                            value.get("seededAt", "").replace("Z", "+00:00")
                        )
                        fresh = (
                            timestamp.tzinfo is not None
                            and 0 <= (utcnow() - timestamp).total_seconds() <= 18 * 3600
                        )
                    except (ValueError, TypeError, AttributeError):
                        fresh = False
                    score = decimal_or_none(value.get("compositeScore"))
                    ready = (
                        value.get("unavailable") is False
                        and score is not None
                        and 0 <= score <= 100
                        and fresh
                        and self.fear_inputs(value) > 0
                    )
                else:
                    ready = (
                        isinstance(value.get("signals"), list)
                        and bool(value["signals"])
                        and not value.get("unavailable", False)
                        and self.recent(value.get("evaluatedAt"), 30 * 60)
                        and any(
                            isinstance(signal, dict)
                            and self.recent(signal.get("detectedAt"), 48 * 3600)
                            and signal.get("id")
                            and signal.get("summary")
                            for signal in value["signals"]
                        )
                    )
                return name, {
                    "ready": ready,
                    "reason": "WM_DATA_PRESENT" if ready else "WM_EMPTY_OR_UNAVAILABLE",
                }
            except DataError as exc:
                return name, {"ready": False, "reason": exc.code}

        checks = dict(await asyncio.gather(*(check(n, p) for n, p in self.PATHS.items())))
        return {
            "status": "READY" if all(x["ready"] for x in checks.values()) else "degraded",
            "checks": checks,
            "hosted_fallback": False,
        }

    async def discovery(self) -> dict:
        body = await self.data.request(
            "GET",
            self.base + self.PATHS["prediction"],
            kind="wm_prediction_discovery",
            params={"page_size": 100},
        )
        if (
            not isinstance(body, dict)
            or body.get("dataAvailable") is not True
            or not body.get("fetchedAt")
            or not self.recent(body.get("fetchedAt"), 90 * 60)
        ):
            raise DataError("WM_PREDICTION_UNAVAILABLE")
        # Price-blind discovery. Never pass upstream yesPrice/outcomePrices to BOOK.
        allowed = {"id", "title", "source", "url", "category", "closesAt"}
        markets = [
            {k: v for k, v in m.items() if k in allowed}
            for m in body.get("markets", [])
            if isinstance(m, dict)
        ]
        return {"markets": markets, "fetched_at": body["fetchedAt"]}

    async def events(self) -> dict:
        value = await self.data.request("GET", self.base + self.PATHS["signals"], kind="wm_signals")
        if not isinstance(value, dict) or not isinstance(value.get("signals"), list):
            raise DataError("WM_SIGNALS_INVALID")
        if not self.recent(value.get("evaluatedAt"), 30 * 60):
            raise DataError("WM_SIGNALS_STALE")
        return value


class PredictionMarkets:
    """GET-only public data surfaces, deliberately no order/client-credential methods."""

    def __init__(self, data: PublicDataClient):
        self.data = data

    async def kalshi(self) -> dict:
        result = await self.data.request(
            "GET",
            "https://external-api.kalshi.com/trade-api/v2/markets",
            kind="kalshi_markets",
            params={"limit": 100, "status": "open"},
        )
        if not isinstance(result, dict) or not isinstance(result.get("markets"), list):
            raise DataError("KALSHI_MARKETS_INVALID")
        return result

    async def polymarket(self) -> list:
        result = await self.data.request(
            "GET",
            "https://gamma-api.polymarket.com/markets",
            kind="polymarket_markets",
            params={"closed": "false", "order": "volumeNum", "limit": 50},
        )
        if not isinstance(result, list):
            raise DataError("POLYMARKET_MARKETS_INVALID")
        return result

    async def metadata(self, target: VenueTarget) -> dict:
        if target.venue == "kalshi":
            value = await self.data.request(
                "GET",
                f"https://external-api.kalshi.com/trade-api/v2/markets/{target.market_id}",
                kind="kalshi_market_metadata",
            )
            value = value.get("market") if isinstance(value, dict) else None
        else:
            value = await self.data.request(
                "GET",
                f"https://gamma-api.polymarket.com/markets/{target.market_id}",
                kind="polymarket_market_metadata",
            )
        if not isinstance(value, dict):
            raise DataError("PREDICTION_METADATA_UNAVAILABLE")
        return value

    async def question_metadata(self, venue: str, market_id: str) -> tuple[dict, list[VenueTarget]]:
        target = VenueTarget(venue=venue, market_id=market_id, outcome="yes")
        value = await self.metadata(target)
        if venue == "kalshi":
            if value.get("ticker") != market_id or value.get("status") not in {"open", "active"}:
                raise DataError("PREDICTION_MARKET_NOT_OPEN")
            title = value.get("title")
            rules = "\n".join(
                x
                for x in [value.get("rules_primary"), value.get("rules_secondary")]
                if isinstance(x, str) and x
            )
            closes = value.get("close_time")
        else:
            if str(value.get("id")) != market_id or value.get("closed") is not False:
                raise DataError("PREDICTION_MARKET_NOT_OPEN")
            title, rules, closes = (
                value.get("question"),
                value.get("description"),
                value.get("endDate"),
            )
        if not all(isinstance(x, str) and x.strip() for x in (title, rules, closes)):
            raise DataError("PREDICTION_RULES_OR_CLOSE_TIME_UNKNOWN")
        return {
            "question_id": f"{venue}:{market_id}",
            "title": title,
            "resolution_rules": rules,
            "closes_at": datetime.fromisoformat(closes.replace("Z", "+00:00")),
        }, [target, target.model_copy(update={"outcome": "no"})]

    async def kalshi_quote(self, target: VenueTarget, asset_id: str, side: str) -> ExecutionQuote:
        book = await self.data.request(
            "GET",
            f"https://external-api.kalshi.com/trade-api/v2/markets/{target.market_id}/orderbook",
            kind="kalshi_book",
        )
        if not isinstance(book, dict):
            raise DataError("PREDICTION_QUOTE_INVALID")
        timestamp = book.get("ts") or book.get("timestamp")
        if not isinstance(timestamp, str):
            raise DataError("PREDICTION_QUOTE_TIMESTAMP_UNKNOWN")
        opposite = "no" if target.outcome == "yes" else "yes"
        key = (opposite if side == "buy" else target.outcome) + "_dollars"
        levels = [
            Level(price=Decimal(1) - Decimal(row[0]) if side == "buy" else row[0], quantity=row[1])
            for row in (book.get("orderbook_fp") or {}).get(key, [])
        ]
        levels.sort(key=lambda x: x.price, reverse=side == "sell")
        if not levels:
            raise DataError("PREDICTION_QUOTE_EMPTY")
        return ExecutionQuote(
            asset_id=asset_id,
            side=side,
            reference_price=levels[0].price,
            levels=levels,
            observed_at=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
            source="kalshi_public_book",
            source_reference=target.market_id,
        )

    @staticmethod
    def _token(value: dict, outcome: str) -> str:
        tokens, outcomes = value.get("clobTokenIds"), value.get("outcomes")
        tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        if (
            not isinstance(tokens, list)
            or not isinstance(outcomes, list)
            or len(tokens) != len(outcomes)
        ):
            raise DataError("PREDICTION_OUTCOMES_UNAVAILABLE")
        matches = [index for index, name in enumerate(outcomes) if str(name).lower() == outcome]
        if len(matches) != 1:
            raise DataError("PREDICTION_OUTCOME_MISMATCH")
        return str(tokens[matches[0]])

    async def risk_observation(
        self, asset_id: str
    ) -> tuple[MarketSnapshot | None, PredictionResolution | None]:
        parts = asset_id.split(":")
        if len(parts) != 4 or parts[0] != "prediction":
            raise DataError("PREDICTION_ASSET_INVALID")
        target = VenueTarget(venue=parts[1], market_id=parts[2], outcome=parts[3])
        value = await self.metadata(target)
        resolved = None
        resolved_at = None
        if target.venue == "kalshi":
            if value.get("ticker") != target.market_id:
                raise DataError("PREDICTION_OUTCOME_MISMATCH")
            if value.get("status") == "finalized" and value.get("result") in {"yes", "no"}:
                payout_yes = decimal_or_none(value.get("settlement_value_dollars"))
                timestamp = value.get("settlement_ts")
                if payout_yes == (1 if value["result"] == "yes" else 0) and isinstance(
                    timestamp, str
                ):
                    resolved_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if resolved_at.tzinfo is not None and resolved_at <= utcnow():
                        resolved = value["result"]
            if resolved is None and value.get("status") not in {"active", "open"}:
                return None, None
        else:
            if str(value.get("id")) != target.market_id:
                raise DataError("PREDICTION_OUTCOME_MISMATCH")
            if value.get("closed") is True and value.get("umaResolutionStatus") == "resolved":
                prices, outcomes = value.get("outcomePrices"), value.get("outcomes")
                prices = json.loads(prices) if isinstance(prices, str) else prices
                outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                if (
                    isinstance(prices, list)
                    and isinstance(outcomes, list)
                    and len(prices) == len(outcomes) == 2
                    and {Decimal(str(x)) for x in prices} == {Decimal(0), Decimal(1)}
                    and {str(x).lower() for x in outcomes} == {"yes", "no"}
                ):
                    resolved = str(
                        outcomes[prices.index(next(x for x in prices if Decimal(str(x)) == 1))]
                    ).lower()
        now = utcnow()
        if resolved is not None:
            return None, PredictionResolution(
                asset_id=asset_id,
                payout=1 if resolved == target.outcome else 0,
                fetched_at=now,
                resolved_at=resolved_at,
                source=target.venue + "_public_resolution",
                source_reference=target.market_id,
            )
        quote = (
            await self.kalshi_quote(target, asset_id, "sell")
            if target.venue == "kalshi"
            else await self.polymarket_quote(self._token(value, target.outcome), asset_id, "sell")
        )
        return MarketSnapshot(
            asset_id=asset_id,
            symbol=str(value.get("title") or value.get("question") or target.market_id),
            chain="prediction",
            pair_id=target.market_id,
            source=target.venue,
            source_reference=target.market_id,
            fetched_at=now,
            observed_at=quote.observed_at,
            price=quote.reference_price,
            sell_quote=quote,
        ), None

    async def polymarket_quote(self, token_id: str, asset_id: str, side: str) -> ExecutionQuote:
        if side not in {"buy", "sell"} or not token_id.isdigit() or len(token_id) > 150:
            raise DataError("PREDICTION_QUOTE_ARGUMENT_INVALID")
        result = await self.data.request(
            "GET",
            "https://clob.polymarket.com/book",
            kind="polymarket_book",
            params={"token_id": token_id},
        )
        if not isinstance(result, dict) or result.get("asset_id") != token_id:
            raise DataError("PREDICTION_QUOTE_ASSET_MISMATCH")
        timestamp = epoch_millis(int(result.get("timestamp", 0)))
        if timestamp is None:
            raise DataError("PREDICTION_QUOTE_TIMESTAMP_UNKNOWN")
        levels = [
            Level(price=x["price"], quantity=x["size"])
            for x in result.get("asks" if side == "buy" else "bids", [])
        ]
        levels.sort(key=lambda x: x.price, reverse=side == "sell")
        if not levels:
            raise DataError("PREDICTION_QUOTE_EMPTY")
        return ExecutionQuote(
            asset_id=asset_id,
            side=side,
            reference_price=levels[0].price,
            levels=levels,
            observed_at=timestamp,
            source="polymarket_public_book",
            source_reference=token_id,
        )

    async def prices_for(
        self, question: PredictionQuestion, targets: list[VenueTarget]
    ) -> list[PredictionPrice]:
        """Called only after BOOK's probability is committed. Exact metadata match or skip."""
        prices = []
        for target in targets[:10]:
            try:
                if target.venue == "polymarket":
                    result = await self.data.request(
                        "GET",
                        f"https://gamma-api.polymarket.com/markets/{target.market_id}",
                        kind="polymarket_market",
                    )
                    if not isinstance(result, dict) or result.get("closed") is not False:
                        continue
                    title, rules = result.get("question"), result.get("description")
                    closes = result.get("endDate")
                    tokens, outcomes = result.get("clobTokenIds"), result.get("outcomes")
                    tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
                    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                    if (
                        not isinstance(tokens, list)
                        or not isinstance(outcomes, list)
                        or len(tokens) != len(outcomes)
                    ):
                        continue
                    matches = [
                        i
                        for i, value in enumerate(outcomes)
                        if str(value).lower() == target.outcome
                    ]
                    if len(matches) != 1:
                        continue
                else:
                    result = await self.data.request(
                        "GET",
                        f"https://external-api.kalshi.com/trade-api/v2/markets/{target.market_id}",
                        kind="kalshi_market",
                    )
                    result = result.get("market") if isinstance(result, dict) else None
                    if not isinstance(result, dict) or result.get("status") not in {
                        "open",
                        "active",
                    }:
                        continue
                    title = result.get("title")
                    rules = "\n".join(
                        x for x in [result.get("rules_primary"), result.get("rules_secondary")] if x
                    )
                    closes = result.get("close_time")
                if title != question.title or rules != question.resolution_rules or not closes:
                    continue
                if datetime.fromisoformat(closes.replace("Z", "+00:00")) != question.closes_at:
                    continue
                asset_id = f"prediction:{target.venue}:{target.market_id}:{target.outcome}"
                if target.venue == "polymarket":
                    execution = await self.polymarket_quote(
                        str(tokens[matches[0]]), asset_id, "buy"
                    )
                else:
                    book = await self.data.request(
                        "GET",
                        f"https://external-api.kalshi.com/trade-api/v2/markets/{target.market_id}/orderbook",
                        kind="kalshi_book",
                    )
                    # A timestamp-free book is useful for display but not an execution observation.
                    timestamp = book.get("ts") or book.get("timestamp")
                    if not isinstance(timestamp, str):
                        continue
                    observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    opposite = "no_dollars" if target.outcome == "yes" else "yes_dollars"
                    levels = [
                        Level(price=Decimal(1) - Decimal(x[0]), quantity=x[1])
                        for x in (book.get("orderbook_fp") or {}).get(opposite, [])
                    ]
                    levels.sort(key=lambda x: x.price)
                    if not levels:
                        continue
                    execution = ExecutionQuote(
                        asset_id=asset_id,
                        side="buy",
                        reference_price=levels[0].price,
                        levels=levels,
                        observed_at=observed,
                        source="kalshi_public_book",
                        source_reference=target.market_id,
                    )
                prices.append(
                    PredictionPrice(
                        question_id=question.question_id,
                        venue=target.venue,
                        venue_market_id=target.market_id,
                        outcome=target.outcome,
                        ask=execution.reference_price,
                        observed_at=execution.observed_at,
                        matching_verified=True,
                        resolution_hash=stable_hash(question.resolution_rules),
                        quote=execution,
                    )
                )
            except (DataError, ValueError, KeyError, TypeError):
                continue  # A venue failure never creates a synthetic quote or alternative match.
        return prices


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    number, text = int.from_bytes(raw, "big"), ""
    while number:
        number, rest = divmod(number, 58)
        text = B58[rest] + text
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + text


class SolanaOnchain:
    METHODS = {"getAccountInfo", "getBlockTime", "getProgramAccounts"}
    TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    # Token-2022 extensions without transfer fees, hooks, delegates, freezes or pausing.
    SAFE_2022_EXTENSIONS = {"metadataPointer", "tokenMetadata"}
    # Shared vault authorities, each checked against a live pool's vault on 2026-10-22.
    # Pump.fun, PumpSwap, Orca, Meteora DLMM and Raydium CLMM vaults belong to the pool itself.
    POOL_AUTHORITIES = {
        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4
        "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM
        "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",  # Meteora DAMM v2
        "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM",  # Meteora DBC
    }
    MAX_ACCOUNTS = 30_000

    def __init__(self, settings: Settings, data: PublicDataClient):
        self.url = settings.solana_rpc_url
        self.data = data

    async def rpc(self, method: str, params: list, summarize=None):
        if method not in self.METHODS:
            raise DataError("RPC_METHOD_NOT_ALLOWED")
        value = await self.data.request(
            "POST",
            self.url,
            kind="solana_" + method,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            summarize=summarize,
        )
        if not isinstance(value, dict) or value.get("error") or "result" not in value:
            raise DataError("SOLANA_RPC_ERROR")
        return value["result"]

    async def block_time(self, slot: int) -> datetime:
        value = await self.rpc("getBlockTime", [slot])
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DataError("SOLANA_OBSERVATION_TIME_UNKNOWN")
        return datetime.fromtimestamp(value, UTC)

    @staticmethod
    def _scan_summary(body):
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            return {"error": "unavailable"}
        accounts = result.get("value")
        return {
            "context": result.get("context"),
            "token_accounts": len(accounts) if isinstance(accounts, list) else None,
        }

    async def mint_account(self, mint: str) -> tuple[dict, str, dict]:
        if not mint.isalnum() or not 32 <= len(mint) <= 44:
            raise DataError("SOLANA_MINT_INVALID")
        account = await self.rpc(
            "getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        )
        value = account.get("value") or {}
        program = value.get("owner")
        parsed = (value.get("data") or {}).get("parsed") or {}
        info = parsed.get("info") or {}
        if (
            program not in {self.TOKEN_PROGRAM, self.TOKEN_2022_PROGRAM}
            or parsed.get("type") != "mint"
        ):
            raise DataError("SOLANA_MINT_PROGRAM_UNSUPPORTED")
        try:
            if int(info["supply"]) <= 0 or not 0 <= int(info["decimals"]) <= 18:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise DataError("SOLANA_SUPPLY_UNKNOWN") from None
        return account, program, info

    async def scan_mint(self, mint: str, pools: set[str]) -> dict:
        """Holders, the largest non-pool owner and mint authorities from one aligned scan.

        Every token account of the mint is read with only its owner and amount bytes.
        Liquidity pool vaults are not wallets and are excluded from holders and concentration.
        """
        account, program, info = await self.mint_account(mint)
        supply = int(info["supply"])
        filters = [{"memcmp": {"offset": 0, "bytes": mint}}]
        if program == self.TOKEN_PROGRAM:
            filters.insert(0, {"dataSize": 165})
        scan = await self.rpc(
            "getProgramAccounts",
            [
                program,
                {
                    "encoding": "base64",
                    "commitment": "confirmed",
                    "withContext": True,
                    "dataSlice": {"offset": 32, "length": 40},
                    "filters": filters,
                },
            ],
            summarize=self._scan_summary,
        )
        rows = scan.get("value") if isinstance(scan, dict) else None
        if not isinstance(rows, list) or len(rows) > self.MAX_ACCOUNTS:
            raise DataError("HOLDER_SCAN_INCOMPLETE_OR_TOO_LARGE")
        excluded = set(pools) | self.POOL_AUTHORITIES
        owners: dict[str, int] = {}
        try:
            for row in rows:
                raw = base64.b64decode(row["account"]["data"][0], validate=True)
                if len(raw) != 40:
                    raise ValueError
                amount = int.from_bytes(raw[32:], "little")
                owner = b58encode(raw[:32])
                if amount and owner not in excluded:
                    owners[owner] = owners.get(owner, 0) + amount
            slots = [int(account["context"]["slot"]), int(scan["context"]["slot"])]
        except (KeyError, IndexError, TypeError, ValueError):
            raise DataError("HOLDER_SCAN_INVALID") from None
        if abs(slots[0] - slots[1]) > 150:
            raise DataError("SOLANA_OBSERVATIONS_NOT_ALIGNED")
        observed_at = await self.block_time(min(slots))
        ranked = sorted(owners.items(), key=lambda item: item[1], reverse=True)
        extensions = {
            x.get("extension") for x in info.get("extensions") or [] if isinstance(x, dict)
        }
        safe = (
            info.get("isInitialized") is True
            and info.get("mintAuthority") is None
            and info.get("freezeAuthority") is None
            and (program == self.TOKEN_PROGRAM or extensions <= self.SAFE_2022_EXTENSIONS)
        )
        return {
            "holder_count": len(owners),
            "largest_token_account_share": Decimal(ranked[0][1]) / Decimal(supply)
            if ranked
            else Decimal(0),
            "top_owners": [owner for owner, _ in ranked[:20]],
            "observed_at": observed_at,
            "contract_safe": safe,
            "contract_source": "solana_mint_authorities_and_token2022_extensions",
            "proxy_definition": "largest non-pool owner (all its token accounts) / total supply",
            "token_accounts": len(rows),
            "decimals": int(info["decimals"]),
        }
