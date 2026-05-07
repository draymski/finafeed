"""REST poller for Binance Top Trader Long/Short Position Ratio."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp
import structlog

from finafeed.infra import metrics as m

if TYPE_CHECKING:
    from finafeed.config import AppConfig
    from finafeed.infra.alerter import Alerter
    from finafeed.storage import Storage

log = structlog.get_logger("long_short_ratio")

_API_URL = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"


async def collect_long_short_ratio(
    symbol: str,
    storage: Storage,
    alerter: Alerter,
    config: AppConfig,
    shutdown_event: asyncio.Event,
) -> None:
    """Poll Top Trader Long/Short Ratio every ``interval_min`` minutes.

    Each poll fetches ``limit`` rows with ``period`` granularity.
    Every execution is logged in detail per user requirement.
    """
    cfg_lsr = config.collectors.long_short_ratio
    interval_sec = cfg_lsr.interval_min * 60
    consecutive_failures = 0

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        while not shutdown_event.is_set():
            t0 = time.monotonic()
            try:
                m.lsr_polls_total.labels(symbol=symbol).inc()

                params = {
                    "symbol": symbol,
                    "period": cfg_lsr.period,
                    "limit": cfg_lsr.limit,
                }

                async with session.get(_API_URL, params=params) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error(
                            "lsr_http_error",
                            symbol=symbol,
                            status=resp.status,
                            body=body[:200],
                        )
                        m.errors_total.labels(type="lsr_http", symbol=symbol).inc()
                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            await alerter.fire(
                                "rest_fail_3_consecutive",
                                f"Long/Short Ratio API failed {consecutive_failures}x for {symbol}: HTTP {resp.status}",
                                symbol=symbol,
                            )
                        await _sleep_or_shutdown(60, shutdown_event)  # retry after 1 min
                        continue

                    data = await resp.json()

                consecutive_failures = 0
                await alerter.resolve(
                    "rest_fail_3_consecutive",
                    f"Long/Short Ratio API recovered for {symbol}",
                    symbol=symbol,
                )

                if not isinstance(data, list) or len(data) == 0:
                    log.warning("lsr_empty_response", symbol=symbol)
                    await _sleep_or_shutdown(interval_sec, shutdown_event)
                    continue

                collected_at = int(time.time() * 1000)
                rows = []
                for item in data:
                    rows.append({
                        "symbol": symbol,
                        "long_short_ratio": float(item.get("longShortRatio", 0)),
                        "long_account": float(item.get("longAccount", 0)),
                        "short_account": float(item.get("shortAccount", 0)),
                        "data_timestamp": int(item.get("timestamp", 0)),
                        "collected_at": collected_at,
                    })

                inserted = await storage.write_long_short_ratio(rows)
                elapsed_ms = (time.monotonic() - t0) * 1000

                # Detailed log per user requirement
                first_ts = _format_ts(rows[0]["data_timestamp"]) if rows else "N/A"
                last_ts = _format_ts(rows[-1]["data_timestamp"]) if rows else "N/A"
                latest_ratio = rows[-1]["long_short_ratio"] if rows else 0

                m.lsr_rows_inserted.labels(symbol=symbol).inc(inserted or len(rows))
                m.last_activity.labels(type="long_short_ratio", symbol=symbol).set_to_current_time()

                log.info(
                    "LONG_SHORT_RATIO",
                    symbol=symbol,
                    rows=len(rows),
                    inserted=inserted,
                    first_ts=first_ts,
                    last_ts=last_ts,
                    latest_ratio=latest_ratio,
                    elapsed_ms=round(elapsed_ms, 1),
                )

            except asyncio.CancelledError:
                log.info("lsr_cancelled", symbol=symbol)
                break
            except Exception as exc:
                log.error("lsr_unexpected_error", symbol=symbol, error=str(exc), exc_info=True)
                m.errors_total.labels(type="lsr_error", symbol=symbol).inc()
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    await alerter.fire(
                        "rest_fail_3_consecutive",
                        f"Long/Short Ratio collector error {consecutive_failures}x for {symbol}: {exc}",
                        symbol=symbol,
                    )

            await _sleep_or_shutdown(interval_sec, shutdown_event)


def _format_ts(ms_timestamp: int) -> str:
    """Convert millisecond timestamp to ISO 8601 string."""
    try:
        dt = datetime.fromtimestamp(ms_timestamp / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, OSError):
        return str(ms_timestamp)


async def _sleep_or_shutdown(seconds: float, shutdown_event: asyncio.Event) -> None:
    """Sleep for ``seconds`` but wake up immediately on shutdown."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
