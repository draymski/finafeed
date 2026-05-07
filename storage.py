"""SQLite WAL async storage with 800 MB rotation, batch writes, and OI dedup."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence

import aiosqlite
import structlog

from collector.config import DatabaseConfig
from collector.infra import metrics as m

log = structlog.get_logger("storage")

# ── Schema ──────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS liquidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    event_time INTEGER NOT NULL,
    trade_time INTEGER NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    avg_price REAL NOT NULL,
    qty REAL NOT NULL,
    collected_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liq_symbol_time
    ON liquidations(symbol, event_time);

CREATE TABLE IF NOT EXISTS open_interest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    open_interest REAL NOT NULL,
    api_time INTEGER NOT NULL,
    collected_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oi_symbol_time
    ON open_interest(symbol, api_time);

CREATE TABLE IF NOT EXISTS long_short_ratio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    long_short_ratio REAL NOT NULL,
    long_account REAL NOT NULL,
    short_account REAL NOT NULL,
    data_timestamp INTEGER NOT NULL,
    collected_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lsr_symbol_time
    ON long_short_ratio(symbol, data_timestamp);
"""


class Storage:
    """Async SQLite storage with WAL mode and automatic file rotation."""

    def __init__(self, cfg: DatabaseConfig) -> None:
        self._cfg = cfg
        self._base_path = Path(cfg.path)
        self._max_bytes = cfg.max_size_mb * 1024 * 1024
        self._db: aiosqlite.Connection | None = None
        self._current_path: Path | None = None

    # ── Lifecycle ───────────────────────────────────────────────────

    async def open(self) -> None:
        """Open (or create) the database file and apply schema."""
        self._current_path = self._resolve_current_path()
        self._current_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._current_path))

        if self._cfg.wal_mode:
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")

        # Reduce memory footprint
        await self._db.execute("PRAGMA cache_size=-8000")  # ~8 MB
        await self._db.execute("PRAGMA temp_store=MEMORY")

        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()
        log.info("database_opened", path=str(self._current_path))

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.commit()
            await self._db.close()
            log.info("database_closed", path=str(self._current_path))

    # ── Rotation ────────────────────────────────────────────────────

    def _resolve_current_path(self) -> Path:
        """Find the latest DB file or create a new one.

        Naming convention:  liquitrack.db, liquitrack_001.db, liquitrack_002.db, ...
        """
        base = self._base_path
        if not base.exists():
            return base

        # If current file is under the limit, reuse it
        if base.stat().st_size < self._max_bytes:
            return base

        # Find next rotation index
        parent = base.parent
        stem = base.stem
        suffix = base.suffix
        idx = 1
        while True:
            candidate = parent / f"{stem}_{idx:03d}{suffix}"
            if not candidate.exists() or candidate.stat().st_size < self._max_bytes:
                return candidate
            idx += 1

    async def _maybe_rotate(self) -> None:
        """Check file size and rotate if needed."""
        if self._current_path is None:
            return
        try:
            size = self._current_path.stat().st_size
        except OSError:
            return

        if size >= self._max_bytes:
            log.warning("database_rotating", current=str(self._current_path), size_mb=size // (1024 * 1024))
            m.db_rotations_total.inc()
            await self.close()
            await self.open()

    # ── Writers ──────────────────────────────────────────────────────

    async def write_liquidations(self, rows: List[Dict[str, Any]]) -> None:
        """Batch-insert liquidation records."""
        if not rows or not self._db:
            return
        t0 = time.monotonic()
        sql = (
            "INSERT INTO liquidations "
            "(symbol, event_time, trade_time, side, price, avg_price, qty, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = [
            (
                r["symbol"],
                r["event_time"],
                r["trade_time"],
                r["side"],
                r["price"],
                r["avg_price"],
                r["qty"],
                r["collected_at"],
            )
            for r in rows
        ]
        try:
            await self._db.executemany(sql, params)
            await self._db.commit()
            elapsed = time.monotonic() - t0
            m.db_write_latency.labels(table="liquidations").observe(elapsed)
        except Exception as exc:
            log.error("db_write_error", table="liquidations", error=str(exc))
            m.errors_total.labels(type="db_write", symbol="all").inc()
            raise

        await self._maybe_rotate()

    async def write_open_interest(self, row: Dict[str, Any]) -> None:
        """Insert a single open interest record."""
        if not self._db:
            return
        t0 = time.monotonic()
        sql = (
            "INSERT INTO open_interest "
            "(symbol, open_interest, api_time, collected_at) "
            "VALUES (?, ?, ?, ?)"
        )
        try:
            await self._db.execute(
                sql,
                (row["symbol"], row["open_interest"], row["api_time"], row["collected_at"]),
            )
            await self._db.commit()
            elapsed = time.monotonic() - t0
            m.db_write_latency.labels(table="open_interest").observe(elapsed)
        except Exception as exc:
            log.error("db_write_error", table="open_interest", error=str(exc))
            m.errors_total.labels(type="db_write", symbol=row.get("symbol", "?")).inc()
            raise

        await self._maybe_rotate()

    async def write_long_short_ratio(self, rows: List[Dict[str, Any]]) -> None:
        """Batch-insert long/short ratio records, skipping existing timestamps."""
        if not rows or not self._db:
            return
        t0 = time.monotonic()
        # Use INSERT OR IGNORE with a unique constraint workaround:
        # We check existence first to be safe with the current schema.
        sql = (
            "INSERT INTO long_short_ratio "
            "(symbol, long_short_ratio, long_account, short_account, data_timestamp, collected_at) "
            "SELECT ?, ?, ?, ?, ?, ? "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM long_short_ratio WHERE symbol = ? AND data_timestamp = ?"
            ")"
        )
        inserted = 0
        try:
            for r in rows:
                cursor = await self._db.execute(
                    sql,
                    (
                        r["symbol"],
                        r["long_short_ratio"],
                        r["long_account"],
                        r["short_account"],
                        r["data_timestamp"],
                        r["collected_at"],
                        r["symbol"],
                        r["data_timestamp"],
                    ),
                )
                if cursor.rowcount and cursor.rowcount > 0:
                    inserted += 1
            await self._db.commit()
            elapsed = time.monotonic() - t0
            m.db_write_latency.labels(table="long_short_ratio").observe(elapsed)
            return inserted
        except Exception as exc:
            log.error("db_write_error", table="long_short_ratio", error=str(exc))
            m.errors_total.labels(type="db_write", symbol=rows[0].get("symbol", "?") if rows else "?").inc()
            raise

        await self._maybe_rotate()
