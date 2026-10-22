from __future__ import annotations

import json

import httpx
import pytest

from trading_team.data import (
    DataError,
    DexScreener,
    PredictionMarkets,
    PublicDataClient,
    SolanaOnchain,
    WorldMonitor,
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


async def test_solana_account_proxy_and_rpc_allowlist(settings):
    methods = []

    def handler(request):
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        values = {
            "getTokenLargestAccounts": {"context": {"slot": 100}, "value": [{"amount": "40"}]},
            "getTokenSupply": {"context": {"slot": 100}, "value": {"amount": "1000"}},
            "getAccountInfo": {
                "context": {"slot": 100},
                "value": {
                    "owner": SolanaOnchain.TOKEN_PROGRAM,
                    "data": {
                        "parsed": {
                            "type": "mint",
                            "info": {
                                "isInitialized": True,
                                "mintAuthority": None,
                                "freezeAuthority": None,
                            },
                        }
                    },
                },
            },
            "getBlockTime": 1792454400,
        }
        return httpx.Response(200, json={"result": values[method]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SolanaOnchain(settings, PublicDataClient(client=client))
        value = await adapter.inspect_mint("A" * 32)
        assert str(value["largest_token_account_share"]) == "0.04" and value["contract_safe"]
        assert "not beneficial-wallet" in value["proxy_definition"]
        with pytest.raises(DataError, match="RPC_METHOD_NOT_ALLOWED"):
            await adapter.rpc("sendTransaction", [])
        with pytest.raises(DataError, match="FULL_HOLDER_SCAN_NOT_ENABLED"):
            await adapter.holder_count("A" * 32)
    assert len(methods) == 4 and "sendTransaction" not in methods


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
