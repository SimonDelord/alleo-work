#!/usr/bin/env python3
"""
Build a CSV in memory and upload to S3 on an interval (intended to run in OpenShift).
Default: fleet-telemetry-style dataset (see fleet_telemetry_data.py). Set CSV_MODE=random
for a tiny throwaway schema for local testing.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import random
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from fleet_telemetry_data import build_fleet_csv_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("s3_csv_uploader")

BUCKET = os.environ.get("S3_BUCKET", "")
KEY = os.environ.get("S3_KEY", "uploads/sample.csv")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "").strip() or None
INTERVAL = float(os.environ.get("UPLOAD_INTERVAL_SEC", "60"))
CSV_MODE = os.environ.get("CSV_MODE", "fleet").strip().lower()
BUMP_TIMESTAMPS = os.environ.get("BUMP_TIMESTAMPS", "1").strip().lower() in ("1", "true", "yes", "on")
ROW_COUNT = int(os.environ.get("CSV_ROW_COUNT", "5"))
SEED = os.environ.get("RANDOM_SEED", "").strip()
COLUMNS = [c.strip() for c in os.environ.get("CSV_COLUMNS", "device_id,timestamp_c,metric,value").split(",") if c.strip()]


def build_random_csv(rows: int) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    now = datetime.now(timezone.utc)
    for _ in range(rows):
        dev = f"GEN-{random.randint(1, 3)}"
        ts = now.isoformat().replace("+00:00", "Z")
        metric = random.choice(["temp_c", "pressure_bar", "flow_lpm"])
        if "temp" in metric:
            value = round(random.uniform(10.0, 35.0), 2)
        elif "pressure" in metric:
            value = round(random.uniform(0.5, 2.5), 2)
        else:
            value = round(random.uniform(0.0, 100.0), 2)
        w.writerow([dev, ts, metric, value])
    return buf.getvalue().encode("utf-8")


def main() -> None:
    if not BUCKET:
        raise SystemExit("S3_BUCKET is required")
    if SEED and CSV_MODE == "random":
        random.seed(int(SEED))
    client = boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=ENDPOINT,
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )
    cycle = 0
    LOG.info(
        "starting uploader: mode=%s bucket=%s key=%s interval_s=%s bump_timestamps=%s",
        CSV_MODE,
        BUCKET,
        KEY,
        INTERVAL,
        BUMP_TIMESTAMPS,
    )
    while True:
        cycle += 1
        if CSV_MODE == "random":
            body = build_random_csv(ROW_COUNT)
        else:
            body = build_fleet_csv_bytes(bump_timestamps=BUMP_TIMESTAMPS, upload_cycle=cycle)
        client.put_object(
            Bucket=BUCKET,
            Key=KEY,
            Body=body,
            ContentType="text/csv",
        )
        LOG.info(
            "uploaded s3://%s/%s (%s bytes) cycle=%s",
            BUCKET,
            KEY,
            len(body),
            cycle,
        )
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
