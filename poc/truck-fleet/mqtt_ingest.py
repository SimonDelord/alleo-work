#!/usr/bin/env python3
"""
Subscribes to fleet/trucks/+/telemetry and writes rows to PostgreSQL.
Maintains truck_telemetry (history) and truck_state (latest snapshot).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import paho.mqtt.client as mqtt
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("mqtt_ingest")

MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC_SUBSCRIBE", "fleet/trucks/+/telemetry")
PGHOST = os.environ.get("PGHOST", "postgresql")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PGDATABASE = os.environ.get("PGDATABASE", "truckfleet")
PGUSER = os.environ.get("PGUSER", "truckfleet")
PGPASSWORD = os.environ.get("PGPASSWORD", "changeme")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS truck_telemetry (
    id BIGSERIAL PRIMARY KEY,
    truck_id VARCHAR(32) NOT NULL,
    state VARCHAR(32) NOT NULL,
    load_pct REAL NOT NULL,
    speed_kmh REAL NOT NULL,
    position_x REAL NOT NULL,
    position_y REAL NOT NULL,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    destination_crusher VARCHAR(32),
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_truck_telemetry_truck_recorded
    ON truck_telemetry (truck_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS truck_state (
    truck_id VARCHAR(32) PRIMARY KEY,
    state VARCHAR(32) NOT NULL,
    load_pct REAL NOT NULL,
    speed_kmh REAL NOT NULL,
    position_x REAL NOT NULL,
    position_y REAL NOT NULL,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    destination_crusher VARCHAR(32),
    recorded_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

INSERT_TELEMETRY = """
INSERT INTO truck_telemetry (
    truck_id, state, load_pct, speed_kmh, position_x, position_y,
    lat, lon, destination_crusher, recorded_at
) VALUES (
    %(truck_id)s, %(state)s, %(load_pct)s, %(speed_kmh)s,
    %(position_x)s, %(position_y)s, %(lat)s, %(lon)s,
    %(destination_crusher)s, %(recorded_at)s
);
"""

UPSERT_STATE = """
INSERT INTO truck_state (
    truck_id, state, load_pct, speed_kmh, position_x, position_y,
    lat, lon, destination_crusher, recorded_at, updated_at
) VALUES (
    %(truck_id)s, %(state)s, %(load_pct)s, %(speed_kmh)s,
    %(position_x)s, %(position_y)s, %(lat)s, %(lon)s,
    %(destination_crusher)s, %(recorded_at)s, NOW()
)
ON CONFLICT (truck_id) DO UPDATE SET
    state = EXCLUDED.state,
    load_pct = EXCLUDED.load_pct,
    speed_kmh = EXCLUDED.speed_kmh,
    position_x = EXCLUDED.position_x,
    position_y = EXCLUDED.position_y,
    lat = EXCLUDED.lat,
    lon = EXCLUDED.lon,
    destination_crusher = EXCLUDED.destination_crusher,
    recorded_at = EXCLUDED.recorded_at,
    updated_at = NOW();
"""


def _parse_timestamp(raw: Optional[str]) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _row_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    truck_id = str(payload.get("truck_id", "unknown"))
    return {
        "truck_id": truck_id,
        "state": str(payload.get("state", "unknown")),
        "load_pct": float(payload.get("load_pct", 0)),
        "speed_kmh": float(payload.get("speed_kmh", 0)),
        "position_x": float(payload.get("position_x", 0)),
        "position_y": float(payload.get("position_y", 0)),
        "lat": payload.get("lat"),
        "lon": payload.get("lon"),
        "destination_crusher": payload.get("destination_crusher"),
        "recorded_at": _parse_timestamp(payload.get("timestamp")),
    }


class IngestService:
    def __init__(self) -> None:
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

    def handle_message(self, payload: dict[str, Any]) -> None:
        row = _row_from_payload(payload)
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(INSERT_TELEMETRY, row)
            cur.execute(UPSERT_STATE, row)
        LOG.info(
            "Ingested %s state=%s load=%.0f%%",
            row["truck_id"],
            row["state"],
            row["load_pct"],
        )

    def stop(self) -> None:
        self._running = False
        if self._conn is not None:
            self._conn.close()


def main() -> None:
    service = IngestService()

    def _stop(*_: object) -> None:
        service.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    service.connect_db()

    def on_connect(client: mqtt.Client, userdata: object, flags: dict, reason_code: int, properties: object) -> None:
        del userdata, flags, properties
        if reason_code == 0:
            client.subscribe(MQTT_TOPIC, qos=0)
            LOG.info("Subscribed to %s", MQTT_TOPIC)
        else:
            LOG.error("MQTT connect failed with code %s", reason_code)

    def on_message(
        client: mqtt.Client,
        userdata: object,
        msg: mqtt.MQTTMessage,
    ) -> None:
        del client, userdata
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                LOG.warning("Ignoring non-object payload on %s", msg.topic)
                return
            service.handle_message(payload)
        except json.JSONDecodeError:
            LOG.warning("Invalid JSON on topic %s", msg.topic)
        except psycopg2.Error as exc:
            LOG.error("Database error: %s", exc)
            service.connect_db()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mqtt-ingest")
    client.on_connect = on_connect
    client.on_message = on_message

    LOG.info("Connecting to MQTT %s:%s", MQTT_HOST, MQTT_PORT)
    while service._running:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            break
        except OSError as exc:
            LOG.warning("MQTT connect failed (%s), retrying in 5s", exc)
            time.sleep(5)

    client.loop_start()
    try:
        while service._running:
            time.sleep(1)
    finally:
        client.loop_stop()
        client.disconnect()
        service.stop()

    LOG.info("Stop requested, exiting")
    sys.exit(0)


if __name__ == "__main__":
    main()
