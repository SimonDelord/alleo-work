#!/usr/bin/env python3
"""Read a CSV file and produce each row as JSON to Kafka."""

import csv
import json
import logging
import os
import sys
import time

from kafka import KafkaProducer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("csv_producer")

TOPIC = os.environ.get("KAFKA_TOPIC_CSV", "poc.csv.rows")
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
CSV_PATH = os.environ.get("CSV_PATH", "/data/sample.csv")
INTERVAL_SEC = float(os.environ.get("CSV_SEND_INTERVAL_SEC", "2"))


def main() -> None:
    if not os.path.isfile(CSV_PATH):
        LOG.error("CSV file not found: %s", CSV_PATH)
        sys.exit(1)

    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP.split(","),
        value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k is not None else None,
    )

    LOG.info("bootstrap=%s topic=%s csv=%s", BOOTSTRAP, TOPIC, CSV_PATH)

    while True:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = row.get("device_id") or row.get(list(row.keys())[0])
                producer.send(TOPIC, key=key, value=row)
                LOG.info("sent key=%s %s", key, row)
                time.sleep(INTERVAL_SEC)
        producer.flush()
        LOG.info("finished pass over %s; restarting loop", CSV_PATH)


if __name__ == "__main__":
    main()
