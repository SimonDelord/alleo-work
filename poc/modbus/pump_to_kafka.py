#!/usr/bin/env python3
"""
Polls the **pump** Modbus HR0 and publishes JSON to Kafka when the value changes
(or first seen). State: 0=off, 1=on.
"""

import json
import logging
import os
import signal
import sys
import time
from typing import Optional

from kafka import KafkaProducer
from pymodbus.client import ModbusTcpClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("pump_to_kafka")

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or os.environ.get(
    "KAFKA_BOOTSTRAP", "my-cluster-kafka-bootstrap.kafka-demo.svc:9092"
)
TOPIC = os.environ.get("KAFKA_TOPIC_PUMP", "modbus.pipeline.pump.status")
PUMP_MODBUS = os.environ.get("PUMP_MODBUS", "pump-plc:5020")
POLL = float(os.environ.get("POLL_SEC", "1.0"))


def _parse_host_port(target: str) -> tuple:
    if ":" in target:
        h, p = target.rsplit(":", 1)
        return h, int(p)
    return target, 5020


def main() -> None:
    h, p = _parse_host_port(PUMP_MODBUS)
    running = [True]

    def _stop(*_: object) -> None:
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP.split(","),
        value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode("utf-8"),
    )
    last: Optional[int] = None
    LOG.info("Pump → Kafka: modbus %s:%s -> topic %s (bootstrap %s)", h, p, TOPIC, BOOTSTRAP)
    while running[0]:
        client = ModbusTcpClient(h, port=p, timeout=5)
        try:
            if not client.connect():
                LOG.warning("Modbus connect failed, retrying in 5s")
                time.sleep(5)
                continue
            r = client.read_holding_registers(0, 1, slave=0)
            if r.isError():
                LOG.warning("Modbus read error: %s", r)
            else:
                v = int(r.registers[0]) & 0xFFFF
                if last is None or v != last:
                    last = v
                    payload = {
                        "pump": "on" if v == 1 else "off" if v == 0 else "unknown",
                        "raw_hr0": v,
                    }
                    producer.send(TOPIC, value=payload)
                    producer.flush()
                    LOG.info("Published pump state: %s", payload)
        finally:
            try:
                client.close()
            except OSError:
                pass
        time.sleep(POLL)
    LOG.info("Stop requested, exiting")
    sys.exit(0)


if __name__ == "__main__":
    main()
