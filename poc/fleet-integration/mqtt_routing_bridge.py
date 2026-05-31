#!/usr/bin/env python3
"""
Orchestration bridge: consumes fleet.routing.commands from Kafka and publishes
retained new-destination MQTT messages to truck-fleet broker.

This is the only component that crosses into truck-fleet MQTT — trucks remain
unchanged and subscribe to new-destination/{truck_id}/{crusher_name} as before.
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
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC_ROUTING_COMMANDS", "fleet.routing.commands")
CONSUMER_GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "mqtt-routing-bridge")
MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker.truck-fleet.svc")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
NEW_DESTINATION_TOPIC = os.environ.get("MQTT_NEW_DESTINATION_TOPIC", "new-destination")
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


class MqttRoutingBridge:
    def __init__(self) -> None:
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mqtt-routing-bridge")
        self._last_published: dict[str, str] = {}

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

        if self._last_published.get(truck_id) == crusher_name:
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
        self._last_published[truck_id] = crusher_name
        LOG.info("Published MQTT %s (reason=%s)", topic, payload.get("reason"))

    def run(self, running: list[bool]) -> None:
        self.connect_mqtt()
        self._mqtt.loop_start()

        while running[0]:
            try:
                consumer = KafkaConsumer(
                    KAFKA_TOPIC,
                    bootstrap_servers=BOOTSTRAP.split(","),
                    group_id=CONSUMER_GROUP,
                    auto_offset_reset="latest",
                    enable_auto_commit=True,
                    value_deserializer=lambda v: v,
                )
                LOG.info(
                    "Kafka consumer subscribed to %s, publishing to MQTT %s",
                    KAFKA_TOPIC,
                    NEW_DESTINATION_TOPIC,
                )
                for message in consumer:
                    if not running[0]:
                        break
                    command = _parse_command(message.value)
                    if command is not None:
                        self.publish_destination(command)
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
