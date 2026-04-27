# Proof-of-concept producers (`poc/`)

This folder is split into two **parts** you can treat as separate concerns (or future split into their own repositories): **CSV / S3 / Kafka** vs **Modbus / Kafka**.

## `csv/` — CSV and S3 → Kafka

**End-to-end S3 + Kafka demo (uploader + producer):** see **[`csv/README.md`](csv/README.md)**.

| File | Purpose |
|------|--------|
| `csv_producer.py` | Read a local CSV path, send each row as JSON to Kafka |
| `Dockerfile.csv` | Image for `csv_producer` (reads a mounted CSV path; optional use) |
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

## `modbus/` — Modbus (pump + arm) ↔ Kafka

**Pipeline (OpenShift `modbus` + Strimzi in `kafka-demo`):**

1. **Pump “PLC”** — `pump_plc_sim.py` — HR0: pump off (0) / on (1); toggles on a timer.  
2. **Pump → Kafka** — `pump_to_kafka.py` — reads HR0, publishes to `modbus.pipeline.pump.status`.  
3. **Kafka → Arm** — `kafka_to_arm_modbus.py` — consumes `modbus.pipeline.arm.commands` (`{"action":"left"|"right"|"stop"}`), writes the **arm** PLC HR0 (0 idle, 1 left, 2 right).  
4. **Arm “PLC”** — `arm_plc_sim.py` — serves that register for reads (and for Modbus writes from the bridge).

| File | Purpose |
|------|--------|
| `pump_plc_sim.py` / `arm_plc_sim.py` | Modbus TCP servers (pump and arm state in HR0) |
| `pump_to_kafka.py` / `kafka_to_arm_modbus.py` | Modbus↔Kafka bridges (`kafka-python-ng` + `pymodbus`) |
| `Dockerfile.pump-plc` / `Dockerfile.arm-plc` | PLC sims (`requirements-sim.txt`) |
| `Dockerfile.pump-to-kafka` / `Dockerfile.kafka-to-arm` | Bridges (`requirements-bridge.txt`) |
| `modbus_producer.py` | Generic Modbus poller → Kafka (flexible) |
| `modbus_sim.py` / `Dockerfile.modbus*` / `requirements.txt` | Generic demo/legacy |
| `openshift/modbus/` | Namespace, `KafkaTopic` manifests, `pipeline.yaml`, BuildConfigs (`contextDir: poc/modbus`) |

## Root `poc/README.md`

You are reading it. For cluster setup and the S3→Kafka story, see the [repository README](../README.md) and [`csv/README.md`](csv/README.md).
