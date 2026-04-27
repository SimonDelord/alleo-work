#!/usr/bin/env python3
"""Stream a CSV object from S3 (or S3-compatible API) and produce each row to Kafka topic(s)."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import sys
import time
from typing import Iterable
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from kafka import KafkaProducer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("s3_csv_producer")

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
# Comma-separated list; if unset, fall back to single topic name
TOPICS_ENV = os.environ.get("KAFKA_TOPICS", "").strip()
FALLBACK_TOPIC = os.environ.get("KAFKA_TOPIC_CSV", "poc.csv.rows")

S3_URI = os.environ.get("S3_URI", "").strip()
S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
S3_KEY = os.environ.get("S3_KEY", "").strip()

AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "").strip() or None

POLL_INTERVAL_SEC = float(os.environ.get("S3_POLL_INTERVAL_SEC", "30"))
ROW_SEND_DELAY_SEC = float(os.environ.get("CSV_ROW_SEND_DELAY_SEC", "0"))
# once | loop — loop uses ETag on head_object to detect new uploads
PROCESS_MODE = os.environ.get("PROCESS_MODE", "loop").strip().lower()


def parse_topics() -> list[str]:
    if TOPICS_ENV:
        topics = [t.strip() for t in TOPICS_ENV.split(",") if t.strip()]
        if topics:
            return topics
    return [FALLBACK_TOPIC]


def parse_bucket_key() -> tuple[str, str]:
    if S3_URI:
        parsed = urlparse(S3_URI)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
            LOG.error("S3_URI must look like s3://bucket/path/to/file.csv (got %s)", S3_URI)
            sys.exit(1)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        return bucket, key
    if S3_BUCKET and S3_KEY:
        return S3_BUCKET, S3_KEY
    LOG.error("Set S3_URI or both S3_BUCKET and S3_KEY.")
    sys.exit(1)


def s3_client():
    return boto3.client(
        "s3",
        region_name=AWS_REGION,
        endpoint_url=ENDPOINT_URL,
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


def stream_csv_rows(body_stream) -> Iterable[dict[str, str]]:
    text = io.TextIOWrapper(body_stream, encoding="utf-8", newline="")
    reader = csv.DictReader(text)
    yield from reader


def process_object(s3, bucket: str, key: str, producer: KafkaProducer, topics: list[str]) -> None:
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        for row in stream_csv_rows(body):
            key_field = row.get("device_id") or (row.get(list(row.keys())[0]) if row else None)
            payload = json.dumps(row, separators=(",", ":")).encode("utf-8")
            for topic in topics:
                producer.send(topic, key=key_field.encode("utf-8") if key_field else None, value=payload)
            if ROW_SEND_DELAY_SEC > 0:
                time.sleep(ROW_SEND_DELAY_SEC)
            LOG.info("topics=%s key=%s row=%s", topics, key_field, row)
        producer.flush()
    finally:
        body.close()


def main() -> None:
    bucket, key = parse_bucket_key()
    topics = parse_topics()
    LOG.info(
        "mode=%s bucket=%s key=%s topics=%s bootstrap=%s endpoint=%s",
        PROCESS_MODE,
        bucket,
        key,
        topics,
        BOOTSTRAP,
        ENDPOINT_URL or "(default AWS)",
    )

    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP.split(","),
        value_serializer=lambda b: b,
        key_serializer=lambda k: k,
    )
    s3 = s3_client()
    last_etag: str | None = None

    while True:
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                LOG.warning("object not found yet: s3://%s/%s — retry in %ss", bucket, key, POLL_INTERVAL_SEC)
                time.sleep(POLL_INTERVAL_SEC)
                continue
            raise

        etag = (head.get("ETag") or "").strip('"')
        if etag and etag == last_etag:
            LOG.info(
                "unchanged s3://%s/%s ETag=%s — next check in %ss",
                bucket,
                key,
                etag,
                POLL_INTERVAL_SEC,
            )
            time.sleep(POLL_INTERVAL_SEC)
            continue

        LOG.info("processing s3://%s/%s ETag=%s", bucket, key, etag or "?")
        process_object(s3, bucket, key, producer, topics)
        last_etag = etag or last_etag
        LOG.info("finished processing s3://%s/%s", bucket, key)

        if PROCESS_MODE == "once":
            break
        time.sleep(POLL_INTERVAL_SEC)

    LOG.info("done (once mode)")


if __name__ == "__main__":
    main()
