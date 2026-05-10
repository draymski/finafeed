"""Telegram bot command listener — polls getUpdates for interactive commands.

Currently supports:
    /stat  — Report per-symbol row count deltas and database size.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

_PROCESS_START_TIME = time.time()

import aiohttp
import structlog

if TYPE_CHECKING:
    from finafeed.config import AlertConfig
    from finafeed.storage import Storage

log = structlog.get_logger("telegram_bot")

_POLL_INTERVAL_SEC = 2  # How often to call getUpdates


def _fmt_size(n: int) -> str:
    """Format bytes into a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _fmt_delta(current: dict[str, dict[str, int]],
               previous: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Compute per-table per-symbol delta between two snapshots."""
    delta: dict[str, dict[str, int]] = {}
    for table in current:
        delta[table] = {}
        for symbol, count in current[table].items():
            prev = previous.get(table, {}).get(symbol, 0)
            delta[table][symbol] = count - prev
    return delta


async def run_telegram_bot(
    cfg: AlertConfig,
    storage: Storage,
    shutdown_event: asyncio.Event,
) -> None:
    """Long-running task: poll Telegram for /stat commands and reply with stats."""

    if not cfg.enabled or not cfg.telegram.bot_token or not cfg.telegram.chat_id:
        log.info("telegram_bot_disabled", reason="alert not enabled or missing credentials")
        return

    bot_token = cfg.telegram.bot_token
    chat_id = cfg.telegram.chat_id
    base_url = f"https://api.telegram.org/bot{bot_token}"

    # Track the last update_id we processed (to avoid re-processing)
    last_update_id = 0

    # Row count snapshot: taken at startup (or last /stat)
    prev_snapshot: dict[str, dict[str, int]] = await storage.get_row_counts()
    snapshot_time = time.time()

    log.info("telegram_bot_started")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        # Skip any messages that arrived before we started
        try:
            async with session.get(
                f"{base_url}/getUpdates", params={"timeout": 0}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("result", [])
                    if results:
                        last_update_id = results[-1]["update_id"]
                        log.debug("telegram_bot_skipped_old", count=len(results))
        except Exception as exc:
            log.warning("telegram_bot_init_error", error=str(exc))

        while not shutdown_event.is_set():
            try:
                # Long-poll with a short timeout so we can check shutdown
                params: dict[str, Any] = {
                    "timeout": 5,
                    "offset": last_update_id + 1,
                }
                async with session.get(
                    f"{base_url}/getUpdates",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        await _sleep_or_shutdown(_POLL_INTERVAL_SEC, shutdown_event)
                        continue
                    data = await resp.json()

                for update in data.get("result", []):
                    last_update_id = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip()
                    msg_chat_id = str(msg.get("chat", {}).get("id", ""))

                    # Only respond to our configured chat
                    if msg_chat_id != chat_id:
                        continue

                    if text.lower() == "/stat":
                        await _handle_stat(
                            session, base_url, chat_id,
                            storage, prev_snapshot, snapshot_time,
                        )
                        # Update snapshot after reporting
                        prev_snapshot = await storage.get_row_counts()
                        snapshot_time = time.time()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("telegram_bot_error", error=str(exc))
                await _sleep_or_shutdown(_POLL_INTERVAL_SEC, shutdown_event)

    log.info("telegram_bot_stopped")


async def _handle_stat(
    session: aiohttp.ClientSession,
    base_url: str,
    chat_id: str,
    storage: Storage,
    prev_snapshot: dict[str, dict[str, int]],
    snapshot_time: float,
) -> None:
    """Query stats and send a formatted reply."""
    current = await storage.get_row_counts()
    delta = _fmt_delta(current, prev_snapshot)
    db_size = storage.get_db_size_bytes()

    tz_8 = timezone(timedelta(hours=8))
    now = datetime.now(tz_8)
    prev_dt = datetime.fromtimestamp(snapshot_time, tz=tz_8)
    start_dt = datetime.fromtimestamp(_PROCESS_START_TIME, tz=tz_8)
    
    elapsed_sec = (now - prev_dt).total_seconds()
    if elapsed_sec < 60:
        elapsed_str = f"{elapsed_sec:.0f} secs"
    elif elapsed_sec < 3600:
        elapsed_str = f"{elapsed_sec / 60:.1f} mins"
    elif elapsed_sec < 86400:
        elapsed_str = f"{elapsed_sec / 3600:.1f} hours"
    else:
        elapsed_str = f"{elapsed_sec / 86400:.1f} days"

    # Build message
    lines = [
        f"📊 *Finafeed Stats*",
        f"Started: `{start_dt:%Y-%m-%d %H:%M:%S}`",
        f"Since: `{prev_dt:%Y-%m-%d %H:%M:%S}` ({elapsed_str} ago)",
        "",
    ]

    table_labels = {
        "liquidations": "💥 Liquidations",
        "open_interest": "📈 Open Interest",
        "long_short_ratio": "⚖️ Long/Short Ratio",
    }

    for table, label in table_labels.items():
        lines.append(f"*{label}*")
        symbols_in_table = sorted(
            set(list(current.get(table, {}).keys()) + list(prev_snapshot.get(table, {}).keys()))
        )
        if not symbols_in_table:
            lines.append("  (no data)")
        for sym in symbols_in_table:
            total = current.get(table, {}).get(sym, 0)
            d = delta.get(table, {}).get(sym, 0)
            sign = "+" if d >= 0 else ""
            lines.append(f"  `{sym}`: {total} ({sign}{d})")
        lines.append("")

    lines.append(f"💾 DB size: `{_fmt_size(db_size)}`")

    text = "\n".join(lines)

    # Send reply
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        async with session.post(f"{base_url}/sendMessage", json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error("telegram_stat_send_failed", status=resp.status, body=body[:200])
            else:
                log.info("telegram_stat_sent")
    except Exception as exc:
        log.error("telegram_stat_send_error", error=str(exc))


async def _sleep_or_shutdown(seconds: float, shutdown_event: asyncio.Event) -> None:
    """Sleep but wake on shutdown."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
