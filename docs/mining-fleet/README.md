# Mining fleet demo — three plant systems

This document describes a **Fleet Management System** demonstration built on **OpenShift / ROSA**. The demo models a small open-pit mine as **three independent operational systems**, each running in its own Kubernetes namespace as a **closed ecosystem**. A shared **Apache Kafka** cluster (Strimzi) connects them in a second phase.

The goal is a credible story for stakeholders:

- **Mobile fleet** (haul trucks) — wireless-style telemetry and operational state.
- **Fixed plant** (crushers) — wired automation, local historian, batch export to object storage.
- **Environmental / dust control** (water sprays) — field PLCs commanded by site events.

Each system can be developed, deployed, and demonstrated **on its own**. Integration is explicit and crosses only well-defined boundaries (Kafka topics, S3 objects, or optional bridges into a fleet database).

---

## At a glance

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Phase 1 — stand-alone ecosystems                     │
├─────────────────┬─────────────────────────┬─────────────────────────────────┤
│  truck-fleet    │  crusher-fleet          │  water-spray-fleet              │
│  MQTT + Postgres│  Modbus + historian + S3│  Modbus PLCs (north / south)    │
└────────┬────────┴────────────┬────────────┴──────────────┬──────────────────┘
         │                     │                           │
         │         Phase 2 — Kafka (Strimzi) integration    │
         └─────────────────────┼───────────────────────────┘
                               ▼
                    ┌──────────────────────┐
                    │  Kafka cluster       │
                    │  (e.g. kafka-demo)   │
                    └──────────────────────┘
         CDC (Postgres) │ S3 API / export │ Modbus bridges
```

| System | Namespace (planned) | Field protocol | In-namespace data store | Primary northbound handoff |
|--------|---------------------|----------------|-------------------------|----------------------------|
| **Haul trucks** | `truck-fleet` | MQTT (pub/sub per truck) | PostgreSQL | Operational UI, assignment, metrics |
| **Crushers** | `crusher-fleet` | Modbus TCP (per crusher PLC) | Plant historian (time-series DB) | CSV files in **AWS S3** |
| **Water sprays** | `water-spray-fleet` | Modbus TCP (north + south PLC pods) | PLC registers (state on device) | Commands via Modbus (from Kafka in phase 2) |

---

## Design principles

1. **Closed namespaces** — Each ecosystem contains its own workloads, services, and persistence. Other systems do not reach into its Postgres or Modbus ports directly.
2. **Industry-shaped protocols** — Trucks use **MQTT** (common for mobile / edge). Crushers and sprays use **Modbus TCP** (common for fixed plant PLCs). PLCs do not write to PostgreSQL; integration services do.
3. **Different speeds** — Trucks update often (position, route). Crushers change slowly (fill level, full/empty). Sprays are actuators (on/off) driven by **events**, not high-frequency telemetry.
4. **Kafka as the site bus (phase 2)** — After stand-alone demos work, Kafka carries **domain events** and **commands** between ecosystems. Crushers may still archive to S3 in parallel; that is not the real-time path for sprays.

---

## 1. Haul trucks (`truck-fleet`)

### Role in the mine

Haul trucks move material between the **loading area** and **crusher bays** (north and south). The demo script typically runs a small fleet (e.g. three trucks) with sequential or coordinated dumping so operators can see routing, bay fill, and exceptions on a live map.

### Architecture (stand-alone)

```text
┌──────────────┐     MQTT publish      ┌─────────────┐
│  truck-TR1   │ ────────────────────► │ MQTT broker │
│  (Pod)       │     telemetry         │ (in ns)     │
├──────────────┤                       └──────┬──────┘
│  truck-TR2   │                              │
│  truck-TR3   │                              ▼
└──────────────┘                       ┌─────────────┐
       ▲                               │ mqtt-ingest │
       │ MQTT subscribe                │  service    │
       │ (reroute, stop, assign)       └──────┬──────┘
       │                                      │ SQL
       └──────────────────────────────────────┤
                                              ▼
                                       ┌─────────────┐
                                       │ PostgreSQL  │
                                       │ (in ns)     │
                                       └──────┬──────┘
                                              │
                                              ▼
                                       Live map / Grafana
                                       (reads DB, not MQTT)
