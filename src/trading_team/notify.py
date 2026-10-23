"""Optional outbound messages: paper fills, desk halts, the intraday 10% alert, daily reports.

Delivery is best effort. A failed or slow message never delays or changes a trading decision.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit

import httpx

from trading_team.settings import Settings


class Notifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.webhook = settings.notify_webhook_url.get_secret_value()
        self.telegram_token = settings.notify_telegram_bot_token.get_secret_value()
        self.telegram_chat = settings.notify_telegram_chat_id
        self.client = client
        self.pending: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook or (self.telegram_token and self.telegram_chat))

    def send(self, text: str):
        """Queue a message without waiting for it."""
        if not self.enabled:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.deliver("[PAPER] " + text))
        except RuntimeError:
            return  # No event loop (synchronous caller): skip rather than block.
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def deliver(self, text: str):
        client = self.client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        try:
            if self.webhook:
                host = urlsplit(self.webhook).hostname or ""
                body = {"content": text} if "discord" in host else {"text": text}
                await client.post(self.webhook, json=body)
            if self.telegram_token and self.telegram_chat:
                await client.post(
                    f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                    json={"chat_id": self.telegram_chat, "text": text},
                )
        except Exception as exc:  # noqa: BLE001 - messages must never break trading.
            # Never print the URL or token; the exception type is enough to diagnose.
            print(f"notification failed: {type(exc).__name__}", file=sys.stderr)
        finally:
            if self.client is None:
                await client.aclose()

    async def drain(self):
        if self.pending:
            await asyncio.gather(*self.pending, return_exceptions=True)


def fill_text(order: dict) -> str | None:
    if order.get("status") not in {"filled", "partial"}:
        return None
    side = "매수" if order["side"] == "buy" else "매도"
    return (
        f"{side} {order['status']} {order['asset_id']} "
        f"{order['notional']} USD, 수수료 {order['fee']}, 손익 {order['realized_pnl']}"
    )
