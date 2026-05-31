# Truck fleet PoC (`poc/truck-fleet`)

Simulated haul trucks publish operational telemetry over **MQTT**; **mqtt-ingest** writes history and latest state into **PostgreSQL**. Deploy on OpenShift via **`../../openshift/truck-fleet/`**.

## Components

| File | Role |
|------|------|
| **`truck_agent.py`** | Cycles loading → hauling → dumping → returning; subscribes to `new-destination/{TRUCK_ID}/+`; publishes JSON to `fleet/trucks/{truck_id}/telemetry`. |
| **`crusher_assignment.py`** | Reads `truck-crusher-assignments` ConfigMap; publishes retained destinations to `new-destination/{truck_id}/{crusher_name}`; watches for runtime changes. |
| **`mqtt_ingest.py`** | Subscribes to `fleet/trucks/+/telemetry`; inserts `truck_telemetry` rows and upserts `truck_state`. |
| **`requirements.txt`** | `paho-mqtt`, `psycopg2-binary`. |
| **`Dockerfile.truck-agent`** | Image for truck Pods. |
| **`Dockerfile.crusher-assignment`** | Image for crusher-assignment Deployment. |
| **`Dockerfile.mqtt-ingest`** | Image for ingest Deployment. |

## MQTT topics

| Topic | Direction | Payload |
|-------|-----------|---------|
| `fleet/trucks/{truck_id}/telemetry` | Truck → broker | JSON: `truck_id`, `state`, `position_x/y`, `lat/lon`, `progress`, `speed_kmh`, `load_pct`, `destination_crusher`, `assignment_source`, `timestamp` |
| `new-destination/{truck_id}/{crusher_name}` | crusher-assignment → broker | JSON: `truck_id`, `crusher_name`, `assigned_at`, `source` (retained; crusher also in topic path) |

Truck states: `loading`, `hauling`, `dumping`, `returning`.

## Environment variables

**Truck agent:** `TRUCK_ID`, `DEFAULT_CRUSHER`, `MQTT_HOST`, `MQTT_PORT`, `MQTT_NEW_DESTINATION_TOPIC`, `TICK_SEC`, `MQTT_TOPIC_PREFIX`.

**Crusher assignment:** `MQTT_HOST`, `MQTT_NEW_DESTINATION_TOPIC`, `ASSIGNMENTS_CONFIGMAP`, `POD_NAMESPACE`, `ASSIGNMENT_SOURCE`.

**Ingest:** `MQTT_HOST`, `MQTT_TOPIC_SUBSCRIBE`, `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`.

Defaults match the **`truck-fleet-env`** ConfigMap and **`postgresql-credentials`** Secret in OpenShift manifests.

## Local run (optional)

```bash
cd poc/truck-fleet
pip install -r requirements.txt

# Terminal 1 — Mosquitto + Postgres via docker compose or cluster services
export TRUCK_ID=TR1 MQTT_HOST=localhost PGHOST=localhost
python truck_agent.py

# Terminal 2
python mqtt_ingest.py
```

## OpenShift

See **`../../openshift/truck-fleet/`** and **`../../docs/mining-fleet/truck-fleet/README.md`**.
