"""Kafka command producer for fleet-live-map manual operator actions."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable

LOG = logging.getLogger("kafka_commands")

BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    "mining-fleet-cluster-kafka-bootstrap.mining-fleet-kafka.svc:9092",
)
TOPIC_ROUTING_COMMANDS = os.environ.get("KAFKA_TOPIC_ROUTING_COMMANDS", "fleet.routing.commands")
TOPIC_TRUCK_COMMANDS = os.environ.get("KAFKA_TOPIC_TRUCK_COMMANDS", "fleet.truck.commands")
VALID_CRUSHERS = frozenset(
    c.strip()
    for c in os.environ.get("VALID_CRUSHERS", "crusher-1,crusher-2").split(",")
    if c.strip()
)
KNOWN_TRUCKS = frozenset(
    t.strip()
    for t in os.environ.get("FLEET_TRUCK_IDS", "TR1,TR2,TR3").split(",")
    if t.strip()
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KafkaCommandProducer:
    """Lazy-connecting Kafka producer for routing and truck commands."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._producer: KafkaProducer | None = None

    def _get_producer(self) -> KafkaProducer:
        with self._lock:
            if self._producer is not None:
                return self._producer
            while True:
                try:
                    self._producer = KafkaProducer(
                        bootstrap_servers=BOOTSTRAP.split(","),
                        value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode("utf-8"),
                        key_serializer=lambda k: k.encode("utf-8") if k else None,
                        acks="all",
                        retries=3,
                    )
                    LOG.info("Kafka command producer connected to %s", BOOTSTRAP)
                    return self._producer
                except NoBrokersAvailable as exc:
                    LOG.warning("Kafka unavailable (%s), retrying in 5s", exc)
                    time.sleep(5)

    def _send(self, topic: str, key: str, value: dict[str, Any]) -> None:
        producer = self._get_producer()
        producer.send(topic, key=key, value=value)
        producer.flush(timeout=5)

    def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action", "")).lower().strip()
        if not action:
            raise ValueError("action is required")

        if action == "acknowledge":
            return {
                "status": "ok",
                "action": action,
                "message": body.get("message", "acknowledged"),
                "acknowledged_at": _now_iso(),
            }

        if action == "reroute":
            truck_id = str(body.get("truck_id", "")).strip()
            crusher_name = str(body.get("crusher_name", "")).strip()
            if truck_id not in KNOWN_TRUCKS:
                raise ValueError(f"unknown truck_id: {truck_id}")
            if crusher_name not in VALID_CRUSHERS:
                raise ValueError(f"unknown crusher_name: {crusher_name}")
            command = {
                "truck_id": truck_id,
                "crusher_name": crusher_name,
                "reason": str(body.get("reason", "manual_reroute")),
                "decided_at": _now_iso(),
                "source": "fleet-live-map",
            }
            self._send(TOPIC_ROUTING_COMMANDS, truck_id, command)
            return {"status": "ok", "action": action, "published": command}

        if action in ("stop", "resume"):
            truck_id = str(body.get("truck_id", "")).strip()
            if truck_id not in KNOWN_TRUCKS:
                raise ValueError(f"unknown truck_id: {truck_id}")
            command = {
                "truck_id": truck_id,
                "action": action,
                "reason": str(body.get("reason", f"manual_{action}")),
                "decided_at": _now_iso(),
                "source": "fleet-live-map",
            }
            self._send(TOPIC_TRUCK_COMMANDS, truck_id, command)
            return {"status": "ok", "action": action, "published": command}

        if action == "clear_fleet":
            published = []
            reason = str(body.get("reason", "manual_resume_fleet"))
            for truck_id in sorted(KNOWN_TRUCKS):
                command = {
                    "truck_id": truck_id,
                    "action": "clear",
                    "reason": reason,
                    "decided_at": _now_iso(),
                    "source": "fleet-live-map",
                }
                self._send(TOPIC_TRUCK_COMMANDS, truck_id, command)
                published.append(command)
            return {"status": "ok", "action": action, "published": published}

        if action == "stop_fleet":
            published = []
            reason = str(body.get("reason", "manual_stop_fleet"))
            for truck_id in sorted(KNOWN_TRUCKS):
                command = {
                    "truck_id": truck_id,
                    "action": "stop",
                    "reason": reason,
                    "decided_at": _now_iso(),
                    "source": "fleet-live-map",
                }
                self._send(TOPIC_TRUCK_COMMANDS, truck_id, command)
                published.append(command)
            return {"status": "ok", "action": action, "published": published}

        if action == "resume_fleet":
            published = []
            reason = str(body.get("reason", "manual_resume_fleet"))
            for truck_id in sorted(KNOWN_TRUCKS):
                command = {
                    "truck_id": truck_id,
                    "action": "resume",
                    "reason": reason,
                    "decided_at": _now_iso(),
                    "source": "fleet-live-map",
                }
                self._send(TOPIC_TRUCK_COMMANDS, truck_id, command)
                published.append(command)
            return {"status": "ok", "action": action, "published": published}

        raise ValueError(
            "unsupported action; use reroute, stop, resume, stop_fleet, resume_fleet, clear_fleet, or acknowledge"
        )

    def close(self) -> None:
        with self._lock:
            if self._producer is not None:
                self._producer.close()
                self._producer = None
