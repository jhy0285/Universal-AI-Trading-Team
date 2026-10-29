from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from trading_team.notify import Notifier, fill_text
from trading_team.settings import Settings


def configured(**values) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://discord.com/api/webhooks/1/secret-token", "content"),
        ("https://hooks.slack.com/services/T/B/secret-token", "text"),
    ],
)
async def test_webhook_body_matches_the_service(url, key):
    sent = []

    def handler(request):
        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = Notifier(configured(notify_webhook_url=url), client)
        notifier.send("체결")
        await notifier.drain()
    assert sent == [(url, {key: "[PAPER] 체결"})]


async def test_telegram_and_failures_never_raise(capsys):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("offline")

    settings = configured(notify_telegram_bot_token="123:abc", notify_telegram_chat_id="-100123")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = Notifier(settings, client)
        notifier.send("halt")
        await notifier.drain()
    assert calls[0].url.path == "/bot123:abc/sendMessage"
    assert json.loads(calls[0].content) == {"chat_id": "-100123", "text": "[PAPER] halt"}
    # The failure is reported by type only, never with the token or URL.
    error = capsys.readouterr().err
    assert "ConnectError" in error and "abc" not in error


def test_disabled_by_default_and_validated():
    assert not Notifier(configured()).enabled
    Notifier(configured()).send("ignored")  # No loop, no settings: silently nothing.
    with pytest.raises(ValidationError):
        configured(notify_webhook_url="http://hooks.example/plain")
    with pytest.raises(ValidationError):
        configured(notify_telegram_chat_id="not a chat")
    assert "secret" not in repr(configured(notify_webhook_url="https://x.example/secret"))


def test_fill_text_only_for_fills():
    order = {
        "status": "filled",
        "side": "sell",
        "asset_id": "solana:A",
        "notional": "61.2",
        "fee": "0.95",
        "realized_pnl": "-1.1",
    }
    assert fill_text(order) == "매도 filled solana:A 61.2 USD, 수수료 0.95, 손익 -1.1"
    assert fill_text(order | {"status": "rejected"}) is None


async def test_supervisor_halt_sends_one_message(db, settings, monkeypatch):
    from trading_team.engine import Desk

    desk = Desk(settings, db)
    messages = []
    monkeypatch.setattr(desk.notifier, "send", messages.append)
    for _ in range(4):  # Two restarts within an hour halt new entries.
        desk.supervisor.observe("VET", False, "LLM_TIMEOUT")
    await desk.close()
    assert len(messages) == 1 and "VET" in messages[0]
