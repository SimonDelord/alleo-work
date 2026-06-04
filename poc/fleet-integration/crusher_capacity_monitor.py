#!/usr/bin/env python3
"""
Crusher capacity monitor: resumes stopped haul trucks when crusher fill drops
below a configurable threshold (default 50%).

Consumes Kafka crusher state and truck telemetry; publishes resume commands and
new-destination assignments directly to MQTT (cross-namespace into truck-fleet).

Complements destination-router (stop on full capacity, reroute) — this service
handles resume when fill_pct falls below the threshold.
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

import paho.mqtt.client as mqtt
from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("crusher_capacity_monitor")

BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    os.environ.get("KAFKA_BOOTSTRAP", "my-cluster-kafka-bootstrap.kafka-demo.svc:9092"),
)
TOPIC_TRUCK_TELEMETRY = os.environ.get("KAFKA_TOPIC_TRUCK_TELEMETRY", "fleet.trucks.telemetry")
TOPIC_CRUSHER_STATE = os.environ.get("KAFKA_TOPIC_CRUSHER_STATE", "fleet.crushers.state")
CONSUMER_GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "crusher-capacity-monitor")
RESUME_THRESHOLD_PCT = float(os.environ.get("CRUSHER_RESUME_THRESHOLD_PCT", "50"))
VALID_CRUSHERS = frozenset(
    c.strip()
    for c in os.environ.get("VALID_CRUSHERS", "crusher-1,crusher-2").split(",")
    if c.strip()
)
NEW_DESTINATION_TOPIC = os.environ.get("MQTT_NEW_DESTINATION_TOPIC", "new-destination")
TRUCK_COMMAND_TOPIC_PREFIX = os.environ.get("MQTT_TRUCK_COMMAND_TOPIC_PREFIX", "fleet/trucks")
ROUTING_SOURCE = os.environ.get("ROUTING_SOURCE", "fleet-integration")
RESUME_REASON = os.environ.get("CRUSHER_RESUME_REASON", "crusher_below_50pct")
MANUAL_STOP_PREFIX = "manual"
SOURCE = "crusher-capacity-monitor"


def _parse_mqtt_broker() -> tuple[str, int]:
    broker = os.environ.get("MQTT_BROKER", "").strip()
    if broker:
        if ":" in broker:
            host, port_str = broker.rsplit(":", 1)
            return host, int(port_str)
        return broker, 1883
    host = os.environ.get("MQTT_HOST", "mqtt-broker.truck-fleet.svc")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    return host, port


MQTT_HOST, MQTT_PORT = _parse_mqtt_broker()


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


class CrusherCapacityMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trucks: dict[str, dict[str, Any]] = {}
        self._crushers: dict[str, dict[str, Any]] = {}
        self._crusher_below_threshold: dict[str, bool] = {}
        self._last_resume: dict[str, tuple[str, str]] = {}
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=SOURCE)
        self._consumer = self._create_consumer()

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

    def connect_mqtt(self, running: list[bool]) -> None:
        while running[0]:
            try:
                self._mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                LOG.info("Connected to MQTT %s:%s", MQTT_HOST, MQTT_PORT)
                return
            except OSError as exc:
                LOG.warning("MQTT connect failed (%s), retrying in 5s", exc)
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
                "paused_from_state": str(event.get("paused_from_state", "")),
                "stop_reason": str(event.get("stop_reason", "")),
                "haul_hold": bool(event.get("haul_hold", False)),
                "updated_at": event.get("timestamp") or _now_iso(),
            }

    def _update_crusher(self, event: dict[str, Any]) -> None:
        crusher_name = str(event.get("crusher_name") or event.get("crusher_id") or "")
        if not crusher_name:
            return
        fill_pct = float(event.get("fill_pct", 0))
        below = fill_pct < RESUME_THRESHOLD_PCT
        with self._lock:
            prev_below = self._crusher_below_threshold.get(crusher_name)
            self._crushers[crusher_name] = {
                "crusher_name": crusher_name,
                "status": str(event.get("status", "unknown")),
                "fill_pct": fill_pct,
                "at_capacity": bool(event.get("at_capacity", False)),
                "updated_at": event.get("updated_at") or _now_iso(),
            }
            self._crusher_below_threshold[crusher_name] = below
            threshold_crossed = prev_below is not None and prev_below != below
        if threshold_crossed:
            LOG.info(
                "Crusher %s fill=%.0f%% crossed resume threshold (below=%s)",
                crusher_name,
                fill_pct,
                below,
            )

    def _crusher_fill(self, crusher_name: str) -> float:
        with self._lock:
            crusher = self._crushers.get(crusher_name)
            if crusher is None:
                return 100.0
            return float(crusher.get("fill_pct", 100.0))

    def _crusher_below_threshold(self, crusher_name: str) -> bool:
        with self._lock:
            if crusher_name in self._crusher_below_threshold:
                return self._crusher_below_threshold[crusher_name]
            crusher = self._crushers.get(crusher_name)
            if crusher is None:
                return False
            return float(crusher.get("fill_pct", 100.0)) < RESUME_THRESHOLD_PCT

    def _any_crusher_below_threshold(self) -> bool:
        if not VALID_CRUSHERS:
            return False
        return any(self._crusher_below_threshold(name) for name in VALID_CRUSHERS)

    def _is_manual_haul_hold(self, truck_id: str) -> bool:
        with self._lock:
            truck = self._trucks.get(truck_id)
            if truck is None:
                return False
            if bool(truck.get("haul_hold")):
                return True
            stop_reason = str(truck.get("stop_reason", ""))
            return stop_reason.startswith(MANUAL_STOP_PREFIX)

    def _is_eligible_stopped_truck(self, truck_id: str) -> bool:
        with self._lock:
            truck = self._trucks.get(truck_id)
            if truck is None:
                return False
            if str(truck.get("state", "")) != "stopped":
                return False
            paused_from = str(truck.get("paused_from_state", ""))
            if paused_from and paused_from != "hauling":
                return False
        if self._is_manual_haul_hold(truck_id):
            return False
        return True

    def _pick_crusher(self, truck_id: str) -> str | None:
        with self._lock:
            truck = self._trucks.get(truck_id)
            destination = str(truck.get("destination_crusher", "")) if truck else ""

        if destination and destination in VALID_CRUSHERS:
            if self._crusher_below_threshold(destination):
                return destination

        candidates: list[tuple[str, float]] = []
        for name in VALID_CRUSHERS:
            if self._crusher_below_threshold(name):
                candidates.append((name, self._crusher_fill(name)))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[1])[0]

    def _publish_destination(self, truck_id: str, crusher_name: str) -> None:
        topic = f"{NEW_DESTINATION_TOPIC}/{truck_id}/{crusher_name}"
        payload = {
            "truck_id": truck_id,
            "crusher_name": crusher_name,
            "assigned_at": _now_iso(),
            "reason": RESUME_REASON,
            "source": SOURCE,
        }
        self._mqtt.publish(
            topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=True,
        )
        LOG.info("Published MQTT %s (reason=%s)", topic, RESUME_REASON)

    def _publish_resume(self, truck_id: str, crusher_name: str) -> None:
        topic = f"{TRUCK_COMMAND_TOPIC_PREFIX}/{truck_id}/command"
        payload = {
            "action": "resume",
            "truck_id": truck_id,
            "reason": RESUME_REASON,
            "crusher_name": crusher_name,
            "source": SOURCE,
            "decided_at": _now_iso(),
        }
        self._mqtt.publish(
            topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        LOG.info(
            "Published MQTT %s action=resume crusher=%s (reason=%s)",
            topic,
            crusher_name,
            RESUME_REASON,
        )

    def _maybe_resume_truck(self, truck_id: str, force: bool = False) -> None:
        if not self._any_crusher_below_threshold():
            return
        if not self._is_eligible_stopped_truck(truck_id):
            return

        crusher = self._pick_crusher(truck_id)
        if crusher is None:
            return

        resume_key = (RESUME_REASON, crusher)
        with self._lock:
            if not force and self._last_resume.get(truck_id) == resume_key:
                return
            self._last_resume[truck_id] = resume_key

        self._publish_destination(truck_id, crusher)
        self._publish_resume(truck_id, crusher)

    def _evaluate_all_trucks(self, force: bool = False) -> None:
        if not self._any_crusher_below_threshold():
            return
        with self._lock:
            truck_ids = list(self._trucks.keys())
        for truck_id in truck_ids:
            self._maybe_resume_truck(truck_id, force=force)

    def _handle_truck_telemetry(self, event: dict[str, Any]) -> None:
        truck_id = str(event.get("truck_id", ""))
        prev_state = ""
        with self._lock:
            prev = self._trucks.get(truck_id)
            if prev:
                prev_state = str(prev.get("state", ""))

        self._update_truck(event)
        state = str(event.get("state", ""))

        if state == "stopped" and prev_state != "stopped":
            self._maybe_resume_truck(truck_id, force=True)
            return
        if state == "stopped":
            self._maybe_resume_truck(truck_id)
        elif state != "stopped":
            with self._lock:
                self._last_resume.pop(truck_id, None)

    def _handle_crusher_state(self, event: dict[str, Any]) -> None:
        crusher_name = str(event.get("crusher_name") or event.get("crusher_id") or "")
        prev_below: bool | None = None
        with self._lock:
            if crusher_name:
                prev_below = self._crusher_below_threshold.get(crusher_name)

        self._update_crusher(event)

        fill_pct = float(event.get("fill_pct", 0))
        now_below = fill_pct < RESUME_THRESHOLD_PCT
        if prev_below is not None and not prev_below and now_below:
            self._evaluate_all_trucks(force=True)
        elif now_below:
            self._evaluate_all_trucks()

    def run(self, running: list[bool]) -> None:
        LOG.info(
            "Crusher capacity monitor started: consume [%s, %s] → MQTT resume + %s",
            TOPIC_TRUCK_TELEMETRY,
            TOPIC_CRUSHER_STATE,
            NEW_DESTINATION_TOPIC,
        )
        LOG.info(
            "Resume threshold=%.0f%% broker=%s:%s group=%s",
            RESUME_THRESHOLD_PCT,
            MQTT_HOST,
            MQTT_PORT,
            CONSUMER_GROUP,
        )
        self.connect_mqtt(running)
        if not running[0]:
            return

        self._mqtt.loop_start()
        try:
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
        finally:
            self._consumer.close()
            self._mqtt.loop_stop()
            self._mqtt.disconnect()


def main() -> None:
    running = [True]

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    monitor = CrusherCapacityMonitor()
    try:
        monitor.run(running)
    finally:
        LOG.info("Stop requested, exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
