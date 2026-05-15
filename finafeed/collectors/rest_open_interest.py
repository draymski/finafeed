"""REST poller for Binance Open Interest with value-based deduplication."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import aiohttp
import structlog

from finafeed.infra import metrics as m

if TYPE_CHECKING:
    from finafeed.config import AppConfig
    from finafeed.infra.alerter import Alerter
    from finafeed.storage import Storage

log = structlog.get_logger("open_interest")

_API_URL = "https://fapi.binance.com/fapi/v1/openInterest"


async def collect_open_interest(
    symbol: str,
    storage: Storage,
    alerter: Alerter,
    config: AppConfig,
    shutdown_event: asyncio.Event,
) -> None:
    """Poll open interest every ``interval_sec`` seconds.

    Downsampling: writes are aligned to 300-second boundaries. Only the first
    successful poll in a new 300s window is saved, with its api_time rewritten
    to the boundary time, ensuring clean alignment for downstream processing.
    """
    cfg_oi = config.collectors.open_interest
    interval = cfg_oi.interval_sec
    last_written_boundary = 0
    consecutive_failures = 0

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
    ) as session:
        while not shutdown_event.is_set():
            try:
                m.oi_polls_total.labels(symbol=symbol).inc()

                async with session.get(_API_URL, params={"symbol": symbol}) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error("oi_http_error", symbol=symbol, status=resp.status, body=body[:200])
                        m.errors_total.labels(type="oi_http", symbol=symbol).inc()
                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            await alerter.fire(
                                "rest_fail_3_consecutive",
                                f"Open Interest API failed {consecutive_failures} times for {symbol}: HTTP {resp.status}",
                                symbol=symbol,
                            )
                        await _sleep_or_shutdown(interval, shutdown_event)
                        continue

                    data = await resp.json()

                consecutive_failures = 0
                await alerter.resolve(
                    "rest_fail_3_consecutive",
                    f"Open Interest API recovered for {symbol}",
                    symbol=symbol,
                )

                oi_value = data.get("openInterest", "")
                
                m.last_activity.labels(type="open_interest", symbol=symbol).set_to_current_time()

                # Align to 300-second boundary
                current_time = time.time()
                current_boundary = int(current_time) // 300 * 300

                if current_boundary > last_written_boundary:
                    row = {
                        "symbol": symbol,
                        "open_interest": float(oi_value),
                        "api_time": current_boundary * 1000,
                        "collected_at": int(current_time * 1000),
                    }
                    await storage.write_open_interest(row)
                    m.oi_writes_total.labels(symbol=symbol).inc()
                    log.debug("oi_written", symbol=symbol, value=oi_value, boundary=current_boundary)
                    last_written_boundary = current_boundary
                else:
                    m.oi_skips_total.labels(symbol=symbol).inc()
                    log.debug("oi_skipped_boundary", symbol=symbol, value=oi_value)

            except asyncio.CancelledError:
                log.info("oi_cancelled", symbol=symbol)
                break
            except Exception as exc:
                log.error("oi_unexpected_error", symbol=symbol, error=str(exc), exc_info=True)
                m.errors_total.labels(type="oi_error", symbol=symbol).inc()
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    await alerter.fire(
                        "rest_fail_3_consecutive",
                        f"Open Interest collector error {consecutive_failures}x for {symbol}: {exc}",
                        symbol=symbol,
                    )

            await _sleep_or_shutdown(interval, shutdown_event)


async def _sleep_or_shutdown(seconds: float, shutdown_event: asyncio.Event) -> None:
    """Sleep for ``seconds`` but wake up immediately on shutdown."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
