"""Prometheus metrics definitions — counters, gauges, histograms."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server

from finafeed.config import MetricsConfig


# ── Liquidation WS ──────────────────────────────────────────────────
liq_messages_total = Counter(
    "finafeed_liq_messages_total",
    "Total liquidation messages received",
    ["symbol"],
)

ws_reconnects_total = Counter(
    "finafeed_ws_reconnects_total",
    "Total WebSocket reconnection attempts",
    ["symbol"],
)

ws_connected = Gauge(
    "finafeed_ws_connected",
    "WebSocket connection status (1=connected, 0=disconnected)",
    ["symbol"],
)

# ── Open Interest ───────────────────────────────────────────────────
oi_polls_total = Counter(
    "finafeed_oi_polls_total",
    "Total open interest poll attempts",
    ["symbol"],
)

oi_writes_total = Counter(
    "finafeed_oi_writes_total",
    "Total open interest writes (value changed)",
    ["symbol"],
)

oi_skips_total = Counter(
    "finafeed_oi_skips_total",
    "Total open interest skips (value unchanged)",
    ["symbol"],
)

# ── Long/Short Ratio ───────────────────────────────────────────────
lsr_polls_total = Counter(
    "finafeed_lsr_polls_total",
    "Total long/short ratio poll attempts",
    ["symbol"],
)

lsr_rows_inserted = Counter(
    "finafeed_lsr_rows_inserted",
    "Total long/short ratio rows inserted",
    ["symbol"],
)

# ── Errors ──────────────────────────────────────────────────────────
errors_total = Counter(
    "finafeed_errors_total",
    "Total errors by type and symbol",
    ["type", "symbol"],
)

# ── Database ────────────────────────────────────────────────────────
db_write_latency = Histogram(
    "finafeed_db_write_latency_seconds",
    "Database write latency in seconds",
    ["table"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

db_rotations_total = Counter(
    "finafeed_db_rotations_total",
    "Total database file rotations",
)

# ── Activity tracking ──────────────────────────────────────────────
last_activity = Gauge(
    "finafeed_last_activity_timestamp",
    "Unix timestamp of last activity",
    ["type", "symbol"],
)

# ── Build info ──────────────────────────────────────────────────────
build_info = Info(
    "finafeed_build",
    "Collector build information",
)


def start_metrics_server(cfg: MetricsConfig) -> None:
    """Start the Prometheus HTTP server if metrics are enabled."""
    if not cfg.enabled:
        return
    build_info.info({"version": "1.0.0", "app": "finafeed"})
    start_http_server(cfg.port)
