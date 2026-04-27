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

Flow: you put a CSV object in **S3** (or any **S3-compatible** bucket, for example Ceph/RGW or MinIO). The worker **`poc/csv/s3_csv_producer.py`** streams that object, parses rows with a header line, and publishes **each row as JSON** to **one or more** Kafka topics.

- **Topics:** set `KAFKA_TOPICS` to a comma-separated list (for example `ingest.raw,ingest.archive`). If unset, it uses `KAFKA_TOPIC_CSV` (default `poc.csv.rows`).
- **Object location:** either `S3_URI=s3://bucket/path/file.csv` or `S3_BUCKET` + `S3_KEY`.
- **Credentials / region:** standard AWS env vars (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` if needed, `AWS_DEFAULT_REGION`). For non-AWS endpoints, set `AWS_ENDPOINT_URL`.
- **Runs:** `PROCESS_MODE=once` exits after one successful pass (good for a Kubernetes `Job`). `PROCESS_MODE=loop` (default) polls on an interval and uses **ETag** from `head_object` so unchanged objects are skipped and **re-uploads** are picked up.
- **Local demo:** `docker compose --profile s3 up --build` starts [LocalStack](https://github.com/localstack/localstack) S3, seeds `sample.csv` into `s3://alleo-poc/uploads/sample.csv`, and runs the producer against Redpanda.

For a fully event-driven pipeline on AWS (object created → process exactly once), the usual pattern is **S3 event notification → SQS** (or SNS → Lambda) carrying the bucket/key; this PoC uses **poll + ETag** so it stays simple and works the same on ROSA with an S3-compatible store.

## Layout

| Path | Purpose |
|------|---------|
| [`poc/README.md`](poc/README.md) | **Index of `poc/csv` vs `poc/modbus` subfolders** |
| `poc/csv/` | CSV file + S3 → Kafka uploader/producer code and Dockerfiles |
| `poc/modbus/` | Modbus TCP simulator + poller → Kafka |
| `openshift/BuildConfig-s3-csv-*.yaml` | OpenShift builds (`contextDir: poc/csv`) |
| `docker-compose.yml` | Redpanda + Modbus (`poc/modbus`) + CSV (`poc/csv`); profile `s3` adds LocalStack + S3 worker |
| `openshift/` | Example manifests for cluster deploy (fill in broker URL and images) |

## OpenShift / ROSA

Do not commit API tokens or kubeconfigs. Set `KAFKA_BOOTSTRAP_SERVERS` (and TLS/SASL per your broker) on the producers; adjust `openshift/kafka-bridge-example.yaml` for your registry and Kafka endpoint.

**Kafka demo on cluster:** namespace `kafka-demo`, bucket `arn:aws:s3:::simon-kafka-csv-demo`. Apply `openshift/BuildConfig-s3-csv-uploader.yaml` and run `oc start-build s3-csv-uploader -n kafka-demo` so the **CSV uploader image is built on the cluster** (no laptop). Then apply `openshift/s3-csv-uploader-kafka-demo.yaml` (upload) and `openshift/s3-csv-producer-kafka-demo.yaml` (S3 → Kafka). Create `Secret` `aws-s3-csv-creds` as in `aws/SETUP-iam-manual.md`. Use the same `S3_KEY` in both; `BUMP_TIMESTAMPS=1` on the uploader changes file bytes each cycle so S3 ETags update and the reader re-processes.

## License

No license file yet; add one if you open-source this beyond personal use.
