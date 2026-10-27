"""Optional Telegram ping when a proposal is waiting for approval."""

from __future__ import annotations

import json
import urllib.request

from trading_god.config import TelegramSettings


def telegram_notifier(settings: TelegramSettings):
    """Return a callable that sends one message, or ``None`` when Telegram is not set up."""
    if not (settings.bot_token.strip() and settings.chat_id.strip()):
        return None
    url = f"https://api.telegram.org/bot{settings.bot_token.strip()}/sendMessage"

    def send(text: str) -> None:
        body = json.dumps({"chat_id": settings.chat_id.strip(), "text": text[:4000]}).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed https host
            response.read()

    return send
