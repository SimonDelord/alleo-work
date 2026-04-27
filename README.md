# alleo-work

Small experiments around **Kafka** with **CSV** ingestion and **Modbus TCP** (OT / PLC style) producers.

Repository: [github.com/SimonDelord/alleo-work](https://github.com/SimonDelord/alleo-work)

## Quick start (local)

Requires [Docker](https://docs.docker.com/get-docker/) with Compose v2.

```bash
git clone https://github.com/SimonDelord/alleo-work.git
cd alleo-work
docker compose up --build
```

Kafka API on **localhost:9092**; example topics `poc.csv.rows` and `poc.modbus.readings`. Modbus simulator on **localhost:5020**.

```bash
docker compose exec redpanda rpk topic consume poc.csv.rows poc.modbus.readings --brokers localhost:9092
```

## S3 → Kafka (CSV)

Flow: you put a CSV object in **S3** (or any **S3-compatible** bucket, for example Ceph/RGW or MinIO). The worker **`poc/s3_csv_producer.py`** streams that object, parses rows with a header line, and publishes **each row as JSON** to **one or more** Kafka topics.

- **Topics:** set `KAFKA_TOPICS` to a comma-separated list (for example `ingest.raw,ingest.archive`). If unset, it uses `KAFKA_TOPIC_CSV` (default `poc.csv.rows`).
- **Object location:** either `S3_URI=s3://bucket/path/file.csv` or `S3_BUCKET` + `S3_KEY`.
- **Credentials / region:** standard AWS env vars (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` if needed, `AWS_DEFAULT_REGION`). For non-AWS endpoints, set `AWS_ENDPOINT_URL`.
- **Runs:** `PROCESS_MODE=once` exits after one successful pass (good for a Kubernetes `Job`). `PROCESS_MODE=loop` (default) polls on an interval and uses **ETag** from `head_object` so unchanged objects are skipped and **re-uploads** are picked up.
- **Local demo:** `docker compose --profile s3 up --build` starts [LocalStack](https://github.com/localstack/localstack) S3, seeds `sample.csv` into `s3://alleo-poc/uploads/sample.csv`, and runs the producer against Redpanda.

For a fully event-driven pipeline on AWS (object created → process exactly once), the usual pattern is **S3 event notification → SQS** (or SNS → Lambda) carrying the bucket/key; this PoC uses **poll + ETag** so it stays simple and works the same on ROSA with an S3-compatible store.

## Layout

| Path | Purpose |
|------|---------|
| `poc/` | Python producers, Dockerfiles, sample CSV |
| `poc/s3_csv_producer.py` | S3 object → Kafka (multi-topic) |
| `poc/s3_csv_uploader.py` | Random CSV in memory → `put_object` (mimics manual upload) |
| `docker-compose.yml` | Redpanda + Modbus sim + producers; profile `s3` adds LocalStack + S3 worker |
| `openshift/` | Example manifests for cluster deploy (fill in broker URL and images) |

## OpenShift / ROSA

Do not commit API tokens or kubeconfigs. Set `KAFKA_BOOTSTRAP_SERVERS` (and TLS/SASL per your broker) on the producers; adjust `openshift/kafka-bridge-example.yaml` for your registry and Kafka endpoint.

**Kafka demo on cluster:** for namespace `kafka-demo` with Strimzi `my-cluster` and S3 bucket `arn:aws:s3:::simon-kafka-csv-demo`, see `openshift/s3-csv-producer-kafka-demo.yaml` (S3 → Kafka) and `openshift/s3-csv-uploader-kafka-demo.yaml` (synthetic CSV upload on an interval). Use the same `S3_KEY` in both so each upload overwrites the object, the reader sees a new ETag, and rows are produced again. Build/push `Dockerfile.s3-csv` and `Dockerfile.s3-csv-uploader`, wire AWS credentials, then set images on the Deployments.

## License

No license file yet; add one if you open-source this beyond personal use.
