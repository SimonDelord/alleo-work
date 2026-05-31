#!/usr/bin/env python3
"""
Demo bridge: subscribes to truck MQTT telemetry and produces to Kafka.

Keeps truck-fleet untouched — reads from the truck-fleet MQTT broker
(cross-namespace) and writes fleet.trucks.telemetry events.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from typing import Any

import paho.mqtt.client as mqtt
from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("kafka_truck_bridge")

MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker.truck-fleet.svc")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC_SUBSCRIBE", "fleet/trucks/+/telemetry")
BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    os.environ.get("KAFKA_BOOTSTRAP", "my-cluster-kafka-bootstrap.kafka-demo.svc:9092"),
)
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC_TRUCK_TELEMETRY", "fleet.trucks.telemetry")


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

    def on_connect(
        client: mqtt.Client,
        userdata: object,
        flags: dict,
        reason_code: int,
        properties: object,
    ) -> None:
        del userdata, flags, properties
        if reason_code == 0:
            client.subscribe(MQTT_TOPIC, qos=0)
            LOG.info("Subscribed to MQTT %s", MQTT_TOPIC)
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
                return
            truck_id = str(payload.get("truck_id", "unknown"))
            event: dict[str, Any] = dict(payload)
            event.setdefault("source", "mqtt-bridge")
            producer.send(KAFKA_TOPIC, key=truck_id, value=event)
            producer.flush(timeout=5)
            LOG.debug("Produced telemetry for %s to %s", truck_id, KAFKA_TOPIC)
        except json.JSONDecodeError:
            LOG.warning("Invalid JSON on topic %s", msg.topic)
        except Exception as exc:
            LOG.error("Failed to produce to Kafka: %s", exc)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="kafka-truck-bridge")
    client.on_connect = on_connect
    client.on_message = on_message

    LOG.info(
        "Starting kafka-truck-bridge: MQTT %s:%s → Kafka %s topic %s",
        MQTT_HOST,
        MQTT_PORT,
        BOOTSTRAP,
        KAFKA_TOPIC,
    )

    while running[0]:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            break
        except OSError as exc:
            LOG.warning("MQTT connect failed (%s), retrying in 5s", exc)
            time.sleep(5)

    client.loop_start()
    try:
        while running[0]:
            time.sleep(1)
    finally:
        client.loop_stop()
        client.disconnect()
        producer.close()

    LOG.info("Stop requested, exiting")
    sys.exit(0)


if __name__ == "__main__":
    main()
