"""Telegram webhook alerter with dedup and recovery notifications."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import aiohttp
import structlog

if TYPE_CHECKING:
    from finafeed.config import AlertConfig

log = structlog.get_logger("alerter")

# Dedup window: same alert key won't fire again within this many seconds.
_DEDUP_WINDOW_SEC = 600  # 10 minutes


class Alerter:
    """Asynchronous Telegram alerter with dedup and recovery support."""

    def __init__(self, cfg: AlertConfig) -> None:
        self._enabled = cfg.enabled and bool(cfg.telegram.bot_token) and bool(cfg.telegram.chat_id)
        self._bot_token = cfg.telegram.bot_token
        self._chat_id = cfg.telegram.chat_id
        self._alert_on = set(cfg.alert_on)
        self._last_fired: dict[str, float] = {}   # alert_key → unix ts
        self._active_alerts: set[str] = set()      # currently active alert keys
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def fire(self, alert_key: str, message: str, *, symbol: str = "") -> None:
        """Send an alert if enabled, not deduped, and the key is in alert_on."""
        if not self._enabled:
            return

        # Check if this alert type is configured
        if alert_key not in self._alert_on:
            return

        # Dedup: skip if we already fired this key recently
        dedup_id = f"{alert_key}:{symbol}"
        now = time.monotonic()
        if dedup_id in self._last_fired:
            elapsed = now - self._last_fired[dedup_id]
            if elapsed < _DEDUP_WINDOW_SEC:
                return

        self._last_fired[dedup_id] = now
        self._active_alerts.add(dedup_id)

        full_msg = f"🚨 *finafeed Alert*\n\n`{alert_key}`"
        if symbol:
            full_msg += f"  symbol=`{symbol}`"
        full_msg += f"\n\n{message}"

        await self._send_telegram(full_msg)
        log.warning("alert_fired", alert_key=alert_key, symbol=symbol, message=message)

    async def resolve(self, alert_key: str, message: str, *, symbol: str = "") -> None:
        """Send a recovery notification when an alert condition clears."""
        if not self._enabled:
            return

        dedup_id = f"{alert_key}:{symbol}"
        if dedup_id not in self._active_alerts:
            return

        self._active_alerts.discard(dedup_id)
        self._last_fired.pop(dedup_id, None)

        full_msg = f"✅ *finafeed Resolved*\n\n`{alert_key}`"
        if symbol:
            full_msg += f"  symbol=`{symbol}`"
        full_msg += f"\n\n{message}"

        await self._send_telegram(full_msg)
        log.info("alert_resolved", alert_key=alert_key, symbol=symbol, message=message)

    async def _send_telegram(self, text: str) -> None:
        """POST to the Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("telegram_send_failed", status=resp.status, body=body)
        except Exception as exc:
            log.error("telegram_send_error", error=str(exc))

    async def close(self) -> None:
        """Shutdown the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
