#!/usr/bin/env python3
"""Poll Modbus TCP holding registers and publish readings to Kafka."""

import json
import logging
import os
import time

from kafka import KafkaProducer
from pymodbus.client import ModbusTcpClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("modbus_producer")

TOPIC = os.environ.get("KAFKA_TOPIC_MODBUS", "poc.modbus.readings")
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
MODBUS_HOST = os.environ.get("MODBUS_HOST", "localhost")
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "5020"))
UNIT_ID = int(os.environ.get("MODBUS_UNIT_ID", "0"))
START_ADDR = int(os.environ.get("MODBUS_START_ADDRESS", "0"))
COUNT = int(os.environ.get("MODBUS_REGISTER_COUNT", "4"))
POLL_SEC = float(os.environ.get("MODBUS_POLL_INTERVAL_SEC", "1.0"))


def main() -> None:
    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP.split(","),
        value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode("utf-8"),
    )
    LOG.info(
        "bootstrap=%s topic=%s modbus=%s:%s unit=%s addr=%s count=%s",
        BOOTSTRAP,
        TOPIC,
        MODBUS_HOST,
        MODBUS_PORT,
        UNIT_ID,
        START_ADDR,
        COUNT,
    )

    while True:
        client = ModbusTcpClient(host=MODBUS_HOST, port=MODBUS_PORT)
        try:
            if not client.connect():
                LOG.warning("connect failed; retry in %ss", POLL_SEC)
                time.sleep(POLL_SEC)
                continue
            rr = client.read_holding_registers(START_ADDR, count=COUNT, slave=UNIT_ID)
            if rr.isError():
                LOG.warning("modbus error: %s", rr)
            else:
                regs = list(rr.registers)
                payload = {
                    "source": "modbus_tcp",
                    "host": MODBUS_HOST,
                    "port": MODBUS_PORT,
                    "unit_id": UNIT_ID,
                    "start_address": START_ADDR,
                    "registers": regs,
                }
                producer.send(TOPIC, value=payload)
                LOG.info("sent %s", payload)
                producer.flush()
        finally:
            client.close()
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
