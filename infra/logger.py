"""Structured logging via structlog — JSON to file + human-readable to console."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from collector.config import LoggingConfig


def setup_logging(cfg: LoggingConfig) -> structlog.stdlib.BoundLogger:
    """Initialise structlog with JSON file output and coloured console output."""

    log_dir = Path(cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "collector.log"

    # ── stdlib root handler: JSON to file ───────────────────────────
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=cfg.rotation_mb * 1024 * 1024,
        backupCount=max(1, cfg.retention_days),
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, cfg.level.upper(), logging.INFO))

    # ── stdlib root handler: human-readable to stderr ───────────────
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(getattr(logging, cfg.level.upper(), logging.INFO))

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, cfg.level.upper(), logging.INFO),
        handlers=[file_handler, console_handler],
        force=True,
    )

    # ── structlog processors ────────────────────────────────────────
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # File formatter: JSON
    json_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    file_handler.setFormatter(json_formatter)

    # Console formatter: coloured key-value
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        foreign_pre_chain=shared_processors,
    )
    console_handler.setFormatter(console_formatter)

    return structlog.get_logger("liquitrack")
