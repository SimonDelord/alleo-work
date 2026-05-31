#!/usr/bin/env python3
"""
Phase 1 demo: publishes mock crusher availability to fleet.crushers.state.

Replaced in Phase 2 by crusher-fleet plant-collector → Kafka.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

import yaml
from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("crusher_state_producer")

BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    os.environ.get("KAFKA_BOOTSTRAP", "my-cluster-kafka-bootstrap.kafka-demo.svc:9092"),
)
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC_CRUSHER_STATE", "fleet.crushers.state")
PUBLISH_INTERVAL_SEC = float(os.environ.get("PUBLISH_INTERVAL_SEC", "10"))
CRUSHER_STATE_FILE = os.environ.get("CRUSHER_STATE_FILE", "/config/crushers.yaml")
CAPACITY_FILL_PCT = float(os.environ.get("CAPACITY_FILL_PCT", "90"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_crushers() -> dict[str, dict[str, Any]]:
    return {
        "crusher-1": {
            "crusher_name": "crusher-1",
            "status": "accepting",
            "fill_pct": 45.0,
            "at_capacity": False,
            "max_queue": 3,
            "current_queue": 1,
        },
        "crusher-2": {
            "crusher_name": "crusher-2",
            "status": "accepting",
            "fill_pct": 30.0,
            "at_capacity": False,
            "max_queue": 3,
            "current_queue": 0,
        },
    }


def _load_crushers() -> dict[str, dict[str, Any]]:
    if not os.path.isfile(CRUSHER_STATE_FILE):
        LOG.warning("Config file %s not found, using defaults", CRUSHER_STATE_FILE)
        return _default_crushers()

    with open(CRUSHER_STATE_FILE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    crushers_raw = data.get("crushers", data)
    if not isinstance(crushers_raw, dict):
        raise ValueError("crushers config must be a mapping")

    crushers: dict[str, dict[str, Any]] = {}
    for name, state in crushers_raw.items():
        if not isinstance(state, dict):
            continue
        entry = dict(state)
        entry["crusher_name"] = str(name)
        fill_pct = float(entry.get("fill_pct", 0))
        status = str(entry.get("status", "accepting"))
        at_capacity = bool(entry.get("at_capacity", False))
        if not at_capacity and (fill_pct >= CAPACITY_FILL_PCT or status == "full"):
            at_capacity = True
        entry["at_capacity"] = at_capacity
        crushers[str(name)] = entry
    return crushers


def _create_producer() -> KafkaProducer:
    while True:
        try:
            producer = KafkaProducer(
                bootstrap_servers=BOOTSTRAP.split(","),
                value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                acks="all",
                retries=3,
            )
            LOG.info("Kafka producer connected to %s", BOOTSTRAP)
            return producer
        except NoBrokersAvailable as exc:
            LOG.warning("Kafka unavailable (%s), retrying in 5s", exc)
            time.sleep(5)


def main() -> None:
    running = [True]
    producer = _create_producer()

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    LOG.info(
        "Publishing mock crusher state to %s every %.0fs",
        KAFKA_TOPIC,
        PUBLISH_INTERVAL_SEC,
    )

    while running[0]:
        try:
            crushers = _load_crushers()
            for crusher_name, state in sorted(crushers.items()):
                event = dict(state)
                event["updated_at"] = _now_iso()
                event["source"] = "demo-crusher-state-producer"
                producer.send(KAFKA_TOPIC, key=crusher_name, value=event)
                LOG.info(
                    "Published %s status=%s fill=%.0f%% at_capacity=%s",
                    crusher_name,
                    event.get("status"),
                    float(event.get("fill_pct", 0)),
                    event.get("at_capacity"),
                )
            producer.flush(timeout=5)
        except Exception as exc:
            LOG.error("Publish cycle failed: %s", exc)

        for _ in range(int(PUBLISH_INTERVAL_SEC * 10)):
            if not running[0]:
                break
            time.sleep(0.1)

    producer.close()
    LOG.info("Stop requested, exiting")
    sys.exit(0)


if __name__ == "__main__":
    main()
