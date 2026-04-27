# CSV demo: S3 handoff → Kafka

This folder contains the pieces for an end-to-end **demo** on **OpenShift** that looks like a simple **file-based integration**: something creates a CSV, drops it in object storage, and a separate service **pulls** that file and **streams** each row to Kafka.

## What we are simulating

1. **A human- or system-like upload to S3**  
   A small container runs **`s3_csv_uploader`**. It does **not** use a person or a laptop. In memory it builds a CSV (by default, fleet-style / telematics-style rows from `fleet_telemetry_data.py`), then calls **`PutObject`** on a fixed bucket and key, on a timer. That is the same outcome as if someone uploaded `measurements.csv` to the same path—except it is automated and repeatable.

2. **Object storage**  
   The file lives in a bucket (for example `simon-kafka-csv-demo`) at a key such as `uploads/sample.csv`. Each time the uploader overwrites the object, its **ETag** changes, which the reader uses to know there is new content.

3. **S3 → Kafka producer**  
   A second container runs **`s3_csv_producer`**. It talks to the same bucket, uses **`GetObject` / `HeadObject`**, parses the CSV (header row + data rows), and publishes **one Kafka record per row** (JSON in the value) to one or more topics. In the cluster demo, the topic is typically **`poc.csv.from_s3`**.

## Flow at a glance

```text
┌─────────────────────┐     PutObject (CSV)      ┌──────────────┐
│  s3-csv-uploader    │ ────────────────────────►│  Amazon S3   │
│  (builds CSV,       │     periodic overwrite    │  bucket/key  │
│   mimics “upload”)  │                            └──────┬───────┘
└─────────────────────┘                                   │
                                                            │ GetObject
                                                            ▼
                                                   ┌────────────────┐
                                                   │ s3-csv-producer│
                                                   │  (stream rows  │
                                                   │   to Kafka)    │
                                                   └────────┬───────┘
                                                            │
                                                            ▼
                                                   ┌────────────────┐
                                                   │ Kafka topic    │
                                                   │ (e.g.          │
                                                   │  poc.csv.from_s3)│
                                                   └────────────────┘
```

## Code and images in this directory

| Role | Script / Dockerfile | Notes |
|------|--------------------|--------|
| Uploader | `s3_csv_uploader.py`, `Dockerfile.s3-csv-uploader` | Env: `S3_BUCKET`, `S3_KEY`, `UPLOAD_INTERVAL_SEC`, `CSV_MODE=fleet`, `BUMP_TIMESTAMPS=1` so each upload is slightly different (helps ETag). |
| S3 → Kafka | `s3_csv_producer.py`, `Dockerfile.s3-csv` | Env: `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_TOPICS` or `KAFKA_TOPIC_CSV`, `S3_BUCKET`, `S3_KEY`, `PROCESS_MODE=loop`, `S3_POLL_INTERVAL_SEC`. |
| Optional file-based CSV | `csv_producer.py`, `Dockerfile.csv` | Reads a CSV path from disk; not part of the S3 pipeline—use only if you mount a file in a pod. |
| Data | `fleet_telemetry_data.py` | Shared rows for the uploader. |

**AWS access** for both S3 clients is through normal credentials (for example a Kubernetes `Secret` with `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` on the cluster), not in this repo.

## Where it runs (OpenShift)

- Build contexts for these images: **`poc/csv`** (see `../../openshift/BuildConfig-s3-csv-uploader.yaml` and `BuildConfig-s3-csv-producer.yaml`).
- Example deployments: `../../openshift/s3-csv-uploader-kafka-demo.yaml` and `s3-csv-producer-kafka-demo.yaml` in namespace `kafka-demo`.
- IAM / Secret setup: `../../aws/SETUP-iam-manual.md`.

For the **full** project (Modbus, root README, license), see [`../../README.md`](../../README.md).
