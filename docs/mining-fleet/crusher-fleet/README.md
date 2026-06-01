# Crusher fleet (`crusher-fleet`)

Fixed plant demo — two crusher bays simulated as **Modbus TCP PLCs**, polled by a **plant historian** that writes time-series and latest state to **PostgreSQL** inside the `crusher-fleet` namespace.

This system is **fully independent** from `truck-fleet` and `fleet-integration`:

- No MQTT
- No Kafka (crushers do not publish to topics)
- No modifications to truck or orchestration code

Phase 2 integration replaces the demo `crusher-state-producer` with **`crusher-fill-bridge`**, which writes crusher Modbus registers when trucks dump and publishes live state to `fleet.crushers.state`.

---

## Architecture

```text
truck-fleet                         fleet-integration                    crusher-fleet
  truck dumps load                    crusher-fill-bridge                  crusher PLCs
  → MQTT telemetry        → Kafka     consumes fleet.trucks.telemetry    (Modbus TCP)
  (state=dumping,                     detects dump at crusher-X            HR0=fill_pct
   load_pct decreasing,               writes Modbus registers              HR4=dump_count
   destination_crusher)               publishes fleet.crushers.state              ↓
                                                                               historian polls
                                                                                    ↓
                                                                              PostgreSQL
                                                                              crusher_state
```

Within `crusher-fleet` namespace only:

```text
┌────────────────────┐         ┌────────────────────┐
│   crusher-1-plc    │         │   crusher-2-plc    │
│  (Pod, Modbus:502) │         │  (Pod, Modbus:502) │
└─────────┬──────────┘         └─────────┬──────────┘
          │ Modbus poll (2s)             │ Modbus poll
          └──────────────┬───────────────┘
                         ▼
                 ┌───────────────┐
                 │   historian   │  polls HR0..HR5, writes SQL
                 └───────┬───────┘
                         │
                         ▼
                 ┌───────────────┐
                 │  PostgreSQL   │  crusher_telemetry + crusher_state
                 └───────────────┘
```

Fill changes originate in `fleet-integration` (`crusher-fill-bridge`), not inside crusher PLCs.

Compared to the original mining-fleet design (historian → S3 CSV export), this implementation stores data **directly in PostgreSQL** per demo requirements. S3 batch export can be added later as an optional sidecar.

---

## Modbus register map

Each crusher PLC exposes **holding registers** starting at address 0 (Modbus function code 03). The historian reads six consecutive registers.

**Port note:** Containers bind Modbus on **5020** (non-privileged). Kubernetes Services expose **502** → `targetPort` 5020 so clients inside the cluster connect to `crusher-1:502` as in field deployments.

| HR | Name | Type | Range / values | Description |
|----|------|------|----------------|-------------|
| 0 | `fill_pct` | uint16 | 0–100 | Bin fill level percentage |
| 1 | `at_capacity` | uint16 | 0 or 1 | 1 when fill ≥ capacity threshold (default 90%) |
| 2 | `status_code` | uint16 | 0–3 | 0=empty, 1=accepting, 2=full, 3=fault |
| 3 | `throughput_tph` | uint16 | 0–65535 | Processing rate (tons per hour) |
| 4 | `dump_count` | uint16 | 0–65535 | Cumulative dump counter |
| 5 | `ready` | uint16 | 0 or 1 | Ready to accept material |

### Status codes

| Code | Name | Meaning |
|------|------|---------|
| 0 | `empty` | Bin nearly empty, ready for dumps |
| 1 | `accepting` | Normal operation, accepting trucks |
| 2 | `full` | At or above capacity threshold |
| 3 | `fault` | Fault condition (simulator reserved) |

### PLC behaviour

`crusher_plc.py` exposes Modbus TCP holding registers. Fill increases **only** when an external client writes registers (typically `crusher-fill-bridge` in `fleet-integration`, which subscribes to truck MQTT telemetry and detects dump events). The PLC does **not** simulate truck dumps autonomously.

Optional slow drain (`DRAIN_RATE_PCT`) simulates ore processing — fill decreases over time but never increases without external Modbus writes.

Default state: empty (`INITIAL_FILL_PCT=0`) until trucks dump via the orchestration layer.

Configure per deployment with `CRUSHER_ID`, `INITIAL_FILL_PCT`, and timing env vars (see `openshift/crusher-fleet/02-configmaps-secrets.yaml`).

---

## PostgreSQL schema

The historian creates tables on startup (same pattern as `mqtt-ingest` in truck-fleet).

