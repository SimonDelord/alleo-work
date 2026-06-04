#!/usr/bin/env python3
"""
Kafka → MQTT bridge for fleet orchestration commands.

Consumes:
  - fleet.routing.commands → retained new-destination/{truck_id}/{crusher_name}
  - fleet.truck.commands   → fleet/trucks/{truck_id}/command (stop/resume JSON)

This is the only component that crosses into truck-fleet MQTT — trucks subscribe
to new-destination and command topics as before.
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

import paho.mqtt.client as mqtt
from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("mqtt_routing_bridge")

BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    os.environ.get("KAFKA_BOOTSTRAP", "my-cluster-kafka-bootstrap.kafka-demo.svc:9092"),
)
TOPIC_ROUTING_COMMANDS = os.environ.get("KAFKA_TOPIC_ROUTING_COMMANDS", "fleet.routing.commands")
TOPIC_TRUCK_COMMANDS = os.environ.get("KAFKA_TOPIC_TRUCK_COMMANDS", "fleet.truck.commands")
CONSUMER_GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "mqtt-routing-bridge")
MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker.truck-fleet.svc")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
NEW_DESTINATION_TOPIC = os.environ.get("MQTT_NEW_DESTINATION_TOPIC", "new-destination")
TRUCK_COMMAND_TOPIC_PREFIX = os.environ.get("MQTT_TRUCK_COMMAND_TOPIC_PREFIX", "fleet/trucks")
ROUTING_SOURCE = os.environ.get("ROUTING_SOURCE", "fleet-integration")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_command(raw: bytes | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _kafka_topics() -> list[str]:
    return [TOPIC_ROUTING_COMMANDS, TOPIC_TRUCK_COMMANDS]


class MqttRoutingBridge:
    def __init__(self) -> None:
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mqtt-routing-bridge")
        self._last_published_destination: dict[str, str] = {}
        self._last_published_action: dict[str, str] = {}

    def connect_mqtt(self) -> None:
        while True:
            try:
                self._mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                LOG.info("Connected to MQTT %s:%s", MQTT_HOST, MQTT_PORT)
                return
            except OSError as exc:
                LOG.warning("MQTT connect failed (%s), retrying in 5s", exc)
                time.sleep(5)

    def publish_destination(self, command: dict[str, Any]) -> None:
        truck_id = str(command.get("truck_id", ""))
        crusher_name = str(command.get("crusher_name", ""))
        if not truck_id or not crusher_name:
            LOG.warning("Ignoring invalid routing command: %s", command)
            return

        if self._last_published_destination.get(truck_id) == crusher_name:
            LOG.debug("Already published %s → %s, skipping", truck_id, crusher_name)
            return

        topic = f"{NEW_DESTINATION_TOPIC}/{truck_id}/{crusher_name}"
        payload = {
            "truck_id": truck_id,
            "crusher_name": crusher_name,
            "assigned_at": command.get("decided_at") or _now_iso(),
            "reason": command.get("reason"),
            "source": command.get("source") or ROUTING_SOURCE,
        }
        self._mqtt.publish(
            topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=True,
        )
        self._last_published_destination[truck_id] = crusher_name
        LOG.info("Published MQTT %s (reason=%s)", topic, payload.get("reason"))

    def publish_truck_command(self, command: dict[str, Any]) -> None:
        truck_id = str(command.get("truck_id", ""))
        action = str(command.get("action", "")).lower()
        if not truck_id or action not in ("stop", "resume", "clear"):
            LOG.warning("Ignoring invalid truck command: %s", command)
            return

        if self._last_published_action.get(truck_id) == action and action not in ("resume", "clear"):
            LOG.debug("Already published %s action=%s, skipping", truck_id, action)
            return

        topic = f"{TRUCK_COMMAND_TOPIC_PREFIX}/{truck_id}/command"
        payload = {
            "action": action,
            "truck_id": truck_id,
            "reason": command.get("reason"),
            "source": command.get("source") or ROUTING_SOURCE,
            "decided_at": command.get("decided_at") or _now_iso(),
        }
        self._mqtt.publish(
            topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        self._last_published_action[truck_id] = action
        LOG.info("Published MQTT %s action=%s (reason=%s)", topic, action, payload.get("reason"))

    def handle_kafka_message(self, topic: str, command: dict[str, Any]) -> None:
        if topic == TOPIC_ROUTING_COMMANDS:
            self.publish_destination(command)
        elif topic == TOPIC_TRUCK_COMMANDS:
            self.publish_truck_command(command)
        else:
            LOG.warning("Unexpected Kafka topic %s", topic)

    def run(self, running: list[bool]) -> None:
        self.connect_mqtt()
        self._mqtt.loop_start()

        while running[0]:
            try:
                consumer = KafkaConsumer(
                    *_kafka_topics(),
                    bootstrap_servers=BOOTSTRAP.split(","),
                    group_id=CONSUMER_GROUP,
                    auto_offset_reset="latest",
                    enable_auto_commit=True,
                    value_deserializer=lambda v: v,
                )
                LOG.info(
                    "Kafka consumer subscribed to %s, publishing routing → %s and commands → %s/+/command",
                    _kafka_topics(),
                    NEW_DESTINATION_TOPIC,
                    TRUCK_COMMAND_TOPIC_PREFIX,
                )
                for message in consumer:
                    if not running[0]:
                        break
                    command = _parse_command(message.value)
                    if command is not None:
                        self.handle_kafka_message(message.topic, command)
                consumer.close()
            except NoBrokersAvailable as exc:
                if not running[0]:
                    break
                LOG.warning("Kafka unavailable (%s), retrying in 5s", exc)
                time.sleep(5)

        self._mqtt.loop_stop()
        self._mqtt.disconnect()


def main() -> None:
    running = [True]

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    bridge = MqttRoutingBridge()
    try:
        bridge.run(running)
    finally:
        LOG.info("Stop requested, exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
