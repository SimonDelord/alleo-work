#!/usr/bin/env python3
"""
Consumes **arm** commands from Kafka and writes **holding register 0** on the arm
Modbus "PLC" host. Message JSON: {"action": "left"|"right"|"stop"} (or legacy string body).
0=idle, 1=left, 2=right in HR0.
"""

import json
import logging
import os
import signal
import sys
from typing import Optional

from kafka import KafkaConsumer
from pymodbus.client import ModbusTcpClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("kafka_to_arm")

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or os.environ.get(
    "KAFKA_BOOTSTRAP", "my-cluster-kafka-bootstrap.kafka-demo.svc:9092"
)
TOPIC = os.environ.get("KAFKA_TOPIC_ARM_COMMANDS", "modbus.pipeline.arm.commands")
GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "modbus-arm-writer")
ARM_MODBUS = os.environ.get("ARM_MODBUS", "arm-plc:5020")


def _parse_host_port(target: str) -> tuple:
    if ":" in target:
        h, p = target.rsplit(":", 1)
        return h, int(p)
    return target, 5020


def _action_to_hr0(action: str) -> int:
    a = (action or "stop").strip().lower()
    if a in ("left", "l", "1"):
        return 1
    if a in ("right", "r", "2"):
        return 2
    if a in ("stop", "idle", "0", ""):
        return 0
    return 0


def _parse_value(raw: object) -> int:
    if raw is None:
        return 0
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _action_to_hr0(raw.decode("utf-8", errors="replace"))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return _action_to_hr0(raw)
    if isinstance(raw, dict):
        return _action_to_hr0(str(raw.get("action", raw.get("command", "stop"))))
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n if 0 <= n <= 2 else 0
    return 0


def main() -> None:
    h, p = _parse_host_port(ARM_MODBUS)
    running = [True]

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP.split(","),
        group_id=GROUP,
        value_deserializer=lambda b: b,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    client: Optional[ModbusTcpClient] = None
    LOG.info("Kafka → Arm: topic %s (group %s) -> modbus %s:%s (HR0: 0=idle,1=left,2=right)", TOPIC, GROUP, h, p)

    def get_client() -> ModbusTcpClient:
        nonlocal client
        if client is not None and client.is_socket_open():
            return client
        if client is not None:
            try:
                client.close()
            except OSError:
                pass
        client = ModbusTcpClient(h, port=p, timeout=5)
        if not client.connect():
            client = None
            raise ConnectionError("Modbus connect failed")
        return client

    while running[0]:
        try:
            records = consumer.poll(timeout_ms=2000)
        except Exception:
            LOG.exception("consumer.poll failed")
            break
        if not records or not running[0]:
            continue
        for _tp, batch in records.items():
            for msg in batch:
                if not running[0]:
                    break
                v = _parse_value(msg.value)
                try:
                    c = get_client()
                    w = c.write_register(0, v, slave=0)
                    if w.isError():
                        LOG.warning("Modbus write error: %s", w)
                    else:
                        LOG.info("Wrote arm HR0=%s (partition=%s offset=%s)", v, msg.partition, msg.offset)
                except (ConnectionError, OSError) as e:
                    LOG.warning("Modbus: %s", e)
                except Exception:
                    LOG.exception("Failed to write arm from Kafka")
            if not running[0]:
                break
        if not running[0]:
            break
    if client is not None:
        try:
            client.close()
        except OSError:
            pass
    consumer.close()
    LOG.info("Stop requested, exiting")
    sys.exit(0)


if __name__ == "__main__":
    main()
