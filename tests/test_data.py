from __future__ import annotations

import base64
import json
from decimal import Decimal as D

import httpx
import pytest

from trading_team.data import (
    DataError,
    DexScreener,
    PredictionMarkets,
    PublicDataClient,
    SolanaOnchain,
    WorldMonitor,
    b58encode,
)
from trading_team.domain import utcnow


def test_dex_missing_data_kept_unknown():
    pair = {
        "chainId": "solana",
        "pairAddress": "pool",
        "baseToken": {"address": "mint", "symbol": "TEST"},
        "priceUsd": ".1",
        "liquidity": {"usd": 20000},
        "volume": {"h6": 100, "h24": 1000},
        "holders": 999999,
    }
    sample = DexScreener.normalize(pair, utcnow())
    assert sample.observed_at is None and sample.holder_count is None
    assert sample.volume_h6 == 100 and sample.volume_h24 == 1000
    assert not sample.fresh(utcnow()) and sample.buy_quote is None


@pytest.mark.parametrize("ready", [True, False])
async def test_worldmonitor_readiness_and_price_blind_discovery(settings, ready):
    now = utcnow()
    milliseconds = int(now.timestamp() * 1000)

    def handler(request):
        if "list-prediction" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "dataAvailable": ready,
                    "fetchedAt": milliseconds if ready else 0,
                    "markets": [
                        {
                            "id": "q",
                            "title": "question",
                            "yesPrice": 0.9,
                            "outcomePrices": [0.9, 0.1],
                            "volume": 100,
                            "price": 0.9,
                        }
                    ]
                    if ready
                    else [],
                },
            )
        if "fear" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "compositeScore": 50,
                    "unavailable": False,
                    "seededAt": now.isoformat(),
                    "sentiment": {"inputsJson": '{"observed_index":50}'},
                }
                if ready
                else {},
            )
        return httpx.Response(
            200 if ready else 503,
            json={
                "signals": [
                    {"id": "s", "summary": "Recent observed signal", "detectedAt": milliseconds}
                ],
                "evaluatedAt": milliseconds,
            }
            if ready
            else {"error": "seed missing"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        data = PublicDataClient(client=client)
        wm = WorldMonitor(settings, data)
        status = await wm.readiness()
        assert (status["status"] == "READY") is ready
        assert not status["hosted_fallback"]
        if ready:
            discovery = await wm.discovery()
            assert discovery["markets"] == [{"id": "q", "title": "question"}]
        else:
            with pytest.raises(DataError, match="WM_PREDICTION_UNAVAILABLE"):
                await wm.discovery()


@pytest.mark.parametrize("status", [429, 451, 500])
async def test_external_errors_remain_explicit(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="sensitive upstream response")
        )
    ) as client:
        with pytest.raises(DataError) as error:
            await PublicDataClient(client=client).request("GET", "https://example.com", kind="test")
    assert error.value.code == f"DATA_HTTP_{status}"
    assert "sensitive" not in str(error.value)


def token_account(owner: bytes, amount: int) -> dict:
    return {"account": {"data": [base64.b64encode(owner + amount.to_bytes(8, "little")).decode()]}}


async def test_solana_holder_scan_excludes_pools_and_checks_token2022(settings):
    mint = "M" * 32
    pool, wallet, small = b"\x01" * 32, b"\x02" * 32, b"\x03" * 32
    methods, extensions = [], [{"extension": "metadataPointer"}, {"extension": "tokenMetadata"}]

    def handler(request):
        body = json.loads(request.content)
        methods.append(body["method"])
        if body["method"] == "getAccountInfo":
            result = {
                "context": {"slot": 100},
                "value": {
                    "owner": SolanaOnchain.TOKEN_2022_PROGRAM,
                    "data": {
                        "parsed": {
                            "type": "mint",
                            "info": {
                                "isInitialized": True,
                                "mintAuthority": None,
                                "freezeAuthority": None,
                                "supply": "1000",
                                "decimals": 6,
                                "extensions": extensions,
                            },
                        }
                    },
                },
            }
        elif body["method"] == "getProgramAccounts":
            assert body["params"][0] == SolanaOnchain.TOKEN_2022_PROGRAM
            assert body["params"][1]["dataSlice"] == {"offset": 32, "length": 40}
            rows = [token_account(pool, 600), token_account(wallet, 30)]
            rows += [token_account(wallet, 10), token_account(small, 5), token_account(small, 0)]
            result = {"context": {"slot": 101}, "value": rows}
        else:
            result = 1792454400
        return httpx.Response(200, json={"result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SolanaOnchain(settings, PublicDataClient(client=client))
        value = await adapter.scan_mint(mint, {b58encode(pool)})
        # The pool vault is not a wallet; one owner's two token accounts are combined.
        assert value["holder_count"] == 2 and value["token_accounts"] == 5
        assert value["largest_token_account_share"] == D("0.04") and value["contract_safe"]
        assert value["top_owners"] == [b58encode(wallet), b58encode(small)]
        assert value["observed_at"].timestamp() == 1792454400 and value["decimals"] == 6
        # Including the pool as a holder makes it the largest owner.
        assert (await adapter.scan_mint(mint, set()))["largest_token_account_share"] == D("0.6")
        extensions.append({"extension": "transferHook"})
        assert not (await adapter.scan_mint(mint, set()))["contract_safe"]
        with pytest.raises(DataError, match="RPC_METHOD_NOT_ALLOWED"):
            await adapter.rpc("sendTransaction", [])
    assert "sendTransaction" not in methods and "getTokenLargestAccounts" not in methods


async def test_oversized_response_stops_reading():
    chunks = [b'{"a":"'] + [b"x" * 4_000_000] * 5 + [b'"}']

    class Stream(httpx.AsyncByteStream):
        def __init__(self):
            self.sent = 0

        async def __aiter__(self):
            for chunk in chunks:
                self.sent += 1
                yield chunk

    stream = Stream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as client:
        with pytest.raises(DataError, match="DATA_RESPONSE_TOO_LARGE"):
            await PublicDataClient(client=client).request("GET", "https://example.com", kind="t")
    assert stream.sent == 4  # Not all seven chunks.


async def test_polymarket_execution_depth_has_producer_timestamp():
    body = {
        "asset_id": "123",
        "timestamp": "1792454400000",
        "asks": [{"price": ".55", "size": "10"}, {"price": ".5", "size": "20"}],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        adapter = PredictionMarkets(PublicDataClient(client=client))
        result = await adapter.polymarket_quote("123", "prediction:polymarket:1:yes", "buy")
        assert str(result.reference_price) == "0.5" and len(result.levels) == 2
        body["timestamp"] = "0"
        with pytest.raises(DataError, match="PREDICTION_QUOTE_TIMESTAMP_UNKNOWN"):
            await adapter.polymarket_quote("123", "prediction:polymarket:1:yes", "buy")
