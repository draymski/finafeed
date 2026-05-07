"""WebSocket liquidation stream collector with auto-reconnect and buffered writes."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Dict, List

import aiohttp
import structlog

from collector.infra import metrics as m

if TYPE_CHECKING:
    from collector.config import AppConfig
    from collector.infra.alerter import Alerter
    from collector.storage import Storage

log = structlog.get_logger("ws_liquidation")


async def collect_liquidations(
    symbol: str,
    storage: Storage,
    alerter: Alerter,
    config: AppConfig,
    shutdown_event: asyncio.Event,
) -> None:
    """Long-running coroutine: subscribe to <symbol>@forceOrder and persist every event.

    Reconnects indefinitely with exponential backoff on disconnection.
    Buffers messages for ``buffer_ms`` before flushing to DB.
    """
    cfg_liq = config.collectors.liquidation
    cfg_rc = config.reconnect

    stream = f"{symbol.lower()}@forceOrder"
    ws_url = f"wss://fstream.binance.com/market/stream?streams={stream}"
    delay = cfg_rc.initial_delay_sec

    while not shutdown_event.is_set():
        session: aiohttp.ClientSession | None = None
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            session = aiohttp.ClientSession()
            log.info("ws_connecting", symbol=symbol, url=ws_url)
            ws = await session.ws_connect(
                ws_url,
                heartbeat=20,          # send pong automatically
                autoping=True,
                timeout=30,
            )
            m.ws_connected.labels(symbol=symbol).set(1)
            log.info("ws_connected", symbol=symbol)
            delay = cfg_rc.initial_delay_sec  # reset backoff

            # Resolve any previous disconnect alert
            await alerter.resolve(
                "ws_disconnect_5min",
                f"WebSocket reconnected for {symbol}",
                symbol=symbol,
            )

            # ── Message loop with buffered writes ───────────────────
            buffer: List[Dict[str, Any]] = []
            last_flush = time.monotonic()
            last_msg_time = time.monotonic()
            buffer_sec = cfg_liq.buffer_ms / 1000.0

            while not shutdown_event.is_set():
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                except asyncio.TimeoutError:
                    # No message within 5s — check if we should flush or alert
                    elapsed_no_msg = time.monotonic() - last_msg_time
                    if elapsed_no_msg > 300:  # 5 minutes without any WS message
                        await alerter.fire(
                            "ws_disconnect_5min",
                            f"No WS messages for {elapsed_no_msg:.0f}s on {symbol}",
                            symbol=symbol,
                        )
                    # Flush any pending buffer
                    if buffer:
                        await _flush(buffer, storage, symbol)
                        buffer.clear()
                        last_flush = time.monotonic()
                    continue

                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    log.warning("ws_closed_by_server", symbol=symbol)
                    break
                if msg.type == aiohttp.WSMsgType.ERROR:
                    log.error("ws_error", symbol=symbol, error=str(ws.exception()))
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                last_msg_time = time.monotonic()
                data = msg.json()
                payload = data.get("data", data)

                if payload.get("e") != "forceOrder":
                    continue

                o = payload["o"]
                row = {
                    "symbol": symbol,
                    "event_time": payload["E"],
                    "trade_time": o["T"],
                    "side": o["S"],
                    "price": float(o["p"]),
                    "avg_price": float(o.get("ap", o["p"])),
                    "qty": float(o["q"]),
                    "collected_at": int(time.time() * 1000),
                }
                buffer.append(row)
                m.liq_messages_total.labels(symbol=symbol).inc()
                m.last_activity.labels(type="liquidation", symbol=symbol).set_to_current_time()

                # Flush buffer periodically
                now = time.monotonic()
                if now - last_flush >= buffer_sec:
                    await _flush(buffer, storage, symbol)
                    buffer.clear()
                    last_flush = now

        except asyncio.CancelledError:
            log.info("ws_cancelled", symbol=symbol)
            # Flush remaining
            if buffer:
                try:
                    await _flush(buffer, storage, symbol)
                except Exception:
                    pass
            break
        except Exception as exc:
            log.error("ws_unexpected_error", symbol=symbol, error=str(exc), exc_info=True)
            m.errors_total.labels(type="ws_error", symbol=symbol).inc()
        finally:
            m.ws_connected.labels(symbol=symbol).set(0)
            if ws and not ws.closed:
                await ws.close()
            if session and not session.closed:
                await session.close()

        # ── Reconnect with backoff ──────────────────────────────────
        if shutdown_event.is_set():
            break
        m.ws_reconnects_total.labels(symbol=symbol).inc()
        log.info("ws_reconnecting", symbol=symbol, delay=delay)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            break  # shutdown requested during wait
        except asyncio.TimeoutError:
            pass
        delay = min(delay * cfg_rc.backoff_factor, cfg_rc.max_delay_sec)


async def _flush(buffer: List[Dict[str, Any]], storage: Storage, symbol: str) -> None:
    """Write buffered rows to database."""
    try:
        await storage.write_liquidations(buffer)
        log.debug("ws_flushed", symbol=symbol, count=len(buffer))
    except Exception as exc:
        log.error("ws_flush_error", symbol=symbol, count=len(buffer), error=str(exc))