```

### Behaviour

- **One Pod per truck** (or per truck agent) publishes telemetry: position, speed, payload state, current assignment, status.
- Trucks **subscribe** to command topics (reroute, stop, new destination) issued by assignment logic or a demo orchestrator.
- **`mqtt-ingest`** consumes MQTT messages and **writes normalized rows** into PostgreSQL. The database is the **system of record** for the fleet UI.
- **PostgreSQL does not subscribe to MQTT**; only the ingest service bridges the bus to SQL.

### Typical data in Postgres

- Truck state (location, payload, status, assigned route)
- Route and assignment tables
- Position history / trails for the live map
- Crusher bay summary tables (e.g. `overload_bays` fill %) when a bridge or orchestrator updates them for a unified view

### Stand-alone demo modes

| Mode | Description |
|------|-------------|
| **Scripted orchestrator** | A single service writes Postgres directly to tell a deterministic story (fastest for UI demos). |
| **MQTT agents** | Truck Pods publish/subscribe on MQTT; ingest fills Postgres — closer to production edge architecture. |

### Namespace boundary

Everything required to run trucks (broker, ingest, DB, truck deployments) lives in **`truck-fleet`**. External consumers should use **SQL APIs**, HTTP gateways, or **Kafka** (phase 2), not raw MQTT from outside the namespace.

---

## 2. Crushers (`crusher-fleet`)

### Role in the mine

**Crusher North** and **Crusher South** are fixed plant assets. They accept truck dumps, track fill level, and signal full / accepting / fault states. In the demo narrative, when one bay fills, trucks are re-routed to the other bay.

### Architecture (stand-alone)

```text
┌────────────────────┐         ┌────────────────────┐
│ crusher-north-plc  │         │ crusher-south-plc  │
│   (Pod, Modbus)    │         │   (Pod, Modbus)    │
└─────────┬──────────┘         └─────────┬──────────┘
          │ Modbus poll                  │ Modbus poll
          └──────────────┬───────────────┘
                         ▼
                 ┌───────────────┐
                 │ plant-collector│  reads registers, detects changes
                 └───────┬───────┘
                         │ SQL insert
                         ▼
                 ┌───────────────┐
                 │ plant-historian│  time-series (TimescaleDB or Postgres)
                 └───────┬───────┘
                         │ periodic export
                         ▼
                 ┌───────────────┐
                 │ historian-export│  builds CSV for a time window
                 └───────┬───────┘
                         │ PutObject
                         ▼
                 ┌───────────────┐
                 │   AWS S3      │  s3://…/crusher-fleet/exports/…
                 └───────────────┘
```

### Behaviour

- Each crusher is a **Modbus TCP server** (PLC simulator Pod) exposing holding registers, for example:
  - Fill percentage
  - Status (empty / accepting / full / fault)
  - Dump count, ready flags
- **`plant-collector`** polls Modbus on an interval (e.g. 1–5 s), writes samples to the **historian**, and can emit **change events** when registers cross thresholds (for Kafka in phase 2).
- The **historian** stores high-volume time-series **inside the namespace**. This mimics a site historian (AVEVA PI–style) without claiming a specific vendor product.
- **`historian-export`** rolls up recent samples into a **CSV file** and uploads to **S3** on a schedule. This mimics batch reporting, compliance exports, and offline analysis — not sub-second fleet control.

### Namespace boundary

Modbus and historian access stay inside **`crusher-fleet`**. Other systems learn crusher state via **S3 objects**, **Kafka events** (phase 2), or a thin **bridge** that copies latest fill % into truck-fleet Postgres for a unified map.

---

## 3. Water sprays (`water-spray-fleet`)

### Role in the mine

**Water spray North** and **Water spray South** are dust-suppression zones. They are **actuators**: typically **off** until site logic decides to turn them **on** (e.g. truck on a haul road, material dumped at a crusher, crusher full).

### Architecture (stand-alone)

```text
┌─────────────────────────┐     ┌─────────────────────────┐
│ water-spray-north-plc   │     │ water-spray-south-plc   │
│   (Pod, Modbus server)  │     │   (Pod, Modbus server)  │
└───────────▲─────────────┘     └───────────▲─────────────┘
            │ Modbus write                  │ Modbus write
            │ (on/off, auto)                │
     ┌──────┴───────────────────────────────┴──────┐
     │  kafka-to-spray-modbus  (phase 2)           │
     │  or manual / test Modbus client (phase 1)   │
     └─────────────────────────────────────────────┘
```

### Behaviour

- Each spray is a **Pod representing a field PLC** — it **only speaks Modbus** (holding registers for spray on/off, pressure/flow, fault).
- In **phase 1**, you can prove the namespace by writing registers with a test client or a small simulator.
- In **phase 2**, a **Kafka → Modbus bridge** (same pattern as `poc/modbus/kafka_to_arm_modbus.py`) consumes **`fleet.sprays.commands`** and writes the appropriate PLC.
- A **`spray-controller`** (optional Pod) subscribes to **truck** and **crusher** Kafka topics, applies rules (which zone, how long, debounce), and publishes spray commands. The PLCs themselves remain dumb devices.

### Example control rules (phase 2)

| Event source | Example event | Spray action |
|--------------|---------------|--------------|
| `fleet.crushers.events` | `dump_received` @ North | North spray **ON** for N seconds |
| `fleet.crushers.events` | `crusher_full` @ North | North spray **ON** until accepting again |
| `fleet.trucks.events` | `entered_zone` = north haul road | North spray **ON** |
| `fleet.trucks.events` | `left_zone` | North spray **OFF** (or timed off) |

South spray follows the same pattern with south crusher / south zone events.

### Namespace boundary

**`water-spray-fleet`** contains only spray PLCs and spray-specific bridges/controllers. It does **not** host the MQTT broker or crusher historian. Triggers arrive via **Kafka** (or manual Modbus during bench testing).

---

## Phase 2 — Kafka integration (overview)

Once each ecosystem runs stand-alone, deploy a **Strimzi Kafka** cluster (this repository already includes examples under `openshift/` and `poc/modbus/` for topic wiring). Integration uses **three complementary patterns**:

```text
                    ┌──────────────── Kafka ────────────────┐
                    │                                       │
  truck-fleet       │   CDC ◄── Debezium / connect          │
  PostgreSQL ───────┼──► fleet.trucks.*                     │
                    │                                       │
  crusher-fleet     │   Modbus collector ──► fleet.crushers.*│
  (PLC + historian) │   S3 export (parallel, batch)         │
        │           │        ▲                              │
        │           │        │ S3 API / poller (optional)   │
        └───────────┼────────┘ fleet.crushers.snapshots      │
                    │                                       │
  water-spray-fleet │   fleet.sprays.commands ──► Modbus    │
  (north/south PLC) │   fleet.sprays.status ◄── Modbus poll │
                    └───────────────────────────────────────┘
