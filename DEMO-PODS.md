# Mining fleet demo — pod and workload reference

Quick index of **Deployments**, **StatefulSets**, and **KafkaConnectors** used in the mining fleet demo on OpenShift. Listed by namespace; pod names follow the usual `{workload}-{replicaset-hash}-{pod-id}` pattern (Kafka brokers use `{cluster}-{pool}-{ordinal}`).

**Not included:** OpenShift `BuildConfig` build pods (image builds only), the separate `kafka-demo` PoC stack, or the planned `water-spray-fleet` namespace (not deployed in this demo).

Manifests: [`openshift/`](openshift/) · Design docs: [`docs/mining-fleet/`](docs/mining-fleet/)

---

## `truck-fleet`

Mobile haul fleet — MQTT telemetry, PostgreSQL ingest.

| Workload | What it does |
|----------|--------------|
| `mqtt-broker` | Eclipse Mosquitto broker that relays truck telemetry and destination commands between agents and downstream services. |
| `mqtt-ingest` | Subscribes to `fleet/trucks/+/telemetry` and writes normalized truck rows into PostgreSQL. |
| `postgresql` | PostgreSQL database storing truck telemetry history and the latest snapshot per truck. |
| `truck-tr1` | Simulated haul truck TR1 that cycles through load/haul/dump/return and publishes MQTT telemetry. |
| `truck-tr2` | Simulated haul truck TR2 that cycles through load/haul/dump/return and publishes MQTT telemetry. |
| `truck-tr3` | Simulated haul truck TR3 that cycles through load/haul/dump/return and publishes MQTT telemetry. |

---

## `crusher-fleet`

Fixed plant — Modbus crusher PLCs and plant historian.

| Workload | What it does |
|----------|--------------|
| `crusher-1` | Modbus TCP PLC simulator for crusher bay north exposing fill level, status, and dump-count registers. |
| `crusher-2` | Modbus TCP PLC simulator for crusher bay south exposing fill level, status, and dump-count registers. |
| `historian` | Polls crusher Modbus registers every few seconds and persists time-series samples and latest state to PostgreSQL. |
| `postgresql` | PostgreSQL database storing crusher telemetry history and current fill/status per crusher. |

---

## `fleet-integration`

Kafka orchestration layer — bridges MQTT, Modbus, and routing logic.

| Workload | What it does |
|----------|--------------|
| `kafka-truck-bridge` | Mirrors truck MQTT telemetry into the `fleet.trucks.telemetry` Kafka topic for downstream consumers. |
| `crusher-fill-bridge` | Detects truck dump events from MQTT, writes fill to crusher Modbus registers, and publishes `fleet.crushers.state`. |
| `destination-router` | Consumes truck telemetry and crusher state from Kafka and emits reroute or stop/resume commands when bays are at capacity. |
| `mqtt-routing-bridge` | Consumes Kafka routing and truck commands and publishes retained MQTT destination updates and stop/resume messages to trucks. |
| `crusher-state-producer` | Deprecated mock crusher-state publisher (replicas 0; replaced by `crusher-fill-bridge`). |

---

## `fleet-live-map`

Kafka-backed operator dashboard.

| Workload | What it does |
|----------|--------------|
| `fleet-live-map` | Web dashboard that consumes fleet Kafka topics and renders a live map with trucks, crusher fill, and routing exceptions. |

---

## `mining-fleet-kafka`

Dedicated AMQ Streams / Strimzi stack for the mining fleet demo (separate from `kafka-demo`).

| Workload | What it does |
|----------|--------------|
| `mining-fleet-cluster-mining-fleet-pool` | Three-node KRaft Kafka broker/controller pool (pods `…-pool-0`, `…-pool-1`, `…-pool-2`) hosting all `fleet.*` topics. |
| `mining-fleet-cluster-entity-operator` | Strimzi entity operator that reconciles KafkaTopic and KafkaUser resources for `mining-fleet-cluster`. |
| `mining-fleet-cluster-kafka-exporter` | Prometheus metrics exporter exposing broker, topic, and consumer-group statistics for the cluster. |
| `fleet-cdc-connect` | Kafka Connect worker (StatefulSet; pod `fleet-cdc-connect-connect-0`) running Debezium for PostgreSQL CDC. |
| `truck-postgres-source` | KafkaConnector (Debezium) that streams row-level changes from `truck-fleet` PostgreSQL into Kafka. |
| `crusher-postgres-source` | KafkaConnector (Debezium) that streams row-level changes from `crusher-fleet` PostgreSQL into Kafka. |
| `mining-fleet-console-console-deployment` | Streamshub Kafka Console UI for browsing topics, consumer groups, and messages in the dedicated cluster. |
| `mining-fleet-console-prometheus-deployment` | Prometheus instance backing metrics and health views inside the Streamshub Kafka Console. |

---

## Verify on cluster

```bash
for ns in truck-fleet crusher-fleet fleet-integration fleet-live-map mining-fleet-kafka; do
  echo "=== $ns ==="
  oc get deploy,sts -n "$ns"
done
oc get kafkaconnector -n mining-fleet-kafka
```
