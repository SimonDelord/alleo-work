# Modbus + Kafka demo (`poc/modbus`)

This folder contains a small **OT-style** proof of concept: two **Modbus TCP “PLCs”** (a **pump** and a **robotic arm**), two **Kafka topics**, and **bridge** programs that connect Modbus to **Strimzi Kafka** running on the cluster (namespace `kafka-demo`, cluster `my-cluster`). Workloads and images are deployed in the OpenShift namespace **`modbus`**; see **`../../openshift/modbus/`** for manifests, BuildConfigs, and apply order.

## What the demo does

1. **Pump PLC** (`pump_plc_sim.py`) listens on Modbus TCP and exposes **holding register 0** as pump state: **0 = off**, **1 = on**. A background task toggles that bit on a timer (default 20s) so the line has moving data without manual writes.
2. **Pump → Kafka** (`pump_to_kafka.py`) polls the pump over Modbus and publishes JSON to the topic **`modbus.pipeline.pump.status`** whenever the register value changes (e.g. `{"pump":"on","raw_hr0":1}`).
3. **Arm PLC** (`arm_plc_sim.py`) listens on Modbus and exposes **holding register 0** for the arm: **0 = idle**, **1 = left**, **2 = right**.
4. **Kafka → Arm** (`kafka_to_arm_modbus.py`) consumes **`modbus.pipeline.arm.commands`**, parses JSON such as `{"action":"left"}` / `"right"` / `"stop"`, and **writes** HR0 on the arm Modbus service so the simulated PLC reflects the command.
5. **Arm command producer** (`arm_command_producer.py`, optional demo traffic) publishes the same JSON shape to **`modbus.pipeline.arm.commands`** on a fixed interval (default **10s**), cycling e.g. left → right → stop so you can see the arm bridge and PLC update without a separate client.

Kafka bootstrap used in-cluster is typically:

`my-cluster-kafka-bootstrap.kafka-demo.svc:9092`

(Stronger security such as TLS/SASL is left to your cluster configuration; the PoC uses the internal plain listener where that is allowed.)

## Flow (summary)

```text
[pump PLC :5020] --Modbus--> [pump_to_kafka] --Kafka--> modbus.pipeline.pump.status
                                                                    (observe / metrics)

[arm_command_producer] --Kafka--> modbus.pipeline.arm.commands  (optional demo)
         (any publisher) --Kafka--> modbus.pipeline.arm.commands
                                              |
                                              v
                                    [kafka_to_arm_modbus] --Modbus--> [arm PLC :5020]
```

## Files in this directory

| File | Role |
|------|------|
| **`pump_plc_sim.py`** | Async **Modbus TCP server**: HR0 = pump on/off; toggles on `PUMP_TOGGLE_SEC`. Use with `Dockerfile.pump-plc` and `requirements-sim.txt`. |
| **`arm_plc_sim.py`** | Async **Modbus TCP server**: HR0 = arm idle / left / right; written by `kafka_to_arm_modbus`. `Dockerfile.arm-plc`, `requirements-sim.txt`. |
| **`pump_to_kafka.py`** | **Modbus client** → **Kafka producer** for pump status topic. `Dockerfile.pump-to-kafka`, `requirements-bridge.txt`. |
| **`kafka_to_arm_modbus.py`** | **Kafka consumer** → **Modbus client** writing the arm HR0. `Dockerfile.kafka-to-arm`, `requirements-bridge.txt`. |
| **`arm_command_producer.py`** | **Kafka producer** only: publishes `{"action":"left"}` / `"right"` / `"stop"` on an interval. `Dockerfile.arm-command-producer`, `requirements-kafka-producer.txt`. |
| **`requirements-sim.txt`** | `pymodbus` only — PLC simulator images. |
| **`requirements-bridge.txt`** | `pymodbus` + `kafka-python-ng` — Modbus↔Kafka bridge images. |
| **`requirements-kafka-producer.txt`** | `kafka-python-ng` only — arm command producer image. |
| **`requirements.txt`** | Legacy generic `pymodbus` + Kafka stack for older Dockerfiles. |
| **`Dockerfile.pump-plc`** / **`Dockerfile.arm-plc`** | Images for the two PLCs. |
| **`Dockerfile.pump-to-kafka`** / **`Dockerfile.kafka-to-arm`** | Images for the two bridges. |
| **`Dockerfile.arm-command-producer`** | Image for the periodic arm-command publisher. |
| **`modbus_sim.py`** | Generic Modbus TCP sim (HR 0–3); useful for ad hoc tests. **`Dockerfile.modbus-sim`**. |
| **`modbus_producer.py`** | Generic “poll any holding register range → Kafka” worker; env-driven `MODBUS_*` and `KAFKA_*`. **`Dockerfile.modbus`**. |
| **`README.md`** | This file. |

## Environment variables (high level)

- **Bridges / producer:** `KAFKA_BOOTSTRAP_SERVERS` (or `KAFKA_BOOTSTRAP`), `KAFKA_TOPIC_PUMP`, `KAFKA_TOPIC_ARM_COMMANDS`, `PUMP_MODBUS`, `ARM_MODBUS` (host:port), `POLL_SEC`, `KAFKA_CONSUMER_GROUP` (arm consumer), `ARM_COMMAND_INTERVAL_SEC`, `ARM_COMMAND_SEQUENCE` (producer).
- **PLCs:** `MODBUS_HOST`, `MODBUS_PORT`, `PUMP_TOGGLE_SEC` (pump).

Exact defaults match the **`modbus-pipeline-env`** ConfigMap in **`openshift/modbus/pipeline.yaml`**.

## OpenShift

- **`../../openshift/modbus/namespace.yaml`** — create the `modbus` namespace.  
- **`../../openshift/modbus/kafka-topics-kafka-demo.yaml`** — Strimzi `KafkaTopic` objects in **`kafka-demo`**.  
- **`../../openshift/modbus/buildconfigs.yaml`** — in-cluster builds (`contextDir: poc/modbus`, one Dockerfile per image).  
- **`../../openshift/modbus/pipeline.yaml`** — ConfigMap, Services, Deployments (pump PLC, arm PLC, bridges, arm command producer).

Apply order and one-line commands are in the comment header at the top of **`pipeline.yaml`**.

## Also see

- **[`../README.md`](../README.md)** — index of `poc/csv` vs `poc/modbus`.  
- **[`../../README.md`](../../README.md)** — repository-wide OpenShift and S3 CSV notes.
