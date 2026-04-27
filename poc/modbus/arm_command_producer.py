#!/usr/bin/env python3
"""
Publish arm commands to Kafka as JSON: {"action": "left" | "right" | "stop"}.
Intended for kafka_to_arm_modbus.py (10s interval by default, configurable).
"""

import json
import logging
import os
import signal
import sys
import time

from kafka import KafkaProducer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("arm_command_producer")

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or os.environ.get(
    "KAFKA_BOOTSTRAP", "my-cluster-kafka-bootstrap.kafka-demo.svc:9092"
)
TOPIC = os.environ.get("KAFKA_TOPIC_ARM_COMMANDS", "modbus.pipeline.arm.commands")
INTERVAL = float(os.environ.get("ARM_COMMAND_INTERVAL_SEC", "10"))
# Comma-separated cycle, e.g. "left,right,stop" — mapped to the JSON the consumer expects
_raw = os.environ.get("ARM_COMMAND_SEQUENCE", "left,right,stop").strip()


def _actions_from_env() -> list:
    if not _raw:
        return ["left", "right", "stop"]
    out = []
    for p in _raw.split(","):
        a = p.strip().lower()
        if not a:
            continue
        if a in ("left", "l", "1"):
            out.append("left")
        elif a in ("right", "r", "2"):
            out.append("right")
        elif a in ("stop", "idle", "0", "s", ""):
            out.append("stop")
    return out or ["left", "right", "stop"]


def main() -> None:
    actions = _actions_from_env()
    running = [True]

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP.split(","),
        value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode("utf-8"),
    )
    n = 0
    LOG.info(
        "Arm command → Kafka: topic %s every %ss sequence=%s (bootstrap %s)",
        TOPIC,
        INTERVAL,
        actions,
        BOOTSTRAP,
    )
    while running[0]:
        action = actions[n % len(actions)]
        n += 1
        payload = {"action": action}
        producer.send(TOPIC, value=payload)
        producer.flush()
        LOG.info("Published %s", payload)
        # Slice sleep so SIGTERM is picked up within ~0.1s
        end = time.monotonic() + INTERVAL
        while time.monotonic() < end and running[0]:
            time.sleep(0.1)
    LOG.info("Stop requested, exiting")
    sys.exit(0)


if __name__ == "__main__":
    main()
