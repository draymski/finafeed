"""Finafeed Collector — 7×24 Binance Futures Data Collection Daemon.

Entry point: orchestrates lifecycle, signal handling, and multi-symbol task fan-out.

Usage:
    python -m finafeed.main                  # normal run
    python -m finafeed.main --dry-run        # config check + DB schema only
    python -m finafeed.main --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

import structlog

from finafeed.config import load_config
from finafeed.storage import Storage
from finafeed.infra.logger import setup_logging
from finafeed.infra.metrics import start_metrics_server
from finafeed.infra.alerter import Alerter
from finafeed.collectors.ws_liquidation import collect_liquidations
from finafeed.collectors.rest_open_interest import collect_open_interest
from finafeed.collectors.rest_long_short import collect_long_short_ratio
from finafeed.infra.telegram_bot import run_telegram_bot


async def run(config_path: str | None = None, dry_run: bool = False) -> None:
    """Main async entry: load config → init infra → fan-out collectors."""

    # ── Config ──────────────────────────────────────────────────────
    cfg = load_config(config_path)

    # ── Logging ─────────────────────────────────────────────────────
    log = setup_logging(cfg.logging)
    log.info(
        "finafeed_starting",
        symbols=cfg.symbols,
        dry_run=dry_run,
        db_path=cfg.database.path,
        metrics_port=cfg.metrics.port if cfg.metrics.enabled else "disabled",
    )

    # ── Storage ─────────────────────────────────────────────────────
    storage = Storage(cfg.database)
    await storage.open()

    if dry_run:
        log.info("dry_run_complete", message="Config valid, DB schema applied. Exiting.")
        await storage.close()
        return

    # ── Metrics ─────────────────────────────────────────────────────
    start_metrics_server(cfg.metrics)
    log.info("metrics_server_started", port=cfg.metrics.port)

    # ── Alerter ─────────────────────────────────────────────────────
    alerter = Alerter(cfg.alert)

    # ── Shutdown event ──────────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        log.warning("signal_received", signal=sig.name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler; fall back
            pass

    # ── Fan-out tasks ───────────────────────────────────────────────
    tasks: list[asyncio.Task] = []

    # Telegram bot listener (for /stat etc.)
    tasks.append(
        asyncio.create_task(
            run_telegram_bot(cfg.alert, storage, shutdown_event),
            name="telegram-bot",
        )
    )

    for symbol in cfg.symbols:
        symbol = symbol.upper()

        if cfg.collectors.liquidation.enabled:
            tasks.append(
                asyncio.create_task(
                    collect_liquidations(symbol, storage, alerter, cfg, shutdown_event),
                    name=f"liq-{symbol}",
                )
            )

        if cfg.collectors.open_interest.enabled:
            tasks.append(
                asyncio.create_task(
                    collect_open_interest(symbol, storage, alerter, cfg, shutdown_event),
                    name=f"oi-{symbol}",
                )
            )

        if cfg.collectors.long_short_ratio.enabled:
            tasks.append(
                asyncio.create_task(
                    collect_long_short_ratio(symbol, storage, alerter, cfg, shutdown_event),
                    name=f"lsr-{symbol}",
                )
            )

    log.info("tasks_started", count=len(tasks), tasks=[t.get_name() for t in tasks])

    # Send startup notification
    await alerter.fire(
        "ws_disconnect_30min",  # reuse to ensure it's in alert_on; it will be overridden
        f"🟢 Finafeed started with {len(cfg.symbols)} symbols: {', '.join(cfg.symbols)}",
    )

    # ── Wait for all tasks or shutdown ──────────────────────────────
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        # If a task crashed without shutdown being set, log and set shutdown
        for t in done:
            if t.exception() and not shutdown_event.is_set():
                log.error("task_crashed", task=t.get_name(), error=str(t.exception()))
                shutdown_event.set()
    except asyncio.CancelledError:
        shutdown_event.set()

    # ── Graceful shutdown ───────────────────────────────────────────
    log.info("shutting_down")
    shutdown_event.set()

    # Cancel remaining tasks and wait for them to finish
    for t in tasks:
        if not t.done():
            t.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    # Close resources
    await storage.close()
    await alerter.close()
    log.info("finafeed_stopped")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Finafeed Binance Data Collector")
    parser.add_argument("--config", "-c", type=str, default=None, help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and exit")
    args = parser.parse_args()

    try:
        asyncio.run(run(config_path=args.config, dry_run=args.dry_run))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
