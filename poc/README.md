# Proof-of-concept producers (`poc/`)

This folder is split into two **parts** you can treat as separate concerns (or future split into their own repositories): **CSV / S3 / Kafka** vs **Modbus / Kafka**.

## `csv/` — CSV and S3 → Kafka

**End-to-end S3 + Kafka demo (uploader + producer):** see **[`csv/README.md`](csv/README.md)**.

| File | Purpose |
|------|--------|
| `csv_producer.py` | Read a local CSV path, send each row as JSON to Kafka |
| `Dockerfile.csv` | Image for `csv_producer` (Redpanda demo) |
| `sample.csv` | Small demo CSV |
| `s3_csv_producer.py` | Stream an object from S3, parse CSV, produce to one or more topics |
| `Dockerfile.s3-csv` | Image for S3 → Kafka worker |
| `requirements.s3-csv-producer.txt` | `boto3` + `kafka-python-ng` for that image |
| `s3_csv_uploader.py` | Build fleet-style CSV in memory, `put_object` to S3 on an interval |
| `Dockerfile.s3-csv-uploader` | Image for the uploader |
| `requirements.s3-csv-uploader.txt` | `boto3` only |
| `fleet_telemetry_data.py` | Embedded telematics rows + `build_fleet_csv_bytes()` |
| `build_vulcan_sample_csv.py` | Regenerate `sample_vulcan_fleet_telemetry.csv` on disk |
| `sample_vulcan_fleet_telemetry.csv` | Example fleet/telematics extract (static) |

OpenShift build contexts for these images use **`contextDir: poc/csv`** (see `openshift/BuildConfig-*.yaml`).

## `modbus/` — Modbus TCP → Kafka

| File | Purpose |
|------|--------|
| `modbus_producer.py` | Poll Modbus TCP holding registers, JSON payload to Kafka |
| `modbus_sim.py` | Async Modbus TCP simulator (holding registers) for demos |
| `Dockerfile.modbus` | Image for the poller |
| `Dockerfile.modbus-sim` | Image for the simulator |
| `requirements.txt` | `kafka-python` + `pymodbus` |

OpenShift / Compose build context: **`poc/modbus`**.

## Root `poc/README.md`

You are reading it. For end-to-end compose and cluster notes, see the [repository README](../README.md).