```

### Integration mechanisms

| Mechanism | Source | What it carries | Typical use |
|-----------|--------|-----------------|-------------|
| **CDC** | `truck-fleet` PostgreSQL | Row changes on trucks, routes, assignments | Downstream analytics, spray rules, audit |
| **S3 API** | `crusher-fleet` CSV objects | Historian export files | Batch ingest, replay, external reporting; optional Kafka producer on new object |
| **Modbus bridges** | Crusher & spray PLCs | Live register reads / writes | `collector → Kafka` (telemetry/events); `Kafka → Modbus` (spray commands) |

### Suggested Kafka topic prefix

Use a single prefix for clarity, e.g. `fleet.*`:

| Topic | Direction | Content |
|-------|-----------|---------|
| `fleet.trucks.telemetry` | Produce from mqtt-ingest | Position, payload, status |
| `fleet.trucks.events` | Produce from trucks / ingest | `entered_zone`, `dump_started`, `rerouted`, … |
| `fleet.crushers.state` | Produce from plant-collector | Periodic fill %, status |
| `fleet.crushers.events` | Produce from plant-collector | `crusher_full`, `dump_received`, `fault`, … |
| `fleet.sprays.commands` | Consume by kafka-to-spray-modbus | `{ zone: north\|south, action: on\|off, reason }` |
| `fleet.sprays.status` | Produce from Modbus poll | Spray on, pressure, fault |

Partition keys: `truck_id`, `crusher_id`, `zone_id` where ordering matters.

### Unified operator view

The **live map** and Grafana dashboards can continue to read **truck-fleet PostgreSQL**. Crusher bays on that UI are updated by:

- a **bridge** consuming Kafka or S3/latest snapshot, or
- CDC + stream processor writing `overload_bays`,

so operators still have one screen while backends stay decoupled.

---

## Repository map (this repo)

These paths support the patterns above; the full fleet demo workloads may live in a separate application repository.

| Path | Relevance |
|------|-----------|
| [`poc/modbus/README.md`](../../poc/modbus/README.md) | Modbus PLC sims; **Modbus → Kafka** and **Kafka → Modbus** bridges |
| [`poc/csv/README.md`](../../poc/csv/README.md) | **S3 CSV upload** and **S3 → Kafka** (crusher export handoff pattern) |
| [`openshift/modbus/`](../../openshift/modbus/) | Example namespace, BuildConfigs, Kafka topics |
| [`openshift/`](../../openshift/) | S3 CSV uploader/producer deployments, Kafka demo namespace |

---

## Deployment order (recommended)

1. **Kafka** — Strimzi cluster, topics `fleet.*`.
2. **`truck-fleet`** — Postgres, MQTT, mqtt-ingest, truck Pods; live map against Postgres.
3. **`crusher-fleet`** — North/South PLC Pods, collector, historian, S3 export.
4. **`water-spray-fleet`** — North/South spray PLC Pods; then Kafka → Modbus bridge + spray-controller.
5. **Integration** — CDC from truck Postgres; crusher collector → Kafka; optional S3-triggered consumer; wire spray rules to truck + crusher events.

---

## Glossary

| Term | Meaning in this demo |
|------|----------------------|
| **PLC Pod** | Container running a Modbus TCP server that mimics field registers |
| **Plant collector** | Service that polls Modbus and forwards data to historian / Kafka |
| **Historian** | Time-series store for crusher samples |
| **Closed ecosystem** | Namespace-scoped stack with a single intentional exit (DB, S3, or Kafka) |
| **CDC** | Change Data Capture from PostgreSQL binlog (e.g. Debezium) to Kafka |

---

## Next documents

- **Phase 2 runbook** — Strimzi install, topic manifests, Debezium connector, S3 poller, Modbus bridge env vars (to be added).
- **OpenShift manifests** — per-namespace `Deployment` / `Service` / `PVC` / `Route` (to be added under `openshift/`).

For questions or extensions (OPC UA gateway, Metrics-style cloud export), keep crushers on **Modbus + historian + S3** and trucks on **MQTT + Postgres**; use Kafka only for **coordination** and **spray control**, not as a replacement for the historian archive.
