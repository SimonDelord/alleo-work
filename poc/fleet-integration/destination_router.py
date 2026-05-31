#!/usr/bin/env python3
"""
Routing intelligence: consumes truck telemetry and crusher state from Kafka,
decides destination changes, and produces fleet.routing.commands.

This service lives in fleet-integration — not in truck-fleet or crusher-fleet.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from typing import Any

from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("destination_router")

BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    os.environ.get("KAFKA_BOOTSTRAP", "my-cluster-kafka-bootstrap.kafka-demo.svc:9092"),
)
TOPIC_TRUCK_TELEMETRY = os.environ.get("KAFKA_TOPIC_TRUCK_TELEMETRY", "fleet.trucks.telemetry")
TOPIC_CRUSHER_STATE = os.environ.get("KAFKA_TOPIC_CRUSHER_STATE", "fleet.crushers.state")
TOPIC_ROUTING_COMMANDS = os.environ.get("KAFKA_TOPIC_ROUTING_COMMANDS", "fleet.routing.commands")
CONSUMER_GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "destination-router")
VALID_CRUSHERS = frozenset(
    c.strip()
    for c in os.environ.get("VALID_CRUSHERS", "crusher-1,crusher-2").split(",")
    if c.strip()
)
FALLBACK_CRUSHER = os.environ.get("FALLBACK_CRUSHER", "crusher-2")
HAULING_STATES = frozenset(
    s.strip()
    for s in os.environ.get("ROUTING_ACTIVE_STATES", "hauling,loading").split(",")
    if s.strip()
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(raw: bytes | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


class DestinationRouter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trucks: dict[str, dict[str, Any]] = {}
        self._crushers: dict[str, dict[str, Any]] = {}
        self._last_command: dict[str, str] = {}
        self._producer = self._create_producer()
        self._consumer = self._create_consumer()

    def _create_producer(self) -> KafkaProducer:
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
                import time

                time.sleep(5)

    def _create_consumer(self) -> KafkaConsumer:
        while True:
            try:
                consumer = KafkaConsumer(
                    TOPIC_TRUCK_TELEMETRY,
                    TOPIC_CRUSHER_STATE,
                    bootstrap_servers=BOOTSTRAP.split(","),
                    group_id=CONSUMER_GROUP,
                    auto_offset_reset="latest",
                    enable_auto_commit=True,
                    value_deserializer=lambda v: v,
                )
                LOG.info(
                    "Kafka consumer subscribed to %s, %s",
                    TOPIC_TRUCK_TELEMETRY,
                    TOPIC_CRUSHER_STATE,
                )
                return consumer
            except NoBrokersAvailable as exc:
                LOG.warning("Kafka unavailable (%s), retrying in 5s", exc)
                import time

                time.sleep(5)

    def _update_truck(self, event: dict[str, Any]) -> None:
        truck_id = str(event.get("truck_id", ""))
        if not truck_id:
            return
        with self._lock:
            self._trucks[truck_id] = {
                "truck_id": truck_id,
                "state": str(event.get("state", "unknown")),
                "destination_crusher": str(event.get("destination_crusher", "")),
                "load_pct": float(event.get("load_pct", 0)),
                "updated_at": event.get("timestamp") or _now_iso(),
            }

    def _update_crusher(self, event: dict[str, Any]) -> None:
        crusher_name = str(event.get("crusher_name") or event.get("crusher_id") or "")
        if not crusher_name:
            return
        with self._lock:
            self._crushers[crusher_name] = {
                "crusher_name": crusher_name,
                "status": str(event.get("status", "unknown")),
                "fill_pct": float(event.get("fill_pct", 0)),
                "at_capacity": bool(event.get("at_capacity", False)),
                "updated_at": event.get("updated_at") or _now_iso(),
            }

    def _crusher_at_capacity(self, crusher_name: str) -> bool:
        with self._lock:
            crusher = self._crushers.get(crusher_name)
            if crusher is None:
                return False
            return bool(crusher.get("at_capacity")) or str(crusher.get("status")) == "full"

    def _alternate_crusher(self, current: str) -> str | None:
        alternatives = sorted(c for c in VALID_CRUSHERS if c != current)
        for candidate in alternatives:
            if not self._crusher_at_capacity(candidate):
                return candidate
        return None

    def _emit_routing_command(self, truck_id: str, crusher_name: str, reason: str) -> None:
        with self._lock:
            if self._last_command.get(truck_id) == crusher_name:
                return
            self._last_command[truck_id] = crusher_name

        command = {
            "truck_id": truck_id,
            "crusher_name": crusher_name,
            "reason": reason,
            "decided_at": _now_iso(),
            "source": "destination-router",
        }
        self._producer.send(TOPIC_ROUTING_COMMANDS, key=truck_id, value=command)
        self._producer.flush(timeout=5)
        LOG.info(
            "Routing command: %s → %s (reason=%s)",
            truck_id,
            crusher_name,
            reason,
        )

    def _evaluate_truck(self, truck_id: str) -> None:
        with self._lock:
            truck = self._trucks.get(truck_id)
            if truck is None:
                return
            state = str(truck.get("state", ""))
            destination = str(truck.get("destination_crusher", ""))

        if state not in HAULING_STATES:
            return
        if not destination or destination not in VALID_CRUSHERS:
            return
        if not self._crusher_at_capacity(destination):
            return

        alternate = self._alternate_crusher(destination)
        if alternate is None:
            alternate = FALLBACK_CRUSHER if FALLBACK_CRUSHER != destination else None
        if alternate is None or alternate == destination:
            LOG.debug("No alternate crusher for %s (current=%s)", truck_id, destination)
            return

        self._emit_routing_command(
            truck_id,
            alternate,
            f"{destination}_at_capacity",
        )

    def _evaluate_all_trucks(self) -> None:
        with self._lock:
            truck_ids = list(self._trucks.keys())
        for truck_id in truck_ids:
            self._evaluate_truck(truck_id)

    def _handle_truck_telemetry(self, event: dict[str, Any]) -> None:
        truck_id = str(event.get("truck_id", ""))
        self._update_truck(event)
        self._evaluate_truck(truck_id)

    def _handle_crusher_state(self, event: dict[str, Any]) -> None:
        self._update_crusher(event)
        self._evaluate_all_trucks()

    def run(self, running: list[bool]) -> None:
        LOG.info(
            "Destination router started: consume [%s, %s] → produce %s",
            TOPIC_TRUCK_TELEMETRY,
            TOPIC_CRUSHER_STATE,
            TOPIC_ROUTING_COMMANDS,
        )
        for message in self._consumer:
            if not running[0]:
                break
            event = _parse_json(message.value)
            if event is None:
                continue
            if message.topic == TOPIC_TRUCK_TELEMETRY:
                self._handle_truck_telemetry(event)
            elif message.topic == TOPIC_CRUSHER_STATE:
                self._handle_crusher_state(event)

        self._consumer.close()
        self._producer.close()


def main() -> None:
    running = [True]

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    router = DestinationRouter()
    try:
        router.run(running)
    finally:
        LOG.info("Stop requested, exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
