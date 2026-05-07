"""REST poller for Binance Open Interest with value-based deduplication."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import aiohttp
import structlog

from collector.infra import metrics as m

if TYPE_CHECKING:
    from collector.config import AppConfig
    from collector.infra.alerter import Alerter
    from collector.storage import Storage

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

    Dedup: if the ``openInterest`` value is identical to the previous poll,
    the write is skipped to save storage.
    """
    cfg_oi = config.collectors.open_interest
    interval = cfg_oi.interval_sec
    last_value: str | None = None
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
                api_time = data.get("time", int(time.time() * 1000))

                m.last_activity.labels(type="open_interest", symbol=symbol).set_to_current_time()

                # Dedup check
                if cfg_oi.dedup and oi_value == last_value:
                    m.oi_skips_total.labels(symbol=symbol).inc()
                    log.debug("oi_skipped", symbol=symbol, value=oi_value)
                else:
                    row = {
                        "symbol": symbol,
                        "open_interest": float(oi_value),
                        "api_time": api_time,
                        "collected_at": int(time.time() * 1000),
                    }
                    await storage.write_open_interest(row)
                    m.oi_writes_total.labels(symbol=symbol).inc()
                    log.debug("oi_written", symbol=symbol, value=oi_value)
                    last_value = oi_value

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
