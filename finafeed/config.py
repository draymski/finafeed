"""Configuration loader with dataclass validation and env-var overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml


# ── Dataclass hierarchy ─────────────────────────────────────────────

@dataclass
class DatabaseConfig:
    path: str = "./data/finafeed.db"
    wal_mode: bool = True
    max_size_mb: int = 800


@dataclass
class LiquidationConfig:
    enabled: bool = True
    buffer_ms: int = 100


@dataclass
class OpenInterestConfig:
    enabled: bool = True
    interval_sec: int = 5
    dedup: bool = True


@dataclass
class LongShortRatioConfig:
    enabled: bool = True
    period: str = "5m"
    limit: int = 500
    interval_min: int = 2500


@dataclass
class CollectorsConfig:
    liquidation: LiquidationConfig = field(default_factory=LiquidationConfig)
    open_interest: OpenInterestConfig = field(default_factory=OpenInterestConfig)
    long_short_ratio: LongShortRatioConfig = field(default_factory=LongShortRatioConfig)


@dataclass
class ReconnectConfig:
    initial_delay_sec: float = 1.0
    max_delay_sec: float = 30.0
    backoff_factor: float = 2.0


@dataclass
class MetricsConfig:
    enabled: bool = True
    port: int = 17895


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class AlertConfig:
    enabled: bool = False
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    alert_on: List[str] = field(default_factory=lambda: [
        "ws_disconnect_5min",
        "rest_fail_3_consecutive",
        "db_write_error",
    ])


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"
    dir: str = "./logs"
    rotation_mb: int = 50
    retention_days: int = 30


@dataclass
class AppConfig:
    symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    collectors: CollectorsConfig = field(default_factory=CollectorsConfig)
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


# ── Helpers ─────────────────────────────────────────────────────────

# Registry of dataclass types for resolving string annotations from
# ``from __future__ import annotations``.
_DC_REGISTRY: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        DatabaseConfig,
        LiquidationConfig,
        OpenInterestConfig,
        LongShortRatioConfig,
        CollectorsConfig,
        ReconnectConfig,
        MetricsConfig,
        TelegramConfig,
        AlertConfig,
        LoggingConfig,
        AppConfig,
    )
}


def _merge(dc_class, raw: dict | None):
    """Recursively build a dataclass from a dict, ignoring unknown keys."""
    if raw is None:
        return dc_class()
    hints = dc_class.__dataclass_fields__
    kwargs = {}
    for key, fld in hints.items():
        if key not in raw:
            continue
        val = raw[key]
        # Resolve string type annotations to actual classes
        ftype = fld.type
        if isinstance(ftype, str):
            ftype = _DC_REGISTRY.get(ftype)
        # If the field type is itself a dataclass, recurse
        if ftype is not None and hasattr(ftype, "__dataclass_fields__") and isinstance(val, dict):
            kwargs[key] = _merge(ftype, val)
        else:
            kwargs[key] = val
    return dc_class(**kwargs)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration from YAML, with env-var overrides.

    Environment variable overrides:
        FINAFEED_SYMBOLS          comma-separated symbol list
        FINAFEED_DB_PATH          database file path
        FINAFEED_LOG_LEVEL        log level (DEBUG/INFO/WARNING/ERROR)
        FINAFEED_METRICS_PORT     Prometheus port
        FINAFEED_TELEGRAM_TOKEN   Telegram bot token
        FINAFEED_TELEGRAM_CHAT    Telegram chat ID
    """
    if path is None:
        path = Path(__file__).parent / "config.yaml"
    path = Path(path)

    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {}

    cfg: AppConfig = _merge(AppConfig, raw)

    # ── Env-var overrides ───────────────────────────────────────────
    if env_sym := os.environ.get("FINAFEED_SYMBOLS"):
        cfg.symbols = [s.strip().upper() for s in env_sym.split(",") if s.strip()]

    if env_db := os.environ.get("FINAFEED_DB_PATH"):
        cfg.database.path = env_db

    if env_log := os.environ.get("FINAFEED_LOG_LEVEL"):
        cfg.logging.level = env_log.upper()

    if env_port := os.environ.get("FINAFEED_METRICS_PORT"):
        cfg.metrics.port = int(env_port)

    if env_tg_token := os.environ.get("FINAFEED_TELEGRAM_TOKEN"):
        cfg.alert.telegram.bot_token = env_tg_token
        cfg.alert.enabled = True

    if env_tg_chat := os.environ.get("FINAFEED_TELEGRAM_CHAT"):
        cfg.alert.telegram.chat_id = env_tg_chat

    return cfg
