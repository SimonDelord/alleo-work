#!/usr/bin/env python3
"""
Plant historian: polls crusher Modbus PLCs and writes to PostgreSQL.

Maintains crusher_telemetry (time-series history) and crusher_state (latest snapshot).
Crushers do not talk to PostgreSQL directly — only this service bridges Modbus → SQL.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
from pymodbus.client import ModbusTcpClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("historian")

# Modbus holding register indices (must match crusher_plc.py)
REG_FILL_PCT = 0
REG_AT_CAPACITY = 1
REG_STATUS = 2
REG_THROUGHPUT = 3
REG_DUMP_COUNT = 4
REG_READY = 5
REGISTER_COUNT = 6

STATUS_NAMES = {
    0: "empty",
    1: "accepting",
    2: "full",
    3: "fault",
}

PGHOST = os.environ.get("PGHOST", "postgresql")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PGDATABASE = os.environ.get("PGDATABASE", "crusherfleet")
PGUSER = os.environ.get("PGUSER", "crusherfleet")
PGPASSWORD = os.environ.get("PGPASSWORD", "changeme")
POLL_SEC = float(os.environ.get("POLL_SEC", "2.0"))

# Format: crusher_id:host:port,crusher_id:host:port
CRUSHER_TARGETS = os.environ.get(
    "CRUSHER_TARGETS",
    "crusher-1:crusher-1:502,crusher-2:crusher-2:502",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crusher_telemetry (
    id BIGSERIAL PRIMARY KEY,
    crusher_id VARCHAR(32) NOT NULL,
    fill_pct REAL NOT NULL,
    at_capacity BOOLEAN NOT NULL,
    status_code INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    throughput_tph REAL NOT NULL,
    dump_count INTEGER NOT NULL,
    ready BOOLEAN NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crusher_telemetry_crusher_recorded
    ON crusher_telemetry (crusher_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS crusher_state (
    crusher_id VARCHAR(32) PRIMARY KEY,
    fill_pct REAL NOT NULL,
    at_capacity BOOLEAN NOT NULL,
    status_code INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    throughput_tph REAL NOT NULL,
    dump_count INTEGER NOT NULL,
    ready BOOLEAN NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

INSERT_TELEMETRY = """
INSERT INTO crusher_telemetry (
    crusher_id, fill_pct, at_capacity, status_code, status,
    throughput_tph, dump_count, ready, recorded_at
) VALUES (
    %(crusher_id)s, %(fill_pct)s, %(at_capacity)s, %(status_code)s, %(status)s,
    %(throughput_tph)s, %(dump_count)s, %(ready)s, %(recorded_at)s
);
"""

UPSERT_STATE = """
INSERT INTO crusher_state (
    crusher_id, fill_pct, at_capacity, status_code, status,
    throughput_tph, dump_count, ready, recorded_at, updated_at
) VALUES (
    %(crusher_id)s, %(fill_pct)s, %(at_capacity)s, %(status_code)s, %(status)s,
    %(throughput_tph)s, %(dump_count)s, %(ready)s, %(recorded_at)s, NOW()
)
ON CONFLICT (crusher_id) DO UPDATE SET
    fill_pct = EXCLUDED.fill_pct,
    at_capacity = EXCLUDED.at_capacity,
    status_code = EXCLUDED.status_code,
    status = EXCLUDED.status,
    throughput_tph = EXCLUDED.throughput_tph,
    dump_count = EXCLUDED.dump_count,
    ready = EXCLUDED.ready,
    recorded_at = EXCLUDED.recorded_at,
    updated_at = NOW();
"""


def _parse_targets(raw: str) -> list[tuple[str, str, int]]:
    targets: list[tuple[str, str, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(":")
        if len(pieces) != 3:
            raise ValueError(f"Invalid CRUSHER_TARGETS entry: {part!r} (expected id:host:port)")
        crusher_id, host, port_str = pieces
        targets.append((crusher_id, host, int(port_str)))
    if not targets:
        raise ValueError("CRUSHER_TARGETS must list at least one crusher")
    return targets


def _read_crusher(client: ModbusTcpClient, crusher_id: str) -> Optional[dict[str, Any]]:
    result = client.read_holding_registers(0, REGISTER_COUNT, slave=0)
    if result.isError():
        LOG.warning("Modbus read error for %s: %s", crusher_id, result)
        return None

    regs = [int(v) & 0xFFFF for v in result.registers]
    status_code = regs[REG_STATUS]
    return {
        "crusher_id": crusher_id,
        "fill_pct": float(regs[REG_FILL_PCT]),
        "at_capacity": bool(regs[REG_AT_CAPACITY]),
        "status_code": status_code,
        "status": STATUS_NAMES.get(status_code, "unknown"),
        "throughput_tph": float(regs[REG_THROUGHPUT]),
        "dump_count": int(regs[REG_DUMP_COUNT]),
        "ready": bool(regs[REG_READY]),
        "recorded_at": datetime.now(timezone.utc),
    }


class HistorianService:
    def __init__(self, targets: list[tuple[str, str, int]]) -> None:
        self._targets = targets
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._running = True

    def connect_db(self) -> None:
        while self._running:
            try:
                self._conn = psycopg2.connect(
                    host=PGHOST,
                    port=PGPORT,
                    dbname=PGDATABASE,
                    user=PGUSER,
                    password=PGPASSWORD,
                )
                self._conn.autocommit = True
                with self._conn.cursor() as cur:
                    cur.execute(SCHEMA_SQL)
                LOG.info("PostgreSQL ready at %s:%s/%s", PGHOST, PGPORT, PGDATABASE)
                return
            except psycopg2.Error as exc:
                LOG.warning("PostgreSQL connect failed (%s), retrying in 5s", exc)
                time.sleep(5)

    def persist(self, row: dict[str, Any]) -> None:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(INSERT_TELEMETRY, row)
            cur.execute(UPSERT_STATE, row)
        LOG.info(
            "Recorded %s fill=%.0f%% status=%s at_capacity=%s",
            row["crusher_id"],
            row["fill_pct"],
            row["status"],
            row["at_capacity"],
        )

    def poll_once(self) -> None:
        for crusher_id, host, port in self._targets:
            client = ModbusTcpClient(host, port=port, timeout=5)
            try:
                if not client.connect():
                    LOG.warning("Modbus connect failed for %s at %s:%s", crusher_id, host, port)
                    continue
                row = _read_crusher(client, crusher_id)
                if row is not None:
                    self.persist(row)
            finally:
                try:
                    client.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self._running = False
        if self._conn is not None:
            self._conn.close()


def main() -> None:
    targets = _parse_targets(CRUSHER_TARGETS)
    service = HistorianService(targets)

    def _stop(*_: object) -> None:
        service.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    service.connect_db()
    LOG.info(
        "Historian polling %d crusher(s) every %.1fs: %s",
        len(targets),
        POLL_SEC,
        ", ".join(t[0] for t in targets),
    )

    while service._running:
        try:
            service.poll_once()
        except psycopg2.Error as exc:
            LOG.error("Database error: %s", exc)
            service.connect_db()
        time.sleep(POLL_SEC)

    LOG.info("Stop requested, exiting")
    sys.exit(0)


if __name__ == "__main__":
    main()