### `crusher_telemetry` (time-series history)

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Primary key |
| `crusher_id` | VARCHAR(32) | e.g. `crusher-1` |
| `fill_pct` | REAL | Fill percentage |
| `at_capacity` | BOOLEAN | At capacity flag |
| `status_code` | INTEGER | Modbus status code |
| `status` | VARCHAR(32) | Human-readable status |
| `throughput_tph` | REAL | Throughput |
| `dump_count` | INTEGER | Dump counter |
| `ready` | BOOLEAN | Ready flag |
| `recorded_at` | TIMESTAMPTZ | Sample timestamp |

Index: `(crusher_id, recorded_at DESC)`.

### `crusher_state` (latest snapshot)

Same fields as telemetry (except `id`), keyed by `crusher_id`. Updated via upsert on each poll cycle. `updated_at` tracks last write.

---

## Repository layout

| Path | Purpose |
|------|---------|
| [`poc/crusher-fleet/crusher_plc.py`](../../poc/crusher-fleet/crusher_plc.py) | Modbus TCP crusher PLC simulator |
| [`poc/crusher-fleet/historian.py`](../../poc/crusher-fleet/historian.py) | Modbus poller → PostgreSQL |
| [`openshift/crusher-fleet/`](../../openshift/crusher-fleet/) | OpenShift manifests |

---

## Deployment

### Prerequisites

- OpenShift cluster with `oc` logged in
- Push application code to Git (BuildConfigs pull from GitHub)

### Apply order

```bash
oc apply -f openshift/crusher-fleet/01-namespace.yaml
oc apply -f openshift/crusher-fleet/02-configmaps-secrets.yaml
oc apply -f openshift/crusher-fleet/03-postgresql.yaml
oc apply -f openshift/crusher-fleet/05-buildconfigs.yaml
oc start-build crusher-plc historian -n crusher-fleet --wait
oc apply -f openshift/crusher-fleet/04-crusher-plc.yaml
oc apply -f openshift/crusher-fleet/06-historian.yaml
```

See [`openshift/crusher-fleet/README.md`](../../openshift/crusher-fleet/README.md) for manifest details.

---

## Verification

```bash
# Pod status
oc get pods -n crusher-fleet

# Historian logs
oc logs -n crusher-fleet deploy/historian --tail=20

# Latest state
oc exec -n crusher-fleet deploy/postgresql -- \
  env PGPASSWORD=crusherfleet-demo psql -U crusherfleet -d crusherfleet \
  -c "SELECT crusher_id, fill_pct, status, at_capacity, dump_count, updated_at FROM crusher_state;"

# Recent telemetry
oc exec -n crusher-fleet deploy/postgresql -- \
  env PGPASSWORD=crusherfleet-demo psql -U crusherfleet -d crusherfleet \
  -c "SELECT crusher_id, fill_pct, status, recorded_at FROM crusher_telemetry ORDER BY recorded_at DESC LIMIT 10;"
```

---

## Independence and orchestration integration

| Concern | crusher-fleet | truck-fleet / fleet-integration |
|---------|---------------|--------------------------------|
| Namespace | `crusher-fleet` | `truck-fleet`, `fleet-integration` |
| Field protocol | Modbus TCP | MQTT (trucks), Kafka (orchestration) |
| Data store | PostgreSQL (historian) | PostgreSQL (mqtt-ingest) |
| Fill source | External Modbus writes only | `crusher-fill-bridge` writes Modbus after truck dumps |
| Northbound handoff | Historian → PostgreSQL | `crusher-fill-bridge` → `fleet.crushers.state` |

Crushers do **not** subscribe to Kafka or MQTT. Truck dump events flow:

1. Truck agent publishes MQTT telemetry (`state=dumping`, `destination_crusher`, decreasing `load_pct`)
2. `kafka-truck-bridge` produces `fleet.trucks.telemetry`
3. `crusher-fill-bridge` detects dump events and writes crusher Modbus registers
4. Historian polls Modbus → PostgreSQL `crusher_state`
5. `crusher-fill-bridge` also publishes `fleet.crushers.state` for `destination-router`

No direct truck→crusher coupling — connection is only through `fleet-integration`.

---

## Local development

```bash
cd poc/crusher-fleet
pip install -r requirements.txt

# Terminal 1 — crusher PLC
CRUSHER_ID=crusher-1 MODBUS_PORT=5020 python crusher_plc.py

# Terminal 2 — historian (requires local Postgres)
export PGHOST=localhost PGDATABASE=crusherfleet PGUSER=crusherfleet PGPASSWORD=changeme
export CRUSHER_TARGETS=crusher-1:localhost:5020
python historian.py
```
