#!/usr/bin/env python3
"""
Routing intelligence: consumes truck telemetry and crusher state from Kafka,
decides destination changes, and produces fleet.routing.commands.

When both crushers are at capacity, hauling trucks receive stop commands on
fleet.truck.commands. Resume when capacity is available again.

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
TOPIC_TRUCK_COMMANDS = os.environ.get("KAFKA_TOPIC_TRUCK_COMMANDS", "fleet.truck.commands")
CONSUMER_GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "destination-router")
VALID_CRUSHERS = frozenset(
    c.strip()
    for c in os.environ.get("VALID_CRUSHERS", "crusher-1,crusher-2").split(",")
    if c.strip()
)
FALLBACK_CRUSHER = os.environ.get("FALLBACK_CRUSHER", "crusher-2")
HAULING_STATES = frozenset(
    s.strip()
    for s in os.environ.get("ROUTING_ACTIVE_STATES", "hauling").split(",")
    if s.strip()
)
STOP_REASON = os.environ.get("FLEET_STOP_REASON", "both_crushers_at_capacity")
MANUAL_STOP_PREFIX = "manual"


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
        self._last_routing_command: dict[str, str] = {}
        self._stopped_by_router: set[str] = set()
        self._manual_haul_hold: set[str] = set()
        self._last_truck_action: dict[str, str] = {}
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
                    TOPIC_TRUCK_COMMANDS,
                    bootstrap_servers=BOOTSTRAP.split(","),
                    group_id=CONSUMER_GROUP,
                    auto_offset_reset="latest",
                    enable_auto_commit=True,
                    value_deserializer=lambda v: v,
                )
                LOG.info(
                    "Kafka consumer subscribed to %s, %s, %s",
                    TOPIC_TRUCK_TELEMETRY,
                    TOPIC_CRUSHER_STATE,
                    TOPIC_TRUCK_COMMANDS,
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
                "stop_reason": str(event.get("stop_reason", "")),
                "haul_hold": bool(event.get("haul_hold", False)),
                "updated_at": event.get("timestamp") or _now_iso(),
            }
            state = str(event.get("state", "unknown"))
            if state != "stopped":
                self._stopped_by_router.discard(truck_id)
                if not bool(event.get("haul_hold", False)):
                    self._manual_haul_hold.discard(truck_id)

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

    def _all_crushers_at_capacity(self) -> bool:
        if not VALID_CRUSHERS:
            return False
        with self._lock:
            for crusher_name in VALID_CRUSHERS:
                crusher = self._crushers.get(crusher_name)
                if crusher is None:
                    return False
                if not (
                    crusher.get("at_capacity") or str(crusher.get("status")) == "full"
                ):
                    return False
        return True

    def _alternate_crusher(self, current: str) -> str | None:
        alternatives = sorted(c for c in VALID_CRUSHERS if c != current)
        for candidate in alternatives:
            if not self._crusher_at_capacity(candidate):
                return candidate
        return None

    def _emit_routing_command(self, truck_id: str, crusher_name: str, reason: str) -> None:
        with self._lock:
            if self._last_routing_command.get(truck_id) == crusher_name:
                return
            self._last_routing_command[truck_id] = crusher_name

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

    def _emit_truck_command(self, truck_id: str, action: str, reason: str) -> None:
        with self._lock:
            truck = self._trucks.get(truck_id)
            last_action = self._last_truck_action.get(truck_id)
            if last_action == action:
                if (
                    action != "resume"
                    or truck is None
                    or str(truck.get("state", "")) != "stopped"
                ):
                    return
            self._last_truck_action[truck_id] = action
            if action == "stop":
                self._stopped_by_router.add(truck_id)
            elif action == "resume":
                self._stopped_by_router.discard(truck_id)

        command = {
            "truck_id": truck_id,
            "action": action,
            "reason": reason,
            "decided_at": _now_iso(),
            "source": "destination-router",
        }
        self._producer.send(TOPIC_TRUCK_COMMANDS, key=truck_id, value=command)
        self._producer.flush(timeout=5)
        LOG.info("Truck command: %s action=%s (reason=%s)", truck_id, action, reason)

    def _is_manual_haul_hold(self, truck_id: str) -> bool:
        with self._lock:
            if truck_id in self._manual_haul_hold:
                return True
            truck = self._trucks.get(truck_id)
            if truck is None:
                return False
            stop_reason = str(truck.get("stop_reason", ""))
            return stop_reason.startswith(MANUAL_STOP_PREFIX) or bool(truck.get("haul_hold"))

    def _handle_truck_command(self, event: dict[str, Any]) -> None:
        source = str(event.get("source", ""))
        if source == "destination-router":
            return

        truck_id = str(event.get("truck_id", ""))
        if not truck_id:
            return

        action = str(event.get("action", "")).lower()
        reason = str(event.get("reason", ""))

        with self._lock:
            if action == "stop" and reason.startswith(MANUAL_STOP_PREFIX):
                self._manual_haul_hold.add(truck_id)
                self._stopped_by_router.discard(truck_id)
                LOG.info("Manual haul hold recorded for %s (reason=%s)", truck_id, reason)
            elif action in ("resume", "clear") and reason.startswith(MANUAL_STOP_PREFIX):
                self._manual_haul_hold.discard(truck_id)
                LOG.info("Manual haul hold cleared for %s (reason=%s)", truck_id, reason)

    def _evaluate_fleet_stop(self) -> None:
        if self._all_crushers_at_capacity():
            with self._lock:
                truck_ids = list(self._trucks.keys())
            for truck_id in truck_ids:
                with self._lock:
                    truck = self._trucks.get(truck_id)
                if truck is None:
                    continue
                if str(truck.get("state", "")) in HAULING_STATES:
                    self._emit_truck_command(truck_id, "stop", STOP_REASON)
            return

        with self._lock:
            truck_ids = list(self._trucks.keys())
        for truck_id in truck_ids:
            with self._lock:
                truck = self._trucks.get(truck_id)
            if truck is None:
                continue
            if str(truck.get("state", "")) == "stopped":
                if self._is_manual_haul_hold(truck_id):
                    continue
                with self._lock:
                    if truck_id not in self._stopped_by_router:
                        continue
                self._emit_truck_command(truck_id, "resume", "crusher_capacity_available")

    def _evaluate_truck(self, truck_id: str) -> None:
        if self._all_crushers_at_capacity():
            return

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
            LOG.debug("No alternate crusher for %s (current=%s)", truck_id, destination)
            return
        if alternate == destination:
            return

        self._emit_routing_command(
            truck_id,
            alternate,
            f"{destination}_at_capacity",
        )

    def _evaluate_all_trucks(self) -> None:
        self._evaluate_fleet_stop()
        if self._all_crushers_at_capacity():
            return
        with self._lock:
            truck_ids = list(self._trucks.keys())
        for truck_id in truck_ids:
            self._evaluate_truck(truck_id)

    def _handle_truck_telemetry(self, event: dict[str, Any]) -> None:
        truck_id = str(event.get("truck_id", ""))
        self._update_truck(event)
        self._evaluate_all_trucks()

    def _handle_crusher_state(self, event: dict[str, Any]) -> None:
        self._update_crusher(event)
        self._evaluate_all_trucks()

    def run(self, running: list[bool]) -> None:
        LOG.info(
            "Destination router started: consume [%s, %s, %s] → produce %s, %s",
            TOPIC_TRUCK_TELEMETRY,
            TOPIC_CRUSHER_STATE,
            TOPIC_TRUCK_COMMANDS,
            TOPIC_ROUTING_COMMANDS,
            TOPIC_TRUCK_COMMANDS,
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
            elif message.topic == TOPIC_TRUCK_COMMANDS:
                self._handle_truck_command(event)

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
