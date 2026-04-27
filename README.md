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

## Layout

| Path | Purpose |
|------|---------|
| `poc/` | Python producers, Dockerfiles, sample CSV |
| `docker-compose.yml` | Redpanda + Modbus sim + producers |
| `openshift/` | Example manifests for cluster deploy (fill in broker URL and images) |

## OpenShift / ROSA

Do not commit API tokens or kubeconfigs. Set `KAFKA_BOOTSTRAP_SERVERS` (and TLS/SASL per your broker) on the producers; adjust `openshift/kafka-bridge-example.yaml` for your registry and Kafka endpoint.

## License

No license file yet; add one if you open-source this beyond personal use.
